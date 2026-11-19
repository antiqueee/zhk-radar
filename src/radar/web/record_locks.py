"""Allow unrelated cards to run concurrently without racing on the same card."""

from pathlib import Path
from threading import Lock
from weakref import WeakValueDictionary


class RecordLocks:
    def __init__(self):
        self._guard = Lock()
        self._locks = WeakValueDictionary()

    def for_record(self, store, ident):
        workspace = getattr(store, "path", None) or store.root
        key = (Path(workspace).resolve(), ident)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                self._locks[key] = lock = Lock()
            return lock


REPORT_LOCKS = RecordLocks()
