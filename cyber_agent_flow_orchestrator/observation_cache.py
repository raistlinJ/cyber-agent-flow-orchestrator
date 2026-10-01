"""Short-lived, owner-scoped caches for read-only VM observations."""
from copy import deepcopy
import threading
import time


class ObservationCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, key, ttl, fetch, *, fresh=False):
        with self.lock:
            saved = self.entries.get(key)
            if not fresh and saved and self.clock() - saved[0] < ttl:
                return deepcopy(saved[1]), True
        value = fetch()  # Failed observations are never cached.
        with self.lock:
            self.entries[key] = (self.clock(), deepcopy(value))
            if len(self.entries) > 128:
                oldest = min(self.entries, key=lambda k: self.entries[k][0])
                del self.entries[oldest]
        return deepcopy(value), False

    def invalidate_guest(self, vmid):
        with self.lock:
            for key in list(self.entries):
                if key[:2] == ('guest', vmid):
                    del self.entries[key]


class CachedProbe:
    def __init__(self, probe, cache, *, fresh=False):
        self.probe, self.cache, self.fresh = probe, cache, fresh

    def vm(self, vmid):
        def fetch():
            value = self.probe.vm(vmid)
            # Power observations are cheaper and expire after five seconds.
            # An offline/paused machine must not resurrect older guest data.
            if value.get('power') != 'running' or value.get('qmp_status') in ('paused', 'suspended', 'prelaunch', 'stopped'):
                self.cache.invalidate_guest(vmid)
            return value
        value, cached = self.cache.get(('power', vmid), 5, fetch, fresh=self.fresh)
        value['power_cached'] = cached
        return value

    def guest(self, vmid, definition, units):
        from .monitor import now
        key = ('guest', vmid, definition['role'], definition.get('root'), definition.get('unit'), tuple(sorted(units)))
        active = any(unit != definition.get('unit') for unit in units)
        ttl = 5 if active else 60
        def fetch():
            return dict(self.probe.guest(vmid, definition, units), guest_observed_at=now())
        value, cached = self.cache.get(key, ttl, fetch, fresh=self.fresh)
        value.update(guest_cached=cached, guest_cache_seconds=ttl)
        return value
