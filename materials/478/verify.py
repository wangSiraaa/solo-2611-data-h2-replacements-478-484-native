"""End-to-end, re-runnable verification of the PCR2 page contract.

Two independent worlds are kept apart on purpose:

* ``wire`` assembles and parses PCR2 bytes directly from the format spec
  (magic / uint32 directory length / JSON directory / contiguous payload),
  never calling PageWriter or PageReader. It is the external oracle.
* PageWriter / PageReader are the production APIs under test.

Every scenario saves its real page bytes under artifacts/pages/ and the full
metrics ledger is written to artifacts/metrics.json.
"""
import json
import os
import struct
import zlib

from storage.format import MAGIC, RECORD, encode_group, split_page
from storage.writer import PAGE_FULL, PageWriter
from query.reader import CorruptPage, PageReader

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, 'artifacts')
PAGES = os.path.join(ART, 'pages')
os.makedirs(PAGES, exist_ok=True)


# --------------------------------------------------------------------------
# Independent wire-only producer / consumer (the external oracle).
# --------------------------------------------------------------------------
def wire_build(groups):
    """groups: list of ordered row lists. Emits a fully spec-compliant page
    with prefix compression, per-segment CRC32 and contiguous offsets."""
    directory = []
    payload = bytearray()
    for rows in groups:
        blob = encode_group(rows)
        directory.append(dict(
            first=rows[0][0].hex(),
            offset=len(payload),
            length=len(blob),
            count=len(rows),
            crc32=zlib.crc32(blob),
        ))
        payload += blob
    header = json.dumps(directory, sort_keys=True,
                        separators=(',', ':')).encode('utf-8')
    return MAGIC + struct.pack('>I', len(header)) + header + bytes(payload)


def wire_parse(raw):
    """Independent structural parse + per-segment verification, returning
    (directory, decoded_rows_per_segment). Mirrors the reader contract."""
    assert raw[:4] == MAGIC, 'BAD_MAGIC'
    n = struct.unpack('>I', raw[4:8])[0]
    directory = json.loads(raw[8:8 + n].decode('utf-8'))
    payload = raw[8 + n:]
    cursor = 0
    all_rows = []
    per_segment = []
    prev_first = None
    for index, entry in enumerate(directory):
        first = bytes.fromhex(entry['first'])
        assert prev_first is None or first > prev_first, 'FIRST_ORDER'
        assert entry['offset'] == cursor, 'SPAN_CONTIGUITY'
        blob = payload[entry['offset']:entry['offset'] + entry['length']]
        assert len(blob) == entry['length'], 'SPAN_RANGE'
        assert zlib.crc32(blob) == entry['crc32'], 'CRC'
        rows = []
        at = 0
        previous = b''
        while at < len(blob):
            prefix, slen, vlen = RECORD.unpack_from(blob, at)
            at += RECORD.size
            suffix = blob[at:at + slen]
            at += slen
            value = blob[at:at + vlen]
            at += vlen
            assert (not rows and prefix == 0) or prefix <= len(previous), 'PREFIX'
            key = previous[:prefix] + suffix
            assert 1 <= len(key) <= 64, 'KEY_LEN'
            key.decode('utf-8')
            assert len(value) <= 512, 'VALUE_LEN'
            assert not rows or key > rows[-1][0], 'KEY_ORDER'
            rows.append((key, value))
            previous = key
        assert at == len(blob) and rows, 'BOUNDARY'
        assert len(rows) == entry['count'], 'COUNT'
        assert rows[0][0] == first, 'FIRST'
        if index + 1 < len(directory):
            assert rows[-1][0] < bytes.fromhex(directory[index + 1]['first']), 'BOUNDARY_NEXT'
        cursor = entry['offset'] + entry['length']
        per_segment.append(rows)
        all_rows.extend(rows)
    assert cursor == len(payload), 'COVERAGE'
    return directory, per_segment, all_rows


def wire_save(raw, name):
    path = os.path.join(PAGES, name)
    with open(path, 'wb') as fh:
        fh.write(raw)
    return path


def fresh_reader(path):
    """Reopen a saved file through the production reader, as an independent run."""
    return PageReader.open(path)


