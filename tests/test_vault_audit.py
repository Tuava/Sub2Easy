"""Offline vault regression audit: temporary databases and synthetic secrets only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from sub2easy.intake import parse_batch
from sub2easy.sub2_import import parse_sub2
from sub2easy.vault import Vault, VaultError


MASTER = 'audit-synthetic-master-password'
PASSWORD = 'AUDIT_SYNTHETIC_PASSWORD'
TOTP = 'JBSWY3DPEHPK3PXP'
PROFILE = {'profile_id': 'audit', 'revision': 1, 'instance_id': 'https://audit.invalid',
           'staging_group_id': 10, 'target_group_ids': [20]}


def line(email='audit@example.invalid', password=PASSWORD):
    return f'{email}----{password}----{TOTP}'


def oauth(email='audit@example.invalid', token='AUDIT_ACCESS_TOKEN', user='audit-user'):
    return {'name': 'offline audit', 'platform': 'openai', 'type': 'oauth', 'credentials': {
        'email': email, 'chatgpt_account_id': 'audit-workspace', 'chatgpt_user_id': user,
        'client_id': 'audit-client', 'access_token': token, 'refresh_token': 'AUDIT_REFRESH_TOKEN',
        'expires_at': '2099-01-01T00:00:00Z'}}


class VaultAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sub2easy-vault-audit-')
        self.v = Vault(self.temp.name)
        self.v.unlock(MASTER, setup=True)

    def tearDown(self):
        self.v.close()
        self.temp.cleanup()

    def add(self, email='audit@example.invalid'):
        self.v.import_materials(parse_batch(line(email)), deepcopy(PROFILE))
        return next(a['id'] for a in self.v.accounts()
                    if self.v.account(a['id'])['login']['account'] == email)

    def import_json(self, document=None, update=False):
        return self.v.import_sub2(parse_sub2(json.dumps(document or oauth())), deepcopy(PROFILE), update)

    def add_json(self):
        return self.import_json()['results'][0]['account_id']

    def restart(self):
        self.v.close()
        self.v = Vault(self.temp.name)
        self.v.unlock(MASTER)

    def job(self, job_id):
        return next(job for job in self.v.jobs() if job['id'] == job_id)

    def test_import_reports_in_file_and_existing_duplicates_in_line_order(self):
        first, second = line(), line('second@example.invalid')
        result = self.v.import_materials(parse_batch(f'{first}\n{first}\n{second}\n{second}'), PROFILE)
        self.assertEqual(result, {'added': 2, 'duplicate_lines': [2, 4],
                                  'conflict_lines': [], 'supplemented_lines': []})
        before = {a['id']: self.v.account(a['id']) for a in self.v.accounts()}
        result = self.v.import_materials(parse_batch(f'{first}\n{first}\n{second}\n{second}'), PROFILE)
        self.assertEqual(result['duplicate_lines'], [1, 2, 3, 4])
        self.assertEqual(before, {a['id']: self.v.account(a['id']) for a in self.v.accounts()})

    def test_partial_login_conflict_never_replaces_existing_password(self):
        aid = self.add_json()
        self.v.update_account(aid, login={'account': 'audit@example.invalid', 'password': 'KEEP_THIS'})
        before = self.v.account(aid)
        result = self.v.import_materials(parse_batch(line()), PROFILE)
        self.assertEqual(result['conflict_lines'], [1])
        self.assertEqual(self.v.account(aid), before)

    def test_partial_login_matching_fields_can_be_supplemented_once(self):
        aid = self.add_json()
        self.v.update_account(aid, login={'account': 'audit@example.invalid', 'password': PASSWORD})
        before = self.v.account(aid)
        result = self.v.import_materials(parse_batch(line()), {'profile_id': 'not-replacement'})
        after = self.v.account(aid)
        self.assertEqual(result['supplemented_lines'], [1])
        self.assertEqual(after['revision'], before['revision'] + 1)
        for key in ('authorization', 'profile', 'binding', 'raw_result', 'imported_at'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.v.import_materials(parse_batch(line()), PROFILE)['duplicate_lines'], [1])

    def test_review_and_ambiguous_results_block_supplement_and_token_updates(self):
        aid = self.add_json()
        for status in ('review', 'unknown', 'write_unknown'):
            with self.subTest(status=status):
                self.v.update_account(aid, status=status)
                before = self.v.account(aid)
                self.assertEqual(self.v.import_materials(parse_batch(line()), PROFILE)['conflict_lines'], [1])
                self.assertEqual(self.import_json(oauth(token='UPDATED_ACCESS'), True)['results'][0]['code'],
                                 'SUB2_ACCOUNT_BUSY')
                self.assertEqual(self.v.account(aid), before)
                # Exact imports are harmless idempotent reads even while busy.
                self.assertEqual(self.import_json()['duplicates'], 1)

    def test_busy_recovery_states_block_login_supplement(self):
        aid = self.add_json()
        cases = [dict(monitor={'owned_pause': {'state': 'pending'}}),
                 dict(monitor={'blocked': True}), dict(deployment={'state': 'failed'}),
                 dict(write_intent={'state': 'pending'})]
        for changes in cases:
            with self.subTest(changes=changes):
                self.v.update_account(aid, monitor={}, deployment=None, write_intent=None)
                self.v.update_account(aid, **changes)
                before = self.v.account(aid)
                self.assertEqual(self.v.import_materials(parse_batch(line()), PROFILE)['conflict_lines'], [1])
                self.assertEqual(self.v.account(aid), before)

    def test_completed_deployment_allows_login_supplement_and_explicit_token_update(self):
        aid = self.add_json()
        self.v.update_account(aid, deployment={'state': 'complete'})
        self.assertEqual(self.v.import_materials(parse_batch(line()), PROFILE)['supplemented_lines'], [1])
        self.assertEqual(self.import_json(oauth(token='NEW_TOKEN'), True)['results'][0]['code'], 'SUB2_IMPORTED')

    def test_binding_does_not_hide_stricter_cached_user_identity(self):
        aid = self.add_json()
        identity = deepcopy(self.v.account(aid)['authorization']['identity'])
        identity.pop('chatgpt_user_id')
        self.v.update_account(aid, binding={'cloud_id': 42, 'instance': PROFILE['instance_id'], 'identity': identity})
        before = self.v.account(aid)
        result = self.import_json(oauth(token='NEW_TOKEN', user='different-user'), True)
        self.assertEqual(result['results'][0]['code'], 'SUB2_IDENTITY_CONFLICT')
        self.assertEqual(self.v.account(aid), before)

    def test_queue_batch_validation_rolls_back_earlier_inserts(self):
        aid, token_only = self.add(), self.import_json(oauth(email='token@example.invalid'))['results'][0]['account_id']
        with self.assertRaisesRegex(VaultError, 'LOGIN_MATERIAL_MISSING'):
            self.v.queue([aid, token_only])
        self.assertEqual(self.v.jobs(), [])
        self.restart()
        self.assertEqual(self.v.jobs(), [])

    def test_queue_integrity_error_is_not_swallowed_as_duplicate(self):
        first, second = self.add(), self.add('second@example.invalid')
        with self.v.db:
            self.v.db.execute(f"CREATE TRIGGER audit_reject BEFORE INSERT ON jobs WHEN NEW.account_id='{second}' "
                              "BEGIN SELECT RAISE(ABORT, 'AUDIT_INSERT_FAILED'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'AUDIT_INSERT_FAILED'):
            self.v.queue([first, second])
        self.assertEqual(self.v.jobs(), [])

    def test_nested_transaction_rolls_back_queue_account_and_settings(self):
        aid = self.add()
        before = self.v.account(aid)
        with self.assertRaisesRegex(RuntimeError, 'AUDIT_ROLLBACK'):
            with self.v.transaction():
                jobs = self.v.queue([aid], kind='server_deploy', context={'deployment_id': 'synthetic'})
                self.v.update_account(aid, deployment={'state': 'queued', 'job_id': jobs[0]})
                self.v.set_setting('audit', {'checkpoint': True})
                raise RuntimeError('AUDIT_ROLLBACK')
        self.restart()
        self.assertEqual(self.v.jobs(), [])
        self.assertEqual(self.v.account(aid), before)
        self.assertIsNone(self.v.get_setting('audit'))

    def test_nested_inner_rollback_can_be_caught_without_losing_outer_changes(self):
        with self.v.transaction():
            self.v.set_setting('outer', 1)
            try:
                with self.v.transaction():
                    self.v.set_setting('inner', 2)
                    raise ValueError('AUDIT_INNER')
            except ValueError:
                pass
            self.v.set_setting('outer_after', 3)
        self.restart()
        self.assertEqual(self.v.get_setting('outer'), 1)
        self.assertEqual(self.v.get_setting('outer_after'), 3)
        self.assertIsNone(self.v.get_setting('inner'))

    def test_atomic_queue_checkpoint_success_persists(self):
        aid = self.add()
        with self.v.transaction():
            job_id = self.v.queue([aid], kind='server_deploy', context={'deployment_id': 'synthetic'})[0]
            self.v.update_account(aid, deployment={'id': 'synthetic', 'state': 'queued', 'job_id': job_id})
        self.restart()
        self.assertEqual(self.v.account(aid)['deployment']['job_id'], job_id)
        self.assertEqual(self.v.claim()['context'], {'deployment_id': 'synthetic'})

    def test_concurrent_queue_and_claim_never_duplicate_pending_jobs(self):
        aid = self.add()
        with ThreadPoolExecutor(max_workers=8) as pool:
            queued = list(pool.map(lambda _: self.v.queue([aid, aid]), range(16)))
            claimed = list(pool.map(lambda _: self.v.claim(), range(16)))
        self.assertEqual(sum(map(len, queued)), 1)
        self.assertEqual(sum(job is not None for job in claimed), 1)
        self.assertTrue(self.v.pending(aid))

    def test_worker_cannot_claim_between_queue_and_checkpoint_commit(self):
        aid = self.add()
        entered = threading.Event()

        def claim():
            entered.set()
            job = self.v.claim()
            return job, self.v.account(aid).get('deployment')

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.v.transaction():
                job_id = self.v.queue([aid], kind='server_deploy')[0]
                future = pool.submit(claim)
                self.assertTrue(entered.wait(2))
                self.assertFalse(future.done())
                self.v.update_account(aid, deployment={'state': 'queued', 'job_id': job_id})
            job, checkpoint = future.result(timeout=3)
        self.assertEqual(job['id'], checkpoint['job_id'])

    def test_queue_retry_is_idempotent_after_running_checkpoint_changes(self):
        aid = self.add()
        self.v.queue([aid])
        self.v.claim()
        self.v.update_account(aid, status='write_unknown', write_intent={'state': 'pending'},
                              monitor={'owned_pause': {'state': 'pending'}})
        self.assertEqual(self.v.queue([aid]), [])
        self.assertEqual(len(self.v.jobs()), 1)

    def test_queued_cancellation_does_not_spend_attempt_budget(self):
        aid = self.add()
        for _ in range(5):
            self.v.queue([aid], kind='auto_reauth')
            self.v.cancel_auto(aid)
        self.assertEqual(self.v.auto_jobs_since(0), 0)
        self.assertEqual(len(self.v.queue([aid], kind='auto_reauth')), 1)
        self.assertEqual(self.v.auto_jobs_since(0), 1)

    def test_explicit_tasks_have_no_attempt_cap_and_keep_history(self):
        for kind in ('manual','server_deploy','recovery_continue'):
            aid=self.add(kind+'@example.invalid')
            for _ in range(15):
                job=self.v.queue([aid],kind=kind)[0]
                self.assertEqual(self.v.queue([aid],kind=kind),[])
                self.v.claim();self.v.finish(job,'failed','DEPLOY_READ_FAILED')
            self.assertEqual(len([j for j in self.v.jobs() if j['account_id']==aid]),15)
        self.v.update_account(aid,status='write_unknown',write_intent={'state':'unknown'})
        with self.assertRaisesRegex(VaultError,'PREVIOUS_WRITE_NEEDS_RECONCILIATION'):
            self.v.queue([aid],kind='server_deploy')

    def test_budget_uses_claim_time_for_jobs_queued_long_before_unlock(self):
        aid = self.add()
        for _ in range(2):
            with patch('sub2easy.vault.time.time', return_value=1000):
                job_id = self.v.queue([aid], kind='auto_reauth')[0]
            with patch('sub2easy.vault.time.time', return_value=10000):
                self.v.claim()
                self.v.finish(job_id, 'failed', 'INCORRECT_CODE')
        with patch('sub2easy.vault.time.time', return_value=10001):
            self.assertEqual(self.v.auto_jobs_since(9000), 2)
            with self.assertRaisesRegex(VaultError, 'REAUTH_BUDGET_2_PER_30_MIN'):
                self.v.queue([aid],kind='auto_reauth')

    def test_running_cancellation_retains_pending_slot_and_actual_result(self):
        aid, other = self.add(), self.add('other@example.invalid')
        running, queued = self.v.queue([aid, other])
        self.assertEqual(self.v.claim()['id'], running)
        result = self.v.cancel_jobs([running, queued])
        self.assertEqual(result, {'cancelled': [queued], 'cancel_requested': [running], 'already_finished': []})
        self.assertTrue(self.v.cancellation_requested(running))
        self.assertEqual(self.job(running)['state'], 'running')
        self.assertTrue(self.v.pending(aid))
        self.assertFalse(self.v.pending(other))
        with self.assertRaisesRegex(VaultError, 'JOB_RUNNING_CANNOT_LOCK'):
            self.v.lock()
        self.assertEqual(self.v.queue([aid]), [])
        self.v.finish(running, 'succeeded', 'AUTHORIZATION_READY')
        self.assertEqual(self.job(running)['state'], 'succeeded')
        self.assertEqual(self.v.cancel_jobs([running])['already_finished'], [running])
        self.v.lock()

    def test_cancellation_validation_is_all_or_nothing(self):
        aid = self.add()
        job_id = self.v.queue([aid])[0]
        with self.assertRaisesRegex(VaultError, 'JOB_NOT_FOUND'):
            self.v.cancel_jobs([job_id, 'missing'])
        self.assertEqual(self.job(job_id)['state'], 'queued')

    def test_running_cancelled_attempts_still_spend_budget(self):
        aid = self.add()
        for _ in range(2):
            job_id = self.v.queue([aid])[0]
            self.v.claim()
            self.v.cancel_jobs([job_id])
            self.v.finish(job_id, 'cancelled', 'USER_CANCELLED')
        with self.assertRaisesRegex(VaultError, 'REAUTH_BUDGET_2_PER_30_MIN'):
            self.v.queue([aid],kind='auto_reauth')

    def test_legacy_cancel_helpers_only_cancel_queued_and_filter_kind(self):
        ids = [self.add(f'audit{i}@example.invalid') for i in range(4)]
        running = self.v.queue([ids[0]], kind='auto_reauth')[0]
        self.v.claim()
        auto = self.v.queue([ids[1]], kind='recovery_continue')[0]
        manual = self.v.queue([ids[2]])[0]
        deploy = self.v.queue([ids[3]], kind='server_deploy')[0]
        self.assertIsNone(self.v.cancel_auto())
        self.assertEqual(self.job(auto)['state'], 'cancelled')
        self.assertEqual(self.job(manual)['state'], 'queued')
        self.assertEqual(self.job(deploy)['state'], 'queued')
        self.assertIsNone(self.v.cancel_queued())
        self.assertEqual(self.job(running)['state'], 'running')
        self.assertFalse(self.v.cancellation_requested(running))
        self.assertEqual(self.job(manual)['state'], 'cancelled')
        self.assertEqual(self.job(deploy)['state'], 'cancelled')

    def test_terminal_jobs_cannot_be_resurrected_or_rewritten(self):
        aid = self.add()
        job_id = self.v.queue([aid])[0]
        self.v.cancel_queued()
        before = self.job(job_id)
        self.v.finish(job_id, 'succeeded', 'LATE_SUCCESS')
        self.v.job_stage(job_id, 'complete')
        self.assertEqual(self.job(job_id), before)
        with self.assertRaisesRegex(VaultError, 'INVALID_JOB_STATE'):
            self.v.finish(job_id, 'running')

    def test_restart_retains_queued_and_quarantines_running_write_unknown(self):
        aid, other = self.add(), self.add('other@example.invalid')
        running, queued = self.v.queue([aid, other], context={'access_token': 'AUDIT_CONTEXT_SECRET'})
        self.v.claim()
        self.v.cancel_jobs([running])
        self.v.update_account(aid, status='write_unknown', write_intent={'state': 'unknown'})
        self.v.job_stage(running, 'applying')
        before = self.job(running)['updated']
        self.restart()
        job = self.job(running)
        self.assertEqual((job['state'], job['code'], job['stage']),
                         ('unknown', 'INTERRUPTED_RESULT_UNKNOWN', 'applying'))
        self.assertGreaterEqual(job['updated'], before)
        self.assertTrue(job['cancel_requested'])
        self.assertEqual(self.v.account(aid)['status'], 'write_unknown')
        self.assertEqual(self.v.claim()['id'], queued)
        self.assertIsNone(self.v.claim())

    def test_restart_does_not_rewrite_completed_jobs(self):
        aid = self.add()
        job_id = self.v.queue([aid], kind='auto_reauth')[0]
        self.v.claim()
        self.v.finish(job_id, 'failed', 'MONITOR_FETCH_FAILED', 'applying')
        before = self.job(job_id)
        self.restart()
        self.assertEqual(self.job(job_id), before)
        self.assertEqual(self.v.job_checkpoint_time(job_id, aid), before['updated'])

    def test_corrupt_context_is_quarantined_without_starving_other_jobs(self):
        first, second = self.add(), self.add('second@example.invalid')
        bad, good = self.v.queue([first, second])
        with self.v.db:
            self.v.db.execute('UPDATE jobs SET context=? WHERE id=?', (b'not-an-aead-record', bad))
        self.assertEqual(self.v.claim()['id'], good)
        self.assertEqual(self.job(bad)['state'], 'failed')
        self.assertEqual(self.job(bad)['code'], 'VAULT_DECRYPT_FAILED')
        self.assertIsNone(self.job(bad)['started'])

    def test_changed_revision_and_new_unknown_status_are_not_claimed(self):
        first, second, third = [self.add(f'claim{i}@example.invalid') for i in range(3)]
        stale, blocked, good = self.v.queue([first, second, third])
        with self.v.db:
            self.v.db.execute('UPDATE accounts SET revision=revision+1 WHERE id=?', (first,))
        self.v.update_account(second, status='unknown')
        self.assertEqual(self.v.claim()['id'], good)
        self.assertEqual(self.job(stale)['code'], 'STALE_MATERIAL')
        self.assertEqual(self.job(stale)['state'], 'cancelled')
        self.assertEqual(self.job(blocked)['code'], 'RESULT_REVIEW_REQUIRED_BEFORE_RETRY')

    def test_manual_job_rechecks_monitor_block_before_claim(self):
        aid = self.add()
        job_id = self.v.queue([aid])[0]
        self.v.update_account(aid, monitor={'blocked': True})
        self.assertIsNone(self.v.claim())
        self.assertEqual(self.job(job_id)['code'], 'MONITOR_RECOVERY_NEEDS_REVIEW')

    def test_atomic_import_failure_does_not_keep_first_account(self):
        original = self.v._seal
        count = 0

        def fail_second(purpose, value):
            nonlocal count
            if purpose.startswith('account:'):
                count += 1
                if count == 2:
                    raise RuntimeError('AUDIT_SEAL_FAILED')
            return original(purpose, value)

        with patch.object(self.v, '_seal', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'AUDIT_SEAL_FAILED'):
                self.v.import_materials(parse_batch(line() + '\n' + line('second@example.invalid')), PROFILE)
        self.restart()
        self.assertEqual(self.v.accounts(), [])

    def test_continuation_only_claim_preserves_manual_queue(self):
        first, second = self.add(), self.add('second@example.invalid')
        manual = self.v.queue([first])[0]
        deploy = self.v.queue([second], kind='server_deploy')[0]
        self.assertEqual(self.v.claim(continuation_only=True)['id'], deploy)
        self.assertIsNone(self.v.claim(continuation_only=True))
        self.assertEqual(self.v.claim()['id'], manual)

    def test_account_identity_reserved_fields_and_login_revision(self):
        aid = self.add()
        for updates in ({'id': 'another-id'}, {'revision': 100}, {'updated': 0}):
            with self.assertRaisesRegex(VaultError, 'RESERVED_ACCOUNT_FIELD'):
                self.v.update_account(aid, **updates)
        with self.assertRaisesRegex(VaultError, 'ACCOUNT_IDENTITY_IMMUTABLE'):
            self.v.update_account(aid, login={'account': 'other@example.invalid'})
        login = self.v.account(aid)['login']
        self.v.update_account(aid, login={**login, 'password': 'NEW_PASSWORD'})
        self.assertEqual(self.v.account(aid)['revision'], 2)
        self.v.queue([aid])
        with self.assertRaisesRegex(VaultError, 'ACCOUNT_BUSY'):
            self.v.update_account(aid, login=login)

    def test_unlock_during_running_work_does_not_replace_or_drop_key(self):
        aid = self.add()
        self.v.queue([aid])
        self.v.claim()
        key = self.v.key
        with self.assertRaisesRegex(VaultError, 'JOB_RUNNING_CANNOT_UNLOCK'):
            self.v.unlock(MASTER)
        self.assertIs(self.v.key, key)
        self.assertEqual(self.v.account(aid)['login']['password'], PASSWORD)

    def test_locked_mutators_fail_and_open_preserves_locked_error(self):
        aid = self.add()
        job_id = self.v.queue([aid])[0]
        self.v.lock()
        for call in (lambda: self.v.finish(job_id, 'failed'), lambda: self.v.job_stage(job_id, 'precheck'),
                     lambda: self.v.cancel_auto(), lambda: self.v.cancel_jobs(),
                     lambda: self.v._open('unused', b'')):
            with self.subTest(call=call), self.assertRaisesRegex(VaultError, '^VAULT_LOCKED$'):
                call()
        self.assertIsNone(self.v.claim())

    def test_invalid_queue_inputs_do_not_mutate_jobs(self):
        aid = self.add()
        for ids in (aid, [aid, {}], [aid, ''], None):
            with self.subTest(ids=type(ids)), self.assertRaisesRegex(VaultError, 'INVALID_ACCOUNT_IDS'):
                self.v.queue(ids)
        with self.assertRaisesRegex(VaultError, 'INVALID_JOB_CONTEXT'):
            self.v.queue([aid], context=['not-a-dict'])
        self.assertEqual(self.v.jobs(), [])

    def test_metadata_filters_secret_keys_but_internal_records_stay_complete(self):
        aid = self.add_json()
        identity = self.v.account(aid)['authorization']['identity']
        binding = {'cloud_id': 42, 'instance': PROFILE['instance_id'], 'identity': identity,
                   'access_token': 'BINDING_SECRET', 'extra': {'admin_key': 'ADMIN_SECRET', 'normal': 7}}
        profile = {**PROFILE, 'password': 'PROFILE_SECRET', 'nested': [{'Cookie': 'COOKIE_SECRET', 'normal': 8}]}
        self.v.update_account(aid, binding=binding, profile=profile,
                              result_code='Bearer AUDIT_ACCESS_TOKEN',
                              monitor={'last_code': 'AUDIT_REFRESH_TOKEN'},
                              deployment={'state': 'failed', 'code': 'HTTP failed with AUDIT_ACCESS_TOKEN'})
        public = json.dumps(self.v.accounts())
        for secret in ('BINDING_SECRET', 'ADMIN_SECRET', 'PROFILE_SECRET', 'COOKIE_SECRET',
                       'AUDIT_ACCESS_TOKEN', 'AUDIT_REFRESH_TOKEN'):
            self.assertNotIn(secret, public)
        self.assertEqual(self.v.accounts()[0]['binding']['identity'], identity)
        self.assertEqual(self.v.account(aid)['binding'], binding)
        self.assertEqual(self.v.account(aid)['profile'], profile)

    def test_metadata_redacts_known_secret_inside_display_name(self):
        aid = self.add()
        self.v.set_setting('connection', {'admin_key': 'AUDIT_ADMIN_SECRET'})
        self.v.update_account(aid, profile={**PROFILE, 'profile_id': 'name ' + PASSWORD},
                              deployment={'state': 'failed', 'code': 'AUDIT_ADMIN_SECRET'})
        public = json.dumps(self.v.accounts())
        self.assertNotIn(PASSWORD, public)
        self.assertNotIn('AUDIT_ADMIN_SECRET', public)

    def test_fixed_error_code_not_erased_by_short_synthetic_admin_key(self):
        aid = self.add()
        self.v.set_setting('connection', {'admin_key': 'ADMIN'})
        job_id = self.v.queue([aid])[0]
        self.v.finish(job_id, 'failed', 'ADMIN_AUTH_FAILED')
        self.assertEqual(self.job(job_id)['code'], 'ADMIN_AUTH_FAILED')

    def test_nullable_optional_metadata_does_not_break_account_list(self):
        aid = self.add()
        self.v.update_account(aid, monitor=None, deployment=None)
        self.assertIsNone(self.v.accounts()[0]['monitor']['state'])
        self.assertIsNone(self.v.accounts()[0]['deployment']['state'])
        self.assertEqual(len(self.v.queue([aid])), 1)

    def test_job_diagnostics_never_persist_freeform_or_known_credentials(self):
        aid = self.add()
        self.v.set_setting('connection', {'nvt_cookie': 'AUDIT_COOKIE', 'admin_key': 'AUDIT_ADMIN_KEY'})
        job_id = self.v.queue([aid], context={'access_token': 'AUDIT_CONTEXT_TOKEN'})[0]
        self.v.claim()
        self.v.job_stage(job_id, 'cookie=AUDIT_COOKIE')
        self.v.finish(job_id, 'failed', PASSWORD, 'Bearer AUDIT_CONTEXT_TOKEN')
        job = self.job(job_id)
        self.assertEqual((job['code'], job['stage']), ('REDACTED_DIAGNOSTIC', 'redacted'))
        self.assertNotIn('context', job)
        self.assertNotIn('AUDIT_CONTEXT_TOKEN', json.dumps(job))
        self.restart()
        for path in Path(self.temp.name).glob('vault.sqlite3*'):
            raw = path.read_bytes()
            for secret in (MASTER, PASSWORD, TOTP, 'audit@example.invalid', 'AUDIT_COOKIE',
                           'AUDIT_ADMIN_KEY', 'AUDIT_CONTEXT_TOKEN'):
                self.assertNotIn(secret.encode(), raw)

    def test_legacy_plaintext_diagnostics_are_redacted_in_job_list(self):
        aid = self.add()
        job_id = self.v.queue([aid])[0]
        with self.v.db:
            self.v.db.execute('UPDATE jobs SET code=?,stage=? WHERE id=?',
                              (PASSWORD, 'https://audit.invalid/?token=secret', job_id))
        self.assertEqual(self.job(job_id)['code'], 'REDACTED_DIAGNOSTIC')
        self.assertEqual(self.job(job_id)['stage'], 'redacted')


class LegacyVaultAuditTests(unittest.TestCase):
    def test_original_schema_migrates_without_decrypting_or_replaying(self):
        with tempfile.TemporaryDirectory(prefix='sub2easy-legacy-audit-') as directory:
            path = Path(directory) / 'vault.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript("""
                    CREATE TABLE kv (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                    CREATE TABLE accounts (id TEXT PRIMARY KEY, lookup TEXT NOT NULL UNIQUE,
                        revision INTEGER NOT NULL, status TEXT NOT NULL, payload BLOB NOT NULL, updated REAL NOT NULL);
                    CREATE TABLE jobs (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
                        state TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT '',
                        created REAL NOT NULL, updated REAL NOT NULL);
                    INSERT INTO accounts VALUES ('running-account','lookup1',1,'write_unknown',X'00',1);
                    INSERT INTO accounts VALUES ('queued-account','lookup2',1,'local',X'00',1);
                    INSERT INTO jobs VALUES ('running-job','running-account',1,'running','','applying',1,1);
                    INSERT INTO jobs VALUES ('queued-job','queued-account',1,'queued','','',2,2);
                """)
            db.close()
            with patch.object(Vault, '_open', side_effect=AssertionError('MIGRATION_MUST_NOT_DECRYPT')):
                vault = Vault(directory)
                try:
                    self.assertIsNone(vault.key)
                    rows = vault.db.execute('SELECT id,state,started,cancel_requested FROM jobs ORDER BY created').fetchall()
                    self.assertEqual(rows, [('running-job', 'unknown', 1.0, 0), ('queued-job', 'queued', None, 0)])
                    self.assertEqual(vault.db.execute("SELECT status FROM accounts WHERE id='running-account'").fetchone()[0],
                                     'write_unknown')
                    self.assertEqual(vault.db.execute("SELECT kind,context FROM jobs WHERE id='queued-job'").fetchone(),
                                     ('manual', None))
                finally:
                    vault.close()


if __name__ == '__main__':
    unittest.main()
