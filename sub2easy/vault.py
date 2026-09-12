"""Password-unlocked SQLite vault; all account/connector payloads are AEAD-encrypted."""

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sub2easy.intake import has_login_material


# List/diagnostic surfaces are not credential export APIs. Full encrypted
# records remain available through account()/get_setting() for worker use.
_SECRET_KEYS = frozenset({
    'password', 'cleartextpassword', 'totp', 'totpsecret', '2fa', 'token',
    'accesstoken', 'refreshtoken', 'idtoken', 'apikey', 'adminkey', 'sessionkey',
    'cookie', 'nvtcookie', 'ssotoken', 'sso', 'ssorw', 'privatekey', 'agentprivatekey',
    'awssecretaccesskey', 'awssessiontoken', 'serviceaccount', 'serviceaccountjson',
    'clientsecret', 'secret', 'authorization', 'credentials', 'rawresult',
})


def _secret_key(key):
    return re.sub(r'[^a-z0-9]', '', key.casefold()) in _SECRET_KEYS


def _public_metadata(value, secrets=()):
    if isinstance(value, dict):
        return {k: _public_metadata(v, secrets) for k, v in value.items() if not _secret_key(k)}
    if isinstance(value, list):
        return [_public_metadata(v, secrets) for v in value]
    if isinstance(value, str) and _contains_secret(value, secrets):
        return '[REDACTED]'
    return value


def _secret_values(value, sensitive=False):
    if isinstance(value, dict):
        for k, v in value.items():
            container = k in {'authorization', 'credentials', 'raw_result'}
            yield from _secret_values(v, sensitive or (_secret_key(k) and not container))
    elif isinstance(value, list):
        for v in value:
            yield from _secret_values(v, sensitive)
    elif sensitive and isinstance(value, str) and value:
        yield value


def _contains_secret(value, secrets):
    # Short synthetic/configured values like "ADMIN" must not erase fixed
    # codes such as ADMIN_AUTH_FAILED merely because they share a word.
    return any(value == secret or (len(secret) >= 8 and secret in value) for secret in secrets)


def _diagnostic(value, secrets=(), stage=False):
    pattern = r'[a-z][a-z0-9_]{0,63}' if stage else r'[A-Z][A-Z0-9_]{0,127}'
    if value == '':
        return ''
    if (not isinstance(value, str) or not re.fullmatch(pattern, value)
            or _contains_secret(value, secrets)):
        return 'redacted' if stage else 'REDACTED_DIAGNOSTIC'
    return value


class VaultError(ValueError):
    pass