results = []


def _json_safe(obj):
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def record(name, ok, detail):
    detail = _json_safe(detail)
    results.append(dict(scenario=name, ok=bool(ok), **detail))
    print(('PASS' if ok else 'FAIL'), name, json.dumps(detail, ensure_ascii=False))


# --------------------------------------------------------------------------
# 1. Independent producer -> production reader (no PageWriter involved).
# --------------------------------------------------------------------------
def scenario_independent_basic():
    groups = [
        [(b'aa0', b'0'), (b'aa1', b'1')],
        [(b'bb0', b'2'), (b'bb1', b'3')],
    ]
    raw = wire_build(groups)
    path = wire_save(raw, '01_independent_basic.pcr2')
    reader = fresh_reader(path)
    got = reader.get(b'aa1')
    rng = reader.range(b'aa1', b'bb9')
    full = reader.range(b'', b'zzz')
    empty = reader.range(b'bb9', b'bb9')
    missing = reader.get(b'zz0')
    ok = (got == b'1'
          and [k for k, _ in rng] == [b'aa1', b'bb0', b'bb1']
          and len(full) == 4 and empty == [] and missing is None)
    record('independent_basic', ok, dict(
        get=got.decode(), range_keys=[k.decode() for k, _ in rng],
        empty_range=empty, missing=missing, inspect=reader.inspect()))


def scenario_non_ascii_and_empty_value():
    # Groups must be ordered by raw bytes: 'plain' (0x70) precedes the CJK
    # keys (0xE9..) which precede the 4-byte emoji key (0xF0..).
    groups = [
        [(b'plain', b'q')],
        [('键α'.encode(), b''), ('键β'.encode(), b'v'), ('🔑x'.encode(), b'z')],
    ]
    raw = wire_build(groups)
    path = wire_save(raw, '02_non_ascii_empty_value.pcr2')
    reader = fresh_reader(path)
    audit_rows = reader.audit()
    r2 = fresh_reader(path)
    expected = [r for g in groups for r in g]
    ok = (r2.get('键α'.encode()) == b''
          and r2.get('🔑x'.encode()) == b'z'
          and [k for k, _ in audit_rows] == [r[0] for r in expected])
    record('non_ascii_and_empty_value', ok, dict(
        rows=[k.decode('utf-8', 'surrogatepass') for k, _ in audit_rows],
        empty_value=r2.get('键α'.encode()), inspect=r2.inspect()))


def scenario_prefix_compression_cross_group():
    # Long shared prefixes; the second group must not inherit the first's key.
    groups = [
        [(b'prefix-common-0001', b'a'), (b'prefix-common-0002', b'b')],
        [(b'prefix-common-0003', b'c'), (b'prefix-common-0010', b'd')],
    ]
    raw = wire_build(groups)
    path = wire_save(raw, '03_prefix_cross_group.pcr2')
    reader = fresh_reader(path)
    rng = reader.range(b'prefix-common-0002', b'prefix-common-0010')
    ok = [k for k, _ in rng] == [b'prefix-common-0002', b'prefix-common-0003']
    record('prefix_compression_cross_group', ok,
           dict(keys=[k.decode() for k, _ in rng], inspect=reader.inspect()))


# --------------------------------------------------------------------------
# 2. Production writer -> independent oracle parse, plus reader round trip.
# --------------------------------------------------------------------------
def scenario_writer_roundtrip_and_growth():
    writer = PageWriter()
    rows = [(b'aa0', b'0'), (b'aa1', b'1'), (b'bb0', b'2'), (b'bb1', b'3')]
    raw = writer.create(rows, restart=2)
    changed = writer.put(raw, b'aa0', b'longer value')
    path = wire_save(changed, '04_writer_growth.pcr2')
    # Independent oracle fully re-parses the writer output.
    directory, per_segment, all_rows = wire_parse(changed)
    reader = fresh_reader(path)
    ok = (dict(all_rows) == {b'aa0': b'longer value', b'aa1': b'1',
                             b'bb0': b'2', b'bb1': b'3'}
          and reader.get(b'bb0') == b'2'
          and [k for k, _ in reader.range(b'aa0', b'zz')] ==
          [b'aa0', b'aa1', b'bb0', b'bb1'])
    record('writer_roundtrip_and_growth', ok,
           dict(writer=writer.report, directory=directory))


