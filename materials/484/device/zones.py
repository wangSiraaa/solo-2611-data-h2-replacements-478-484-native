from pathlib import Path
import threading


class Zones:
    def __init__(self, root, count=4, capacity=64):
        self.root = Path(root)
        self.root.mkdir(exist_ok=True)
        self.count = count
        self.capacity = capacity
        self.wp = [0] * count
        self.lock = threading.RLock()
        self.events = []
        for zone in range(count):
            (self.root / f'z{zone}').write_bytes(b'')

    def append(self, zone, data):
        with self.lock:
            if self.wp[zone] + len(data) > self.capacity:
                raise BufferError(zone)
            position = self.wp[zone]
            with open(self.root / f'z{zone}', 'ab', buffering=0) as stream:
                stream.write(data)
            self.wp[zone] += len(data)
            self.events.append(('append', zone, position, len(data)))
            return zone, position, len(data)

    def read(self, location):
        zone, position, length = location
        with open(self.root / f'z{zone}', 'rb') as stream:
            stream.seek(position)
            data = stream.read(length)
        self.events.append(('read', zone, position, length))
        return data

    def reset(self, zone):
        with self.lock:
            (self.root / f'z{zone}').write_bytes(b'')
            self.wp[zone] = 0
            self.events.append(('reset', zone))