class Vault:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.mutex = threading.RLock()
        self._transaction_serial = 0
        self.key = None
        self.runtime_busy = lambda account: False
        self.path = directory / "vault.sqlite3"
        if self.path.is_symlink():
            raise VaultError("INVALID_VAULT_PATH")
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, lookup TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL,
            status TEXT NOT NULL, payload BLOB NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL, updated REAL NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS one_pending_job ON jobs(account_id)
            WHERE state IN ('queued', 'running');
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'kind' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'")
        if 'context' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN context BLOB")
        if 'cancel_requested' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
        if 'started' not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN started REAL")
            # Legacy terminal jobs have no reliable start evidence, so count
            # them conservatively rather than refunding attempted requests.
            self.db.execute("UPDATE jobs SET started=created WHERE state!='queued'")
        # A crash after POST is ambiguous. Never automatically resend that login.
        now = time.time()
        self.db.execute("UPDATE accounts SET status=CASE WHEN status='write_unknown' THEN status ELSE 'unknown' END,updated=? "
                        "WHERE id IN (SELECT account_id FROM jobs WHERE state='running')", (now,))
        self.db.execute("UPDATE jobs SET state='unknown',code='INTERRUPTED_RESULT_UNKNOWN',updated=? WHERE state='running'", (now,))
        self.db.commit()

    @contextmanager
    def transaction(self):
        """Atomically compose vault calls, e.g. queue + deployment checkpoint.

        SQLite connection contexts commit inner calls prematurely. Savepoints
        instead nest, and the RLock also keeps the worker out until commit.
        Do not hold this transaction during network requests.
        """
        with self.mutex:
            self.require_key()
            self._transaction_serial += 1
            savepoint = 'vault_' + str(self._transaction_serial)
            self.db.execute('SAVEPOINT ' + savepoint)
            try:
                yield self
            except BaseException:
                self.db.execute('ROLLBACK TO ' + savepoint)
                self.db.execute('RELEASE ' + savepoint)
                raise
            else:
                try:
                    self.db.execute('RELEASE ' + savepoint)
                except BaseException:
                    self.db.execute('ROLLBACK TO ' + savepoint)
                    self.db.execute('RELEASE ' + savepoint)
                    raise

    @property
    def initialized(self):
        with self.mutex:
            return self.db.execute("SELECT 1 FROM kv WHERE key='check'").fetchone() is not None

    def require_key(self):
        if self.key is None:
            raise VaultError("VAULT_LOCKED")
        return self.key

    def _seal(self, purpose, value):
        nonce = os.urandom(12)
        plain = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        return nonce + AESGCM(self.require_key()).encrypt(nonce, plain, purpose.encode())

    def _open(self, purpose, blob):
        key = self.require_key()
        try:
            return json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], purpose.encode()))
        except (InvalidTag, ValueError, TypeError):
            raise VaultError("VAULT_DECRYPT_FAILED") from None

    def unlock(self, password, setup=False):
        if not isinstance(password, str) or len(password) < 10 or len(password) > 1024:
            raise VaultError("MASTER_PASSWORD_MIN_10")
        with self.mutex:
            if self.db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                raise VaultError('JOB_RUNNING_CANNOT_UNLOCK')
            if setup and self.initialized:
                raise VaultError("VAULT_ALREADY_INITIALIZED")
            if not setup and not self.initialized:
                raise VaultError("VAULT_NOT_INITIALIZED")
            salt_row = self.db.execute("SELECT value FROM kv WHERE key='salt'").fetchone()
            salt = bytes(salt_row[0]) if salt_row else os.urandom(16)
            key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
            self.key = key
            try:
                if setup:
                    with self.db:
                        self.db.execute("INSERT INTO kv VALUES ('salt', ?)", (salt,))
                        self.db.execute("INSERT INTO kv VALUES ('check', ?)", (self._seal("check", {"ok": True}),))
                else:
                    blob = self.db.execute("SELECT value FROM kv WHERE key='check'").fetchone()[0]
                    if self._open("check", blob) != {"ok": True}:
                        raise VaultError("VAULT_DECRYPT_FAILED")
            except Exception:
                self.key = None
                raise

    def lock(self):
        with self.mutex:
            if self.db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone():
                raise VaultError("JOB_RUNNING_CANNOT_LOCK")
            self.key = None  # Python cannot promise memory zeroization.

    def get_setting(self, key, default=None):
        with self.mutex:
            self.require_key()
            row = self.db.execute("SELECT value FROM kv WHERE key=?", ("setting:" + key,)).fetchone()
            return default if row is None else self._open("setting:" + key, row[0])

    def set_setting(self, key, value):
        with self.transaction():
            blob = self._seal("setting:" + key, value)
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("setting:" + key, blob))

    def import_materials(self, batch, profile, include_results=False):
        if batch.issues:
            raise VaultError("FIX_IMPORT_ERRORS_FIRST")
        added, duplicates, conflicts, supplemented = [], list(batch.duplicates), [], []
        results = [{'index':i,'state':'duplicate','code':'DUPLICATE_IN_FILE'} for i in batch.duplicates]
        with self.transaction():
            key = self.require_key()
            for material in batch.materials:
                lookup = hmac.new(key, ("login:openai:" + material.account).encode(), hashlib.sha256).hexdigest()
                row = self.db.execute("SELECT id,payload,status FROM accounts WHERE lookup=?", (lookup,)).fetchone()
                login = {"account": material.account, "password": material.password, "totp_secret": material.totp_secret}
                if row:
                    old = self._open("account:" + row[0], row[1])
                    existing = old['login']
                    # Fill missing fields only. A partial login is not permission
                    # to replace an already supplied password or TOTP seed.
                    compatible = all(not existing.get(k) or existing[k] == v for k, v in login.items())
                    if (not has_login_material(existing) and compatible
                            and row[2] not in {'review', 'unknown', 'write_unknown'}
                            and not self._import_busy(row[0], old, allow_completed=True)):
                        old['login'] = {**existing, **login}
                        old['source'] = 'sub2_json+login'
                        self.db.execute('UPDATE accounts SET payload=?,revision=revision+1,updated=? WHERE id=?',
                                        (self._seal('account:' + row[0], old), time.time(), row[0]))
                        supplemented.append(material.line)
                        results.append({'index':material.line,'state':'supplemented','account_id':row[0]})
                    else:
                        identical = all(existing.get(k) == v for k, v in login.items())
                        (duplicates if identical else conflicts).append(material.line)
                        results.append({'index':material.line,'state':'duplicate' if identical else 'conflict',
                                        'account_id':row[0],'code':'ALREADY_IMPORTED' if identical else 'CONFLICTING_LOGIN_MATERIAL'})
                    continue
                account_id = str(uuid.uuid4())
                payload = {"login": login, "profile": profile, "binding": None, "authorization": None,
                           "imported_at": time.time(), "source": "login"}
                self.db.execute("INSERT INTO accounts VALUES (?,?,1,'local',?,?)",
                                (account_id, lookup, self._seal("account:" + account_id, payload), time.time()))
                added.append(account_id)
                results.append({'index':material.line,'state':'added','account_id':account_id})
        report = {"added": len(added), "duplicate_lines": sorted(duplicates), "conflict_lines": sorted(conflicts),
                  "supplemented_lines": sorted(supplemented)}
        if include_results:report['results']=sorted(results,key=lambda r:r['index'])
        return report

    def _import_busy(self, account_id, account, allow_completed=False):
        monitor = account.get('monitor') or {}
        return (self.runtime_busy({**account,'id':account_id}) or self.pending(account_id) or bool(monitor.get('owned_pause'))
                or bool(monitor.get('blocked')) or bool(account.get('retirement'))
                or bool(account.get('deployment') and
                        (not allow_completed or account['deployment'].get('state')!='complete'))
                or bool(account.get('write_intent') and account['write_intent'].get('state') != 'confirmed'))

    def import_sub2(self, batch, profile, update_credentials=False):
        """Same email-key as text intake; changes never write to the cloud."""
        results = [{'index': e['index'], 'state': 'failed', 'code': e['code']} for e in batch.errors]
        results += [{'index': i, 'state': 'duplicate', 'code': 'SUB2_DUPLICATE_IN_FILE'} for i in batch.duplicates]
        with self.transaction():
            key = self.require_key()
            for item in batch.items:
                auth = item.authorization
                identity = auth['identity']
                lookup = hmac.new(key, ('login:openai:' + identity['account_email']).encode(), hashlib.sha256).hexdigest()
                row = self.db.execute('SELECT id,payload,status FROM accounts WHERE lookup=?', (lookup,)).fetchone()
                if row:
                    aid = row[0]
                    old = self._open('account:' + aid, row[1])
                    identities = [(old.get(source) or {}).get('identity') for source in ('binding', 'authorization')]
                    if any(expected and (expected['account_email'] != identity['account_email']
                            or expected['chatgpt_account_id'] != identity['chatgpt_account_id']
                            or (expected.get('chatgpt_user_id') and expected['chatgpt_user_id'] != identity.get('chatgpt_user_id')))
                           for expected in identities):
                        results.append({'index':item.index,'state':'conflict','code':'SUB2_IDENTITY_CONFLICT','account_id':aid})
                        continue
                    if old.get('authorization') == auth:
                        results.append({'index':item.index,'state':'duplicate','code':'SUB2_ALREADY_IMPORTED','account_id':aid})
                        continue
                    if self._import_busy(aid,old) or row[2] in {'review','unknown','write_unknown'}:
                        results.append({'index':item.index,'state':'conflict','code':'SUB2_ACCOUNT_BUSY','account_id':aid})
                        continue
                    if old.get('authorization') and not update_credentials:
                        results.append({'index':item.index,'state':'conflict','code':'SUB2_UPDATE_CONFIRM_REQUIRED','account_id':aid})
                        continue
                    old.update(authorization=auth, raw_result=item.document, result_code='SUB2_IMPORTED',
                               source='sub2_json+login' if has_login_material(old['login']) else 'sub2_json')
                    self.db.execute("UPDATE accounts SET payload=?,revision=revision+1,status='authorized',updated=? WHERE id=?",
                                    (self._seal('account:'+aid,old),time.time(),aid))
                    state = 'updated'
                else:
                    aid = str(uuid.uuid4())
                    payload = {'login': {'account': identity['account_email']}, 'profile': profile,
                               'binding': None, 'authorization': auth, 'raw_result': item.document,
                               'imported_at': time.time(), 'source': 'sub2_json', 'result_code':'SUB2_IMPORTED'}
                    self.db.execute("INSERT INTO accounts VALUES (?,?,1,'authorized',?,?)",
                                    (aid, lookup, self._seal('account:'+aid,payload),time.time()))
                    state = 'added'
                results.append({'index':item.index,'state':state,'code':'SUB2_IMPORTED','account_id':aid})
        results.sort(key=lambda r:r['index'])
        return {'format':'sub2','added':sum(r['state']=='added' for r in results),
                'updated':sum(r['state']=='updated' for r in results),
                'duplicates':sum(r['state']=='duplicate' for r in results),
                'failed':sum(r['state'] in {'failed','conflict'} for r in results),
                'ignored_proxies':batch.ignored_proxies,'results':results}

    def account(self, account_id):
        with self.mutex:
            self.require_key()
            row = self.db.execute("SELECT revision,status,payload,updated FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise VaultError("ACCOUNT_NOT_FOUND")
            # DB metadata is authoritative even for legacy payloads containing
            # fields that update_account() now rejects as reserved.
            return {**self._open("account:" + account_id, row[2]),
                    "id": account_id, "revision": row[0], "status": row[1], "updated": row[3]}

    def update_account(self, account_id, status=None, **updates):
        if {'id', 'revision', 'updated'} & updates.keys():
            raise VaultError('RESERVED_ACCOUNT_FIELD')
        with self.transaction():
            current = self.account(account_id)
            login_changed = 'login' in updates and updates['login'] != current['login']
            if login_changed:
                if (not isinstance(updates['login'], dict)
                        or updates['login'].get('account') != current['login']['account']):
                    raise VaultError('ACCOUNT_IDENTITY_IMMUTABLE')
                if self.pending(account_id):
                    raise VaultError('ACCOUNT_BUSY')
            payload = {k: v for k, v in current.items() if k not in {"id", "revision", "status", "updated"}}
            payload.update(updates)
            self.db.execute("UPDATE accounts SET status=?,payload=?,revision=revision+?,updated=? WHERE id=?",
                            (status or current["status"], self._seal("account:" + account_id, payload),
                             int(login_changed), time.time(), account_id))

    def accounts(self):
        with self.mutex:
            self.require_key()
            rows = self.db.execute("SELECT id FROM accounts ORDER BY rowid DESC").fetchall()
            connection_secrets = tuple(_secret_values(self.get_setting('connection', {})))
            result = []
            for (account_id,) in rows:
                a = self.account(account_id)
                secrets = (*connection_secrets, *_secret_values(a))
                deployment = a.get('deployment') or {}
                monitor = a.get('monitor') or {}
                result.append({
                    "id": account_id, "label": a["login"]["account"],
                    "status": a["status"], "revision": a["revision"], "updated": a["updated"],
                    "imported_at": a.get("imported_at"),
                    "binding": _public_metadata(a["binding"], secrets), "profile": _public_metadata(a["profile"], secrets),
                    "has_result": a.get("raw_result") is not None,
                    "validated": a.get("authorization") is not None,
                    "result_code": _diagnostic(a.get("result_code", ""), secrets),
                    "has_login_material": has_login_material(a['login']),
                    "source": a.get('source','login'),
                    "retirement": {k:_public_metadata((a.get('retirement') or {}).get(k),secrets)
                                   for k in ('state','step','code','group_id','completed_at')},
                    "deployment": {k:(_diagnostic(deployment.get(k), secrets)
                                      if k == 'code' and deployment.get(k) is not None
                                      else _public_metadata(deployment.get(k), secrets)) for k in
                                   ('state','step','code','cloud_id','verified_at','updated')},
                    "monitor": {key: (_diagnostic(monitor.get(key), secrets)
                                      if key == 'last_code' and monitor.get(key) is not None
                                      else _public_metadata(monitor.get(key), secrets)) for key in (
                        "enabled", "state", "last_check", "last_code", "first_seen", "blocked", "last_recovered",
                        "auth_401", "cloud_status", "cloud_schedulable",
                        "next_retry", "continuation_attempts", "last_http_status",
                        "probe_reauth_attempts",
                        "model_id", "enrollment_code",
                    )},
                })
            return result

    def queue(self, account_ids, kind="manual", context=None):
        if not isinstance(kind, str) or kind not in {"manual", "auto_reauth", "recovery_continue", "server_deploy", "retire"}:
            raise VaultError("INVALID_JOB_KIND")
        if isinstance(account_ids, (str, bytes)):
            raise VaultError('INVALID_ACCOUNT_IDS')
        try:
            account_ids = list(account_ids)
        except TypeError:
            raise VaultError('INVALID_ACCOUNT_IDS') from None
        if any(not isinstance(aid, str) or not aid for aid in account_ids):
            raise VaultError('INVALID_ACCOUNT_IDS')
        if context is not None and not isinstance(context, dict):
            raise VaultError('INVALID_JOB_CONTEXT')
        with self.transaction():
            self.require_key()
            rows = [self.account(account_id) for account_id in dict.fromkeys(account_ids)]
            queued = []
            for a in rows:
                # Retrying the same enqueue is idempotent even after the worker
                # has changed status or created a write/recovery checkpoint.
                if self.pending(a['id']):
                    continue
                if self.runtime_busy(a):
                    raise VaultError('OPERATION_RUNNING')
                if kind=='retire':self.validate_retirement(a)
                elif a.get('retirement'):
                    raise VaultError('ACCOUNT_RETIRED')
                if kind in {'manual','auto_reauth'} and not has_login_material(a['login']):
                    raise VaultError('LOGIN_MATERIAL_MISSING')
                if kind not in {'server_deploy','retire'} and a.get('deployment') and a['deployment'].get('state')!='complete':
                    raise VaultError('DEPLOYMENT_INCOMPLETE')
                monitor = a.get('monitor') or {}
                if kind == 'manual' and (monitor.get('owned_pause') or monitor.get('blocked')):
                    raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
                if a.get("write_intent") and a["write_intent"].get("state") != "confirmed":
                    raise VaultError("PREVIOUS_WRITE_NEEDS_RECONCILIATION")
                if a["status"] in {"review", "unknown", "write_unknown"} and kind!='retire':
                    raise VaultError("RESULT_REVIEW_REQUIRED_BEFORE_RETRY")
                # Explicit imports/manual logins/checkpoint retries have no
                # historical-attempt cap. Only unattended logins retain a budget.
                if kind == 'auto_reauth':
                    recent = self.db.execute(
                        "SELECT COUNT(*) FROM jobs WHERE account_id=? AND COALESCE(started,created)>? "
                        "AND NOT (state='cancelled' AND started IS NULL) "
                        "AND kind IN ('manual','auto_reauth')",
                        (a["id"], time.time() - 1800),
                    ).fetchone()[0]
                    if recent >= 2:
                        raise VaultError("REAUTH_BUDGET_2_PER_30_MIN")
                job_id = str(uuid.uuid4())
                # The mutex and pending index already serialize this single-
                # process queue. Other integrity failures must roll back, not
                # masquerade as duplicates after a partially committed batch.
                self.db.execute("INSERT INTO jobs (id,account_id,revision,state,created,updated,kind,context) VALUES (?,?,?,'queued',?,?,?,?)",
                                (job_id, a["id"], a["revision"], time.time(), time.time(),kind,
                                 self._seal('job:' + job_id, context or {})))
                queued.append(job_id)
            return queued

    @staticmethod
    def validate_retirement(a):
        if (a['status'] not in {'review','failed'} or not a.get('binding')
                or 'EXPECTED_WORKSPACE_NOT_RETURNED' not in {
                    a.get('result_code'),(a.get('monitor') or {}).get('last_code')}):
            raise VaultError('RETIREMENT_NOT_ELIGIBLE')
        if (a.get('deployment') or {}).get('mutation'):
            raise VaultError('RETIREMENT_WRITE_UNKNOWN')

    def claim(self, continuation_only=False, eligible=None):
        with self.mutex:
            if self.key is None:
                return None
            with self.transaction():
                sql="SELECT id,account_id,revision,kind,context FROM jobs WHERE state='queued'"
                if continuation_only:sql+=" AND kind IN ('recovery_continue','server_deploy','retire')"
                for row in self.db.execute(sql+" ORDER BY created,rowid").fetchall():
                    try:
                        a = self.account(row[1])
                        if eligible is not None and not eligible(a):
                            continue
                        if a['revision'] != row[2]:
                            self.finish(row[0], 'cancelled', 'STALE_MATERIAL')
                            continue
                        if row[3]=='retire':self.validate_retirement(a)
                        elif a.get('retirement'):raise VaultError('ACCOUNT_RETIRED')
                        if a['status'] in {'review', 'unknown', 'write_unknown'} and row[3]!='retire':
                            raise VaultError('RESULT_REVIEW_REQUIRED_BEFORE_RETRY')
                        if a.get('write_intent') and a['write_intent'].get('state') != 'confirmed':
                            raise VaultError('PREVIOUS_WRITE_NEEDS_RECONCILIATION')
                        if row[3] in {'manual', 'auto_reauth'} and not has_login_material(a['login']):
                            raise VaultError('LOGIN_MATERIAL_MISSING')
                        if row[3] not in {'server_deploy','retire'} and a.get('deployment') and a['deployment'].get('state') != 'complete':
                            raise VaultError('DEPLOYMENT_INCOMPLETE')
                        monitor = a.get('monitor') or {}
                        if row[3] == 'manual' and (monitor.get('owned_pause') or monitor.get('blocked')):
                            raise VaultError('MONITOR_RECOVERY_NEEDS_REVIEW')
                        context = self._open('job:' + row[0], row[4]) if row[4] is not None else {}
                        if not isinstance(context, dict):
                            raise VaultError('INVALID_JOB_CONTEXT')
                    except VaultError as exc:
                        # A bad queued record must not starve unrelated jobs.
                        self.finish(row[0], 'failed', str(exc))
                        continue
                    now = time.time()
                    self.db.execute("UPDATE jobs SET state='running',started=?,updated=? WHERE id=? AND state='queued'",
                                    (now, now, row[0]))
                    return {"id": row[0], "account_id": row[1], "revision": row[2], "kind": row[3],
                            "context": context, "cancel_requested": False, "started": now}
                return None

    def job_counts(self):
        with self.mutex:
            self.require_key()
            return dict(self.db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state').fetchall())

    def job_stage(self, job_id, stage):
        with self.transaction():
            stage = _diagnostic(stage, self._job_secrets(job_id), stage=True)
            self.db.execute("UPDATE jobs SET stage=?,updated=? WHERE id=? AND state='running'", (stage,time.time(),job_id))

    def job_checkpoint_time(self, job_id, account_id):
        """Stable terminal-job time for migration; never substitute current time."""
        with self.mutex:
            self.require_key()
            row = self.db.execute(
                "SELECT updated FROM jobs WHERE id=? AND account_id=? AND kind='auto_reauth' "
                "AND state IN ('failed','succeeded')", (job_id, account_id),
            ).fetchone()
            return row[0] if row else None

    def cancel_auto(self, account_id=None):
        with self.transaction():
            sql = "UPDATE jobs SET state='cancelled',code='MONITOR_DISABLED',updated=? WHERE kind IN ('auto_reauth','recovery_continue') AND state='queued'"
            args = [time.time()]
            if account_id:
                sql += ' AND account_id=?'; args.append(account_id)
            self.db.execute(sql,args)

    def pending(self, account_id):
        with self.mutex:
            return self.db.execute("SELECT 1 FROM jobs WHERE account_id=? AND state IN ('queued','running')", (account_id,)).fetchone() is not None

    def auto_jobs_since(self, since):
        with self.mutex:
            return self.db.execute("SELECT COUNT(*) FROM jobs WHERE kind='auto_reauth' AND COALESCE(started,created)>? "
                                   "AND NOT (state='cancelled' AND started IS NULL)", (since,)).fetchone()[0]

    def _job_secrets(self, job_id):
        """Known credentials must not be copied into plaintext code/stage columns."""
        secrets = []
        row = self.db.execute('SELECT account_id,context FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row:
            try:
                secrets.extend(_secret_values(self.account(row[0])))
            except VaultError:
                pass  # Still allow a corrupt/orphan queued job to be quarantined.
            if row[1] is not None:
                try:
                    secrets.extend(_secret_values(self._open('job:' + job_id, row[1])))
                except VaultError:
                    pass
        try:
            secrets.extend(_secret_values(self.get_setting('connection', {})))
        except VaultError:
            pass
        return secrets

    def finish(self, job_id, state, code="", stage=""):
        if not isinstance(state, str) or state not in {'succeeded', 'failed', 'cancelled', 'review', 'unknown'}:
            raise VaultError('INVALID_JOB_STATE')
        with self.transaction():
            secrets = self._job_secrets(job_id)
            self.db.execute("UPDATE jobs SET state=?,code=?,stage=?,updated=? "
                            "WHERE id=? AND state IN ('queued','running')",
                            (state, _diagnostic(code, secrets), _diagnostic(stage, secrets, stage=True), time.time(), job_id))

    def jobs(self, job_ids=None):
        with self.mutex:
            self.require_key()
            columns = ("id", "account_id", "state", "code", "stage", "created", "updated", "kind", "cancel_requested", "started")
            sql = "SELECT id,account_id,state,code,stage,created,updated,kind,cancel_requested,started FROM jobs"
            if job_ids is None:
                rows = self.db.execute(sql+" ORDER BY created DESC,rowid DESC LIMIT 200").fetchall()
            else:
                if not isinstance(job_ids,list) or len(job_ids)>1000 or any(not isinstance(i,str) for i in job_ids):
                    raise VaultError('INVALID_JOB_IDS')
                if not job_ids:return []
                rows = self.db.execute(sql+' WHERE id IN ('+','.join('?' for _ in job_ids)+')',job_ids).fetchall()
            result = []
            for row in rows:
                job = dict(zip(columns, row))
                secrets = self._job_secrets(job['id'])
                job['code'] = _diagnostic(job['code'], secrets)
                job['stage'] = _diagnostic(job['stage'], secrets, stage=True)
                job['cancel_requested'] = bool(job['cancel_requested'])
                result.append(job)
            return result

    def cancel_jobs(self, job_ids=None):
        """Cancel queued work; request cooperative cancellation of running work.

        Running jobs retain their pending slot and lock prohibition until the
        worker settles the actual result. A dispatched POST cannot be undone.
        The worker can inspect cancellation_requested() at safe checkpoints.
        """
        if job_ids is not None:
            if isinstance(job_ids, (str, bytes)):
                raise VaultError('INVALID_JOB_IDS')
            try:
                job_ids = list(job_ids)
            except TypeError:
                raise VaultError('INVALID_JOB_IDS') from None
            if any(not isinstance(jid, str) or not jid for jid in job_ids):
                raise VaultError('INVALID_JOB_IDS')
        with self.transaction():
            if job_ids is None:
                rows = self.db.execute("SELECT id,state FROM jobs WHERE state IN ('queued','running')").fetchall()
            else:
                rows = []
                for job_id in dict.fromkeys(job_ids):
                    row = self.db.execute('SELECT id,state FROM jobs WHERE id=?', (job_id,)).fetchone()
                    if row is None:
                        raise VaultError('JOB_NOT_FOUND')
                    rows.append(row)
            result = {'cancelled': [], 'cancel_requested': [], 'already_finished': []}
            now = time.time()
            for job_id, state in rows:
                if state == 'queued':
                    self.db.execute("UPDATE jobs SET state='cancelled',code='USER_CANCELLED',updated=? WHERE id=?", (now, job_id))
                    result['cancelled'].append(job_id)
                elif state == 'running':
                    self.db.execute('UPDATE jobs SET cancel_requested=1,updated=? WHERE id=? AND cancel_requested=0', (now, job_id))
                    result['cancel_requested'].append(job_id)
                else:
                    result['already_finished'].append(job_id)
            return result

    def cancellation_requested(self, job_id):
        with self.mutex:
            self.require_key()
            row = self.db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise VaultError('JOB_NOT_FOUND')
            return bool(row[0])

    def cancel_queued(self):
        with self.transaction():
            self.require_key()
            self.db.execute("UPDATE jobs SET state='cancelled',code='USER_CANCELLED',updated=? WHERE state='queued'", (time.time(),))

    def close(self):
        with self.mutex:
            self.key = None
            self.db.close()