def scenario_segment_head_and_tail_edits():
    writer = PageWriter()
    raw = writer.create([(f'k{i:02}'.encode(), b'v') for i in range(16)], restart=4)
    # Head of first segment and tail of last segment.
    raw = writer.put(raw, b'k00', b'HEAD')
    r1 = dict(writer.report)
    raw = writer.put(raw, b'k15', b'TAIL')
    r2 = dict(writer.report)
    path = wire_save(raw, '05_head_tail_edits.pcr2')
    _d, _s, all_rows = wire_parse(raw)
    data = dict(all_rows)
    ok = data[b'k00'] == b'HEAD' and data[b'k15'] == b'TAIL' and len(data) == 16
    record('segment_head_and_tail_edits', ok,
           dict(head_edit=r1, tail_edit=r2))


def scenario_boundary_insert_split():
    writer = PageWriter()
    # Two full segments of 8; insert at the boundary forces a split.
    keys = [f'g{i:02}'.encode() for i in range(16)]
    raw = writer.create([(k, b'x') for k in keys], restart=8)
    raw = writer.put(raw, b'g075', b'new')  # inside first segment -> 9 -> split
    path = wire_save(raw, '06_boundary_insert_split.pcr2')
    _d, per_segment, all_rows = wire_parse(raw)
    sizes = [len(g) for g in per_segment]
    reader = fresh_reader(path)
    ok = (len(all_rows) == 17 and max(sizes) <= 8
          and reader.get(b'g075') == b'new'
          and [k for k, _ in all_rows] == sorted(k for k, _ in all_rows))
    record('boundary_insert_split', ok,
           dict(segment_sizes=sizes, writer=writer.report,
                range_keys=[k.decode() for k, _ in reader.range(b'g07', b'g08')]))


def scenario_delete_and_rename():
    writer = PageWriter()
    rows = [(b'a1', b'keep'), (b'a2', b'drop'), (b'b1', b'move'), (b'c1', b'x')]
    raw = writer.create(rows, restart=2)
    raw = writer.delete(raw, b'a2')
    after_delete = dict(writer.report)
    raw = writer.rename(raw, b'b1', b'a9')  # crosses segment boundary
    path = wire_save(raw, '07_delete_rename.pcr2')
    _d, _s, all_rows = wire_parse(raw)
    data = dict(all_rows)
    # Error contracts.
    err_delete = err_rename_old = err_rename_new = None
    try:
        writer.delete(raw, b'ghost')
    except KeyError as exc:
        err_delete = 'KeyError'
    try:
        writer.rename(raw, b'ghost', b'z9')
    except KeyError:
        err_rename_old = 'KeyError'
    try:
        writer.rename(raw, b'c1', b'a9')
    except KeyError:
        err_rename_new = 'KeyError'
    ok = (data == {b'a1': b'keep', b'a9': b'move', b'c1': b'x'}
          and err_delete and err_rename_old and err_rename_new)
    record('delete_and_rename', ok, dict(
        after_delete=after_delete, rename=writer.report,
        errors=[err_delete, err_rename_old, err_rename_new]))


def scenario_rename_shortens_common_prefix():
    writer = PageWriter()
    rows = [(b'longprefix-1', b'p'), (b'longprefix-2', b'q'), (b'longprefix-3', b'r')]
    raw = writer.create(rows, restart=3)
    # New key shares a much shorter prefix with its neighbour.
    raw = writer.rename(raw, b'longprefix-2', b'lo-Z')
    path = wire_save(raw, '08_rename_shorter_prefix.pcr2')
    _d, _s, all_rows = wire_parse(raw)
    reader = fresh_reader(path)
    ok = (dict(all_rows) == {b'longprefix-1': b'p', b'lo-Z': b'q',
                             b'longprefix-3': b'r'}
          and reader.get(b'lo-Z') == b'q')
    record('rename_shortens_common_prefix', ok, dict(
        keys=[k.decode() for k, _ in all_rows], writer=writer.report))


