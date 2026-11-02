"""Shared host-file progress feeds. No guest calls; slow viewers keep only latest state."""
from contextlib import contextmanager
from pathlib import Path
import json
import threading
import time


AUTH_REFRESH_SECONDS = 15
HEARTBEAT_SECONDS = 10
WAIT_SECONDS = 1

ACTIVE = {'queued', 'preparing', 'evaluating', 'running', 'stopping'}
TERMINAL = {'completed', 'completed_with_errors', 'failed', 'cancelled', 'interrupted', 'ready'}


def active(value):
    state = value.get('recorded_status')
    return state not in TERMINAL and bool(state in ACTIVE or value.get('coordinator_active') or (value.get('sample_progress') or {}).get('active'))


def changes(old, new, path=()):
    """Exact tree delta; list growth appends, streamed text replaces just its field."""
    if type(old) is not type(new):
        return [{'op': 'set', 'path': path, 'value': new}]
    if isinstance(new, dict):
        result = [{'op': 'remove', 'path': (*path, key)} for key in old.keys() - new.keys()]
        for key, value in new.items():
            result.extend(changes(old[key], value, (*path, key)) if key in old else [{'op': 'set', 'path': (*path, key), 'value': value}])
        return result
    if isinstance(new, list):
        if len(new) == len(old):
            return [change for index, value in enumerate(new) for change in changes(old[index], value, (*path, index))]
        if len(new) > len(old) and new[:len(old)] == old:
            return [{'op': 'append', 'path': path, 'value': new[len(old):]}]
    return [] if old == new else [{'op': 'set', 'path': path, 'value': new}]


def frame(kind, value, revision=None):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return ((f'id: {revision}\n' if revision is not None else '') + f'event: {kind}\ndata: {data}\n\n').encode()


def state_frame(previous, value, before, revision):
    snapshot = frame('snapshot', {'revision': revision, 'value': value}, revision)
    if previous is None:
        return snapshot
    delta = frame('update', {'base': before, 'revision': revision, 'changes': changes(previous, value)}, revision)
    return delta if len(delta) < len(snapshot) else snapshot


def signature(root):
    """Stat only telemetry files/directories, excluding artifacts and symlink trees."""
    root = Path(root)
    found = []

    def stamp(path):
        try:
            if not path.resolve().is_relative_to(root): return
            info = path.lstat()
            found.append((str(path.relative_to(root)), info.st_ino, info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            pass

    def children(path):
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_dir():
            return []
        stamp(path)
        return sorted(path.iterdir())

    for name in ('workflow.json', '.workflow.lock', 'stop-request.json', 'judge-preflight-events.jsonl'):
        stamp(root / name)
    for log in children(root / 'logs'):
        if log.suffix == '.log': stamp(log)
    for name in ('evaluation/manifest.json', 'suite/participant/tasks.json'):
        stamp(root / name)
    for trial in children(root / 'evaluation/trials'):
        for attempt in children(trial):
            if not attempt.name.startswith('attempt-'): continue
            for path in children(attempt):
                if path.suffix in ('.json', '.jsonl'): stamp(path)
            stamp(attempt / 'guest-output/events.jsonl')
            for review in children(attempt / 'progress-checks')[-8:]:
                if not review.is_symlink(): stamp(review / 'judge-events.jsonl')
    return tuple(found)


class StreamUnavailable(ValueError):
    pass


class Feed:
    def __init__(self, read, stamp, *, interval=.5, refresh=5):
        self.read, self.stamp = read, stamp
        self.interval, self.refresh = interval, refresh
        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.value, self.error, self.revision, self.clients = None, None, 0, 0
        self.thread = threading.Thread(target=self._run, daemon=True, name='run-progress-stream')

    def _run(self):
        previous, checked = object(), 0
        while not self.stop.is_set():
            try:
                stamp = self.stamp()
                now = time.monotonic()
                if stamp != previous or now - checked >= self.refresh:
                    value = self.read()
                    with self.condition:
                        if value != self.value:
                            self.value, self.revision = value, self.revision + 1
                            self.condition.notify_all()
                    previous, checked = stamp, now
                    if not active(value):
                        return
            except Exception:
                with self.condition:
                    self.error = 'Live updates unavailable; refresh or retry.'
                    self.condition.notify_all()
                return
            self.stop.wait(self.interval)

    def wait(self, revision, timeout=1):
        with self.condition:
            self.condition.wait_for(lambda: self.revision != revision or self.error or self.stop.is_set(), timeout)
            if self.error:
                raise StreamUnavailable(self.error)
            return self.revision, self.value

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()


class RunStreams:
    def __init__(self, *, interval=.5, refresh=5, max_feeds=16, max_clients=32):
        self.interval, self.refresh = interval, refresh
        self.max_feeds, self.max_clients = max_feeds, max_clients
        self.lock, self.feeds, self.clients, self.closed = threading.Lock(), {}, 0, False

    @contextmanager
    def subscribe(self, key, read, stamp):
        with self.lock:
            if self.closed or self.clients >= self.max_clients or (key not in self.feeds and len(self.feeds) >= self.max_feeds):
                raise StreamUnavailable('Live update capacity reached; polling remains available.')
            feed = self.feeds.get(key)
            if feed is None or feed.stop.is_set():
                feed = self.feeds[key] = Feed(read, stamp, interval=self.interval, refresh=self.refresh)
                feed.thread.start()
            feed.clients += 1
            self.clients += 1
        try:
            yield feed
        finally:
            with self.lock:
                feed.clients -= 1
                self.clients -= 1
                if not feed.clients:
                    feed.close()
                    if self.feeds.get(key) is feed:
                        del self.feeds[key]

    def close(self):
        with self.lock:
            self.closed = True
            for feed in self.feeds.values(): feed.close()
