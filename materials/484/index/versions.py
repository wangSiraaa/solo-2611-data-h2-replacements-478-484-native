from dataclasses import dataclass
import threading


@dataclass
class ReadHandle:
    token: int
    key: str
    version: int
    location: tuple


class VersionIndex:
    def __init__(self):
        self.rows = {}
        self.lock = threading.RLock()
        self.version = 0
        self.next_token = 0
        self.handles = {}

    def publish(self, key, location):
        with self.lock:
            self.version += 1
            self.rows[key] = (self.version, tuple(location))
            return self.version

    def get(self, key):
        with self.lock:
            return self.rows[key]

    def pin(self, key):
        with self.lock:
            version, location = self.rows[key]
            self.next_token += 1
            handle = ReadHandle(self.next_token, key, version, location)
            self.handles[handle.token] = handle
            return handle

    def release(self, handle):
        with self.lock:
            if self.handles.pop(handle.token, None) is None:
                raise ValueError('invalid handle')

    def relocate(self, key, expected_version, expected_location, new_location):
        with self.lock:
            self.rows[key] = (expected_version, tuple(new_location))
            return True

    def references(self, zone):
        with self.lock:
            return sum(location[0] == zone for _, location in self.rows.values())

    def entries(self):
        with self.lock:
            return list(self.rows.items())

    def audit(self):
        with self.lock:
            return {'entries': self.entries(), 'handles': [vars(h).copy() for h in self.handles.values()]}