def scenario_page_full_and_original_readable():
    writer = PageWriter()
    # Fill close to the byte cap with large values, then grow one value past
    # 4096. 12 records x 300-byte values fit; a 512-byte replacement does not.
    rows = [(f'v{i:02}'.encode(), bytes([65 + i]) * 300) for i in range(12)]
    raw = writer.create(rows, restart=4)
    assert raw != PAGE_FULL and len(raw) <= 4096, len(raw)
    outcome = writer.put(raw, b'v04', b'B' * 512)
    full_report = dict(writer.report)
    ok_full = outcome == PAGE_FULL
    # The original page remains fully readable and independently parseable.
    path = wire_save(raw, '09_original_before_full.pcr2')
    _d, _s, before = wire_parse(raw)
    reader = fresh_reader(path)
    ok_original = reader.get(b'v04') == rows[4][1] and len(before) == 12
    record('page_full_and_original_readable', ok_full and ok_original, dict(
        outcome=outcome, full_attempt=full_report, original_bytes=len(raw),
        original_records=len(before)))


def scenario_key_cap_full():
    writer = PageWriter()
    raw = writer.create([(f'k{i:03}'.encode(), b'') for i in range(128)], restart=8)
    outcome = writer.put(raw, b'k999', b'x')
    record('key_cap_full', raw != PAGE_FULL and outcome == PAGE_FULL,
           dict(outcome=outcome, page_bytes=len(raw)))


def scenario_empty_page():
    writer = PageWriter()
    raw = writer.create([], restart=4)
    path = wire_save(raw, '10_empty.pcr2')
    directory, per_segment, all_rows = wire_parse(raw)
    reader = fresh_reader(path)
    ok = (reader.get(b'k') is None and reader.range(b'a', b'z') == []
          and reader.audit() == [] and directory == [] and all_rows == [])
    # Inserting into an empty page produces a valid one-segment page.
    grown = writer.put(raw, b'first', b'1')
    wire_save(grown, '11_empty_then_insert.pcr2')
    _d, _s, grown_rows = wire_parse(grown)
    reader2 = fresh_reader(os.path.join(PAGES, '11_empty_then_insert.pcr2'))
    ok = ok and grown_rows == [(b'first', b'1')] and reader2.get(b'first') == b'1'
    record('empty_page', ok, dict(writer=writer.report,
                                  inspect=reader.inspect()))


def scenario_local_budget_64_keys():
    """benchmark.json contract: one equal-length value replacement on the
    64 short-key page may decode/re-encode at most 16 records."""
    writer = PageWriter()
    bench = json.load(open(os.path.join(HERE, 'benchmark.json')))
    rows = [(bytes.fromhex(r['key']), bytes.fromhex(r['value']))
            for r in bench['records']]
    raw = writer.create(rows, restart=bench['restart'])
    up_key = bytes.fromhex(bench['update']['key'])
    up_value = bytes.fromhex(bench['update']['value'])
    assert len(next(v for k, v in rows if k == up_key)) == len(up_value)
    changed = writer.put(raw, up_key, up_value)
    path = wire_save(changed, '12_benchmark_local_update.pcr2')
    _d, _s, all_rows = wire_parse(changed)
    reader = fresh_reader(path)
    cap = bench['max_decode']
    ok_budget = (writer.report['decoded'] <= cap
                 and writer.report['reencoded'] <= cap
                 and writer.report['moved'] == len(rows) - bench['restart'])
    ok_value = reader.get(up_key) == up_value and len(all_rows) == 64
    record('local_budget_64_keys', ok_budget and ok_value and len(changed) <= 4096,
           dict(report=writer.report, budget=cap, page_bytes=len(changed)))


