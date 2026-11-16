import json
import tempfile
import threading
from pathlib import Path
from device import Zones
from index import VersionIndex
from reclamation import Cleaner
from store import Store


def setup(root):
    zones = Zones(root, 4, 64)
    index = VersionIndex()
    cleaner = Cleaner(zones, index)
    return zones, index, cleaner, Store(zones, index, cleaner)


def main():
    with tempfile.TemporaryDirectory() as td:
        zones, index, cleaner, store = setup(Path(td))
        store.put('a', b'A' * 16)
        cleaner.clean(0)
        print(json.dumps({'scenario': 'normal-clean', 'actual': store.get('a').decode(),
                          'expected': 'A' * 16, 'events': zones.events}))
    with tempfile.TemporaryDirectory() as td:
        zones, index, cleaner, store = setup(Path(td))
        store.put('a', b'A' * 16)
        zones.append(1, b'G' * 64)
        try:
            cleaner.clean(0)
            outcome = 'completed'
        except Exception as exc:
            outcome = type(exc).__name__
        print(json.dumps({'scenario': 'cleaner-neighbor-full-other-free', 'actual': outcome,
                          'expected': 'completed', 'free_zone_2_bytes': zones.capacity - zones.wp[2],
                          'actual_bytes': store.get('a').decode()}))
    with tempfile.TemporaryDirectory() as td:
        zones, index, cleaner, store = setup(Path(td))
        old_location = zones.append(0, b'A' * 16)
        old_version = index.publish('a', old_location)
        new_location = zones.append(2, b'N' * 16)
        new_version = index.publish('a', new_location)
        copied = zones.append(1, b'A' * 16)
        accepted = index.relocate('a', old_version, old_location, copied)
        actual_version, actual_location = index.get('a')
        print(json.dumps({'scenario': 'standalone-index-stale-relocate', 'accepted': accepted,
                          'actual_version': actual_version, 'expected_version': new_version,
                          'actual_bytes': zones.read(actual_location).decode(), 'expected_bytes': 'N' * 16}))
    with tempfile.TemporaryDirectory() as td:
        zones, index, cleaner, store = setup(Path(td))
        store.put('a', b'A' * 16)
        handle = index.pin('a')
        cleaner.clean(0)
        print(json.dumps({'scenario': 'pinned-location-after-clean',
                          'actual': zones.read(handle.location).decode(), 'expected': 'A' * 16,
                          'active_handles': index.audit()['handles'], 'events': zones.events}))
        index.release(handle)
    with tempfile.TemporaryDirectory() as td:
        zones, index, cleaner, store = setup(Path(td))
        store.put('a', b'A' * 16)
        copied, resume = threading.Event(), threading.Event()
        outcome = {}
        def after_copy(*args):
            copied.set()
            if not resume.wait(3):
                raise TimeoutError('probe gate')
        def work():
            try:
                outcome['clean'] = cleaner.clean(0, after_copy=after_copy)
            except Exception as exc:
                outcome['error'] = repr(exc)
        thread = threading.Thread(target=work)
        thread.start()
        try:
            if not copied.wait(3):
                raise TimeoutError('copy not reached')
            new_location = zones.append(2, b'N' * 16)
            new_version = index.publish('a', new_location)
        finally:
            resume.set()
            thread.join(4)
        print(json.dumps({'scenario': 'cleaner-copy-then-new-publish', 'thread_stopped': not thread.is_alive(),
                          'actual': store.get('a').decode(), 'expected': 'N' * 16,
                          'expected_version': new_version, 'actual_version': index.get('a')[0],
                          'cleaner': outcome, 'events': zones.events}))


if __name__ == '__main__':
    main()
