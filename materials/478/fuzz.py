"""Randomized differential fuzzing: PageWriter against an in-memory map,
with every produced page independently re-parsed and re-read from a file."""
import os
import random
import string
import tempfile

from storage.writer import PAGE_FULL, PageWriter
from query.reader import PageReader
from verify import wire_parse, wire_save


def rand_key(rng):
    # Mix ASCII prefixes so prefix compression actually varies, plus occasional
    # multi-byte UTF-8 and boundary-length keys.
    kind = rng.random()
    if kind < 0.8:
        return ('k%03d-' % rng.randrange(140) +
                ''.join(rng.choice(string.ascii_lowercase)
                        for _ in range(rng.randrange(0, 4)))).encode()
    if kind < 0.95:
        return ('键%02d' % rng.randrange(60)).encode()
    return (u'x' * rng.randrange(1, 65)).encode()


def rand_value(rng):
    return os.urandom(rng.randrange(0, 40))


def run():
    writer = PageWriter()
    model = {}
    raw = writer.create([], restart=rng.choice([1, 2, 4, 8]))
    assert raw != PAGE_FULL
    tmpdir = tempfile.mkdtemp(prefix='pcr2_fuzz_')
    steps = 0
    full_seen = 0
    for step in range(4000):
        steps += 1
        op = rng.random()
        key = rand_key(rng)
        if op < 0.55:  # put
            value = rand_value(rng)
            outcome = writer.put(raw, key, value)
            if outcome == PAGE_FULL:
                full_seen += 1
                # Model unchanged; page bytes still parse and match model.
                _d, _s, rows = wire_parse(raw)
                assert dict(rows) == model
                continue
            raw = outcome
            model[key] = value
        elif op < 0.75:  # delete
            outcome = None
            try:
                outcome = writer.delete(raw, key)
            except KeyError:
                assert key not in model
                continue
            if outcome == PAGE_FULL:
                full_seen += 1
                continue
            raw = outcome
            assert key in model
            del model[key]
        else:  # rename
            new_key = rand_key(rng)
            try:
                outcome = writer.rename(raw, key, new_key)
            except KeyError:
                assert key not in model or new_key in model or key == new_key
                continue
            if outcome == PAGE_FULL:
                full_seen += 1
                continue
            raw = outcome
            model[new_key] = model.pop(key)

        # Every step: independent wire parse + file reopen + full reads.
        _d, per_segment, all_rows = wire_parse(raw)
        assert dict(all_rows) == model
        assert [k for k, _ in all_rows] == sorted(model)
        for segment in per_segment:
            assert 1 <= len(segment) <= 8
        path = os.path.join(tmpdir, 'page.pcr2')
        wire_save(raw, path)
        reader = PageReader.open(path)
        # Point reads for every key (each touches <= one segment).
        for k, v in model.items():
            got = reader.get(k)
            assert got == v, (k, got, v)
        assert reader.get(b'\xff' * 3) is None
        # Full-range and random sub-range scans.
        assert dict(reader.range(b'\x00', b'\xff' * 4)) == model
        keys = sorted(model)
        if len(keys) >= 2:
            a, b = rng.sample(range(len(keys)), 2)
            if a > b:
                a, b = b, a
            lo, hi = keys[a], keys[b]
            expect = {k: v for k, v in model.items() if lo <= k < hi}
            r2 = PageReader.open(path)
            assert dict(r2.range(lo, hi)) == expect
        # Full audit.
        assert dict(PageReader.open(path).audit()) == model
        assert len(raw) <= 4096
    print(f'fuzz OK: {steps} steps, {full_seen} PAGE_FULL rejections, '
          f'{len(model)} live keys, page {len(raw)} bytes')


if __name__ == '__main__':
    rng = random.Random(20261116)
    run()