def scenario_reader_lazy_metrics():
    """A point read decodes only the located segment (<=8 records); the
    directory parse is not counted as record decoding."""
    writer = PageWriter()
    raw = writer.create([(f'k{i:03}'.encode(), b'v') for i in range(64)], restart=8)
    wire_save(raw, '13_lazy_metrics.pcr2')
    reader = PageReader(raw)
    reader.get(b'k033')  # inside segment 4
    point = reader.inspect()
    reader2 = PageReader(raw)
    reader2.range(b'k030', b'k017')  # empty/backwards interval decodes nothing
    empty_metrics = reader2.inspect()
    reader3 = PageReader(raw)
    reader3.range(b'k032', b'k048')  # exactly two segments: k032..k039, k040..k047
    cross_metrics = reader3.inspect()
    ok = (point['decoded'] == 8 and point['groups_read'] == [4]
          and empty_metrics['decoded'] == 0
          and cross_metrics['groups_read'] == [4, 5]
          and cross_metrics['decoded'] == 16)
    record('reader_lazy_metrics', ok,
           dict(point=point, empty_interval=empty_metrics,
                cross_range=cross_metrics))


def scenario_corruption_detected_on_access():
    """Unvisited segment corruption must surface on access/audit, while an
    untouched point read still works (no upfront full-page payload check)."""
    writer = PageWriter()
    raw = bytearray(writer.create(
        [(f'k{i:03}'.encode(), b'v') for i in range(16)], restart=8))
    # Corrupt a byte inside the second segment's payload.
    directory, payload = split_page(bytes(raw))
    corrupt_pos = 8 + (len(raw) - len(payload)) + directory[1]['offset'] + 3
    raw[corrupt_pos] ^= 0xFF
    bad = bytes(raw)
    wire_save(bad, '14_corrupted_segment.pcr2')
    reader = PageReader(bad)
    point_ok = reader.get(b'k001') == b'v'  # first segment intact
    raised = None
    try:
        PageReader(bad).get(b'k010')
    except CorruptPage:
        raised = 'CorruptPage'
    audit_raised = None
    try:
        PageReader(bad).audit()
    except CorruptPage:
        audit_raised = 'CorruptPage'
    record('corruption_detected_on_access',
           point_ok and raised == 'CorruptPage' and audit_raised == 'CorruptPage',
           dict(point_read=point_ok, get_raises=raised, audit_raises=audit_raised,
                intact_inspect=reader.inspect()))


def scenario_bad_directories_rejected():
    cases = {}
    # Bad magic.
    cases['bad_magic'] = b'XXXX' + b'\x00\x00\x00\x00'
    # Truncated header length.
    cases['truncated'] = MAGIC + b'\x00\x00\x00\x10' + b'{}'
    # Directory span leaves payload uncovered.
    groups = [[(b'a', b'1')], [(b'b', b'2')]]
    good = bytearray(wire_build(groups))
    n = struct.unpack('>I', good[4:8])[0]
    directory = json.loads(bytes(good[8:8 + n]).decode())
    directory[1]['length'] -= 1  # no longer covers the payload
    header = json.dumps(directory, sort_keys=True, separators=(',', ':')).encode()
    payload = good[8 + n:]
    cases['uncovered'] = MAGIC + struct.pack('>I', len(header)) + header + payload
    outcomes = {}
    for name, raw in cases.items():
        try:
            PageReader(raw)
            outcomes[name] = 'NO_ERROR'
        except (CorruptPage, ValueError):
            outcomes[name] = 'rejected'
    record('bad_directories_rejected', all(v == 'rejected' for v in outcomes.values()),
           dict(outcomes=outcomes))


def main():
    for fn in [
        scenario_independent_basic,
        scenario_non_ascii_and_empty_value,
        scenario_prefix_compression_cross_group,
        scenario_writer_roundtrip_and_growth,
        scenario_segment_head_and_tail_edits,
        scenario_boundary_insert_split,
        scenario_delete_and_rename,
        scenario_rename_shortens_common_prefix,
        scenario_page_full_and_original_readable,
        scenario_key_cap_full,
        scenario_empty_page,
        scenario_local_budget_64_keys,
        scenario_reader_lazy_metrics,
        scenario_corruption_detected_on_access,
        scenario_bad_directories_rejected,
    ]:
        fn()
    with open(os.path.join(ART, 'metrics.json'), 'w', encoding='utf-8') as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)
    total = len(results)
    passed = sum(1 for r in results if r['ok'])
    print(f'\n{passed}/{total} scenarios passed; pages in {PAGES}, '
          f'ledger in {os.path.join(ART, "metrics.json")}')
    if passed != total:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
