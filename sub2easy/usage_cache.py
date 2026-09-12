"""Small async UI cache for passive account usage. Never stores credentials."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from sub2easy.account_usage import collect_usage, UsageError
from sub2easy.preflight import Client


class ReadClient:
    """Use a separate urllib opener per request; collect_usage bounds parallelism."""
    def __init__(self, url, key):
        self.url, self.key = url, key
        self.deadline = time.monotonic() + 120

    def get(self, path, params=None):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise UsageError('USAGE_COLLECTION_TIMEOUT')
        return Client(self.url, self.key, timeout=min(5,remaining/3)).get(path, params)


class UsageCache:
    def __init__(self, collector=collect_usage, client_factory=ReadClient, clock=time.time):
        self.collector, self.client_factory, self.clock = collector, client_factory, clock
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='usage-ui')
        self.future = None
        self.scope = None
        self.epoch = 0
        self.rows = {}
        self.pending_ids = []
        self.last_error = None
        self.active_epoch = None

    def invalidate(self):
        with self.lock:
            self.scope = None; self.epoch += 1; self.rows.clear(); self.last_error = None
            # Running reads cannot be killed; epoch check prevents resurrection.
            if self.future and self.future.cancel():self.future = None;self.pending_ids=[]

    def _scope(self, scope):
        if scope != self.scope:
            self.epoch += 1; self.rows.clear(); self.last_error = None; self.scope = scope

    @staticmethod
    def ids(values):
        if (not isinstance(values, list) or len(values)>50
                or any(type(v) is not int or not 0<v<=2**53-1 for v in values)):
            raise UsageError('USAGE_INVALID_ACCOUNT_IDS')
        return list(dict.fromkeys(values))

    def _harvest(self):
        if self.future and self.future.done():
            future=self.future;self.future=None
            try:
                epoch,result=future.result()
                if epoch==self.epoch:
                    now=self.clock()
                    for row in result['accounts']:
                        self.rows[row['account_id']]={'row':row,'at':now}
                    if len(self.rows)>2000:
                        oldest=sorted(self.rows,key=lambda aid:self.rows[aid]['at'])
                        for aid in oldest[:len(self.rows)-2000]:self.rows.pop(aid,None)
                    self.last_error=None
            except Exception:
                if self.active_epoch==self.epoch:self.last_error='USAGE_COLLECTION_FAILED'
            self.pending_ids=[]

    def _snapshot(self, ids):
        return {'accounts':[self.rows[i]['row'] for i in ids if i in self.rows],
                'pending': bool(self.future), 'pending_ids':list(self.pending_ids) if self.active_epoch==self.epoch else [],
                'error':self.last_error,'ttl_seconds':60,'mode':'read_only'}

    def snapshot(self, scope, ids):
        ids=self.ids(ids)
        with self.lock:
            self._scope(scope);self._harvest();return self._snapshot(ids)

    def request(self, scope, url, key, ids):
        ids=self.ids(ids)
        with self.lock:
            self._scope(scope);self._harvest()
            wanted=[i for i in ids if i not in self.rows or self.clock()-self.rows[i]['at']>=60]
            if wanted and not self.future:
                epoch=self.epoch;client=self.client_factory(url,key)
                def collect():return epoch,self.collector(client,wanted,max_workers=4)
                self.pending_ids=wanted;self.active_epoch=epoch;self.future=self.executor.submit(collect)
            return self._snapshot(ids)

    def close(self):
        self.invalidate();self.executor.shutdown(wait=True,cancel_futures=True)
