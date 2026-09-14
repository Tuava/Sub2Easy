"""Bounded job dispatch. Account reservations live through the entire worker unwind."""

from concurrent.futures import ThreadPoolExecutor
import threading

from sub2easy.vault import VaultError


DEFAULT_TASK_POOL = {'max_workers': 3, 'max_authorizations': 2, 'paused': False}


class TaskPool:
    def __init__(self, service):
        self.service, self.vault = service, service.vault
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='sub2easy-job')
        self.mutex = threading.RLock()
        self.active = {}
        self.authorizing = 0
        self.last_error = ''

    def config(self):
        return {**DEFAULT_TASK_POOL, **self.vault.get_setting('task_pool', {})}

    def save(self, data):
        with self.vault.transaction():
            cfg = self.config()
            for key in DEFAULT_TASK_POOL:
                if key in data: cfg[key] = data[key]
            for key in ('max_workers', 'max_authorizations'):
                if type(cfg[key]) is not int or not 1 <= cfg[key] <= 8:
                    raise VaultError('INVALID_TASK_CONCURRENCY')
            if type(cfg['paused']) is not bool:
                raise VaultError('INVALID_TASK_CONCURRENCY')
            self.vault.set_setting('task_pool', cfg)
        return self.view()

    def view(self):
        cfg = self.config()
        counts = self.vault.job_counts()
        with self.mutex:
            return {'config': cfg, 'runtime': {
                'active': len(self.active), 'authorizing': self.authorizing,
                'queued': counts.get('queued', 0), 'running': counts.get('running', 0),
                'last_error': self.last_error, 'stopping': self.service.stop.is_set(),
            }}

    @staticmethod
    def keys(account):
        keys = {('local', account['id']), ('login', account['login']['account'])}
        binding = account.get('binding')
        if binding:
            keys.add(('cloud', binding['instance'], binding['cloud_id']))
        return keys

    def busy(self, account):
        keys = self.keys(account)
        with self.mutex:
            return any(keys & value for value in self.active.values())

    def reserve_binding(self, job_id, binding):
        """Add a discovered cloud ID to the running job without racing another owner."""
        key=('cloud',binding['instance'],binding['cloud_id'])
        with self.mutex:
            if any(key in keys for jid,keys in self.active.items() if jid!=job_id):
                raise VaultError('OPERATION_RUNNING')
            # run_one/dispatch establish reservations. Do not invent an untracked owner.
            if job_id not in self.active:raise VaultError('OPERATION_RUNNING')
            self.active[job_id].add(key)

    def claim(self):
        # Same short coordinator as foreground queue/bind operations; never held
        # by workers across HTTP. Do not claim ahead of available capacity.
        if not self.service.operation.acquire(blocking=False): return None
        try:
            if self.service.stop.is_set() or self.vault.key is None: return None
            cfg = self.config()
            with self.mutex:
                if cfg['paused'] or len(self.active) >= cfg['max_workers']: return None
            settings = self.vault.get_setting('connection', {})
            continuation_only = not settings.get('nvt_cookie') or settings.get('connector_paused', False)
            job = self.vault.claim(continuation_only=continuation_only,
                                   eligible=lambda a: not self.busy(a))
            if not job: return None
            keys = self.keys(self.vault.account(job['account_id']))
            with self.mutex:
                self.active[job['id']] = keys
            return job, settings
        finally:
            self.service.operation.release()

    def execute(self, job, settings):
        try:
            self.service.run_job(job, settings)
        except Exception:
            # Unexpected worker failures must not silently disappear in a Future.
            with self.mutex: self.last_error = 'TASK_WORKER_FAILED'
            self.vault.update_account(job['account_id'], status='unknown', result_code='TASK_WORKER_FAILED')
            self.vault.finish(job['id'], 'unknown', 'TASK_WORKER_FAILED')
        finally:
            with self.mutex: self.active.pop(job['id'], None)

    def dispatch(self):
        for _ in range(8):
            claimed = self.claim()
            if claimed is None: break
            job, settings = claimed
            try:
                self.executor.submit(self.execute, job, settings)
            except Exception:
                self.vault.finish(job['id'], 'failed', 'TASK_DISPATCH_FAILED')
                with self.mutex:
                    self.active.pop(job['id'], None)
                    self.last_error = 'TASK_DISPATCH_FAILED'
                break

    def close(self):
        # Never cancel a claimed job's Future: every reservation must unwind.
        self.executor.shutdown(wait=True)
