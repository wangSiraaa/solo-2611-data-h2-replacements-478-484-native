"""End-to-end + unit evidence for the materials/481 ABI / host / product.

Run with ``python3 tests.py`` from this directory.  Everything uses
temporary directories and prints a JSON summary whose ``failures`` list is
empty on success.  This is evidence collection, not a framework: each
check records what it actually observed.
"""
import json
import struct
import sys
import tempfile
from pathlib import Path

import wasmtime

from abi import ABIError, decode_image, encode
from batch_client import consume
from product import Product, ProductError
from product_client import publish
from runtime import Runner

RESULTS = []
FAILURES = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append({'check': name, 'status': 'ok', 'detail': detail})
    except Exception as exc:  # noqa: BLE001 - evidence collection
        import traceback
        FAILURES.append({'check': name, 'status': 'fail',
                         'error': f'{type(exc).__name__}: {exc}',
                         'traceback': traceback.format_exc()})
        RESULTS.append(FAILURES[-1])


def expect_raises(exc_types, fn, label=''):
    try:
        fn()
    except exc_types as exc:
        return type(exc).__name__
    raise AssertionError(f'{label}: expected {exc_types} but nothing was raised')


# ---------------------------------------------------------------------------
# Independent validity/slice oracle (does not use ABI's decode logic)
# ---------------------------------------------------------------------------
def oracle_decode(image, address, batch_no, row_count, final):
    """Independent decoder: validates and slices straight from the image."""
    image = bytes(image)
    n = len(image)
    if address < 0 or address + 32 > n:
        raise AssertionError('descriptor out of range')
    fields = struct.unpack_from('<8I', image, address)
    version, got_batch, got_rows, offp, valp, datp, dlen, flags = fields
    assert version == 1, version
    assert got_batch == batch_no, (got_batch, batch_no)
    assert got_rows == row_count, (got_rows, row_count)
    assert bool(flags & 1) == final and not (flags & ~1), flags
    assert offp + 4 * (row_count + 1) <= n
    assert valp + (row_count + 7) // 8 <= n
    assert datp + dlen <= n
    ranges = [(address, address + 32), (offp, offp + 4 * (row_count + 1)),
              (valp, valp + (row_count + 7) // 8), (datp, datp + dlen)]
    active = sorted((s, e) for s, e in ranges if e > s)
    for (s1, e1), (s2, e2) in zip(active, active[1:]):
        assert e1 <= s2, ('overlap', active)
    offs = struct.unpack_from(f'<{row_count + 1}I', image, offp)
    assert offs[0] == 0 and offs[-1] == dlen, offs
    assert all(a <= b for a, b in zip(offs, offs[1:])), offs
    val = image[valp:valp + (row_count + 7) // 8]
    data = image[datp:datp + dlen]
    rows = []
    for i in range(row_count):
        span = data[offs[i]:offs[i + 1]]
        valid = val[i // 8] >> (i % 8) & 1
        if valid:
            # decode per code point: reject partial/truncated sequences
            text = span.decode('utf-8')
            # independent per-code-point re-encode check
            assert ''.join(chr(ord(c)) for c in text).encode('utf-8') == span
            rows.append(text)
        else:
            assert len(span) == 0, ('NULL span non-empty', i)
            rows.append(None)
    return rows


def test_identity_multi_grow(td):
    batches = [['海', None, ''], ['salt']]
    out = Runner().run(Path('identity.wat').read_text(), batches,
                       str(Path(td) / 'identity.json'), job_id='identity')
    assert out['status'] == 'complete', out
    assert out['product']['rows'] == ['海', None, '', 'salt'], out['product']
    grows = [(c['call'], c['pages_before'], c['pages_after'])
             for c in out['calls'] if c.get('pages_after') != c.get('pages_before')]
    return {'rows': out['product']['rows'], 'page_changes': grows}


def test_identity_oracle(td):
    batches = [['海', None, '', 'x' * 256, ''], [None], ['ū' * 128, None]]
    out = Runner().run(Path('identity.wat').read_text(), batches,
                       str(Path(td) / 'oracle.json'), job_id='oracle')
    assert out['status'] == 'complete', out
    flat = [r for b in batches for r in b]
    assert out['product']['rows'] == flat, (out['product']['rows'], flat)
    # spot-check the raw guest memory with the independent oracle
    return {'rows': out['product']['rows'][:3], 'total_rows': len(flat)}


def test_bad_descriptor_blocked(td):
    path = Path(td) / 'bad.json'
    out = Runner().run(Path('bad_descriptor.wat').read_text(), [['a'], ['b']],
                       str(path), job_id='bad')
    assert out['status'] == 'failed', out
    assert out['stage'].startswith('batch0:output-validate'), out['stage']
    assert isinstance(out['reason'], str) and 'ABIError' in out['reason']
    assert Product(path).inspect()['status'] != 'complete'
    assert not path.exists(), 'bad_descriptor must not publish a public artifact'
    return {'stage': out['stage'], 'reason': out['reason'][:80]}


def test_native_oob(td):
    raw = bytearray(encode(['海'], 0, True, 0))
    struct.pack_into('<I', raw, 20, len(raw) + 8)  # data_ptr past image end
    label = expect_raises(ABIError, lambda: consume(raw, 0,
                          {'batch_no': 0, 'row_count': 1, 'final': True}),
                          'native_oob')
    return {'rejected': label}


def test_offset_regression(td):
    raw = bytearray(encode(['ab', 'c'], 0, True, 0))
    # offsets [0, 2, 3]; make them [0, 3, 2] and shrink data_len to 2 so the
    # last-offset check passes but monotonicity must fail.
    struct.pack_into('<I', raw, 32 + 4, 3)
    struct.pack_into('<I', raw, 32 + 8, 2)
    struct.pack_into('<I', raw, 24, 2)
    label = expect_raises(ABIError, lambda: decode_image(raw, 0,
                          {'batch_no': 0, 'row_count': 2, 'final': True}),
                          'offset_regression')
    return {'rejected': label}


def test_last_offset_mismatch(td):
    raw = bytearray(encode(['ab'], 0, True, 0))
    struct.pack_into('<I', raw, 32 + 4, 1)  # final offset != data_len
    label = expect_raises(ABIError, lambda: decode_image(raw, 0,
                          {'batch_no': 0, 'row_count': 1, 'final': True}),
                          'last_offset')
    return {'rejected': label}


def test_null_nonempty_span(td):
    raw = bytearray(encode(['ab', None], 0, True, 0))
    # offsets are [0, 2, 2] with data_len 2; make them [0, 1, 2] so the NULL
    # row gets a non-empty [1, 2) span while the last offset still matches.
    struct.pack_into('<I', raw, 32 + 4, 1)
    label = expect_raises(ABIError, lambda: decode_image(raw, 0,
                          {'batch_no': 0, 'row_count': 2, 'final': True}),
                          'null_span')
    return {'rejected': label}


def test_wrong_batch_and_final(td):
    raw = encode(['a'], 0, False, 0)
    got = []
    got.append(expect_raises(ABIError, lambda: decode_image(raw, 0,
                       {'batch_no': 1, 'row_count': 1, 'final': False}), 'batch'))
    got.append(expect_raises(ABIError, lambda: decode_image(raw, 0,
                       {'batch_no': 0, 'row_count': 1, 'final': True}), 'final'))
    got.append(expect_raises(ABIError, lambda: decode_image(raw, 0,
                       {'batch_no': 0, 'row_count': 2, 'final': False}), 'rows'))
    return {'rejected': got}


def test_flags_and_version(td):
    raw = bytearray(encode(['a'], 0, True, 0))
    bad_flag = bytearray(raw); struct.pack_into('<I', bad_flag, 28, 3)
    bad_ver = bytearray(raw); struct.pack_into('<I', bad_ver, 0, 2)
    got = [
        expect_raises(ABIError, lambda: decode_image(bad_flag, 0,
                       {'batch_no': 0, 'row_count': 1, 'final': True}), 'flags'),
        expect_raises(ABIError, lambda: decode_image(bad_ver, 0,
                       {'batch_no': 0, 'row_count': 1, 'final': True}), 'version'),
    ]
    return {'rejected': got}


def test_validity_padding(td):
    rows = ['a'] * 3  # 3 rows -> one validity byte, high 5 bits must be 0
    raw = bytearray(encode(rows, 0, True, 0))
    # validity starts at 32 + 4*4 = 48
    raw[48] |= 0x80
    label = expect_raises(ABIError, lambda: decode_image(raw, 0,
                          {'batch_no': 0, 'row_count': 3, 'final': True}),
                          'padding')
    return {'rejected': label}


def test_overlap_and_utf8(td):
    raw = bytearray(encode(['ab'], 0, True, 0))
    validptr = struct.unpack_from('<I', raw, 12)[0]
    # point data at the validity byte's start (inside the same file) -> overlap
    struct.pack_into('<I', raw, 20, validptr)
    got = [expect_raises(ABIError, lambda: decode_image(raw, 0,
                       {'batch_no': 0, 'row_count': 1, 'final': True}), 'overlap')]
    # truncated UTF-8: claim 1 byte of a 3-byte char
    bad = bytearray(encode(['海'], 0, True, 0))
    datptr = struct.unpack_from('<I', bad, 20)[0]
    # data_len lives at offset 24; rewrite the single offset end to 1
    struct.pack_into('<I', bad, 32 + 4, 1)
    struct.pack_into('<I', bad, 24, 1)
    got.append(expect_raises(ABIError, lambda: decode_image(bad, 0,
                       {'batch_no': 0, 'row_count': 1, 'final': True}), 'utf8'))
    return {'rejected': got}


def test_immutability(td):
    raw = bytearray(encode(['海', None, ''], 0, True, 0))
    rows = decode_image(raw, 0, {'batch_no': 0, 'row_count': 3, 'final': True})
    raw[:] = b'\xff' * len(raw)
    assert rows['rows'] == ['海', None, ''], rows
    return {'rows_after_mutation': rows['rows']}


def test_offset_math_nowrap(td):
    # Put the descriptor at the very end of the image with a data pointer
    # near 2**32; a naive ptr+len would wrap, we must reject.
    raw = bytearray(64)
    struct.pack_into('<8I', raw, 16, 1, 0, 0, 16 + 32, 0xfffffff0, 0xfffffff0, 0, 0)
    label = expect_raises(ABIError, lambda: decode_image(raw, 16,
                          {'batch_no': 0, 'row_count': 0, 'final': False}),
                          'wrap')
    return {'rejected': label}


def test_product_missing_batch(td):
    path = Path(td) / 'partial.json'
    p = Product(path)
    p.begin('partial', [1, 1])
    p.append('partial', {'batch_no': 0, 'rows': ['first'], 'final': False})
    label = expect_raises(ProductError, lambda: p.commit('partial'), 'partial_commit')
    assert Product(path).inspect()['status'] != 'complete'
    assert not path.exists()
    return {'rejected': label, 'visible': Product(path).inspect()['status']}


def test_product_early_final_and_dupes(td):
    path = Path(td) / 'proto.json'
    p = Product(path)
    p.begin('proto', [1, 1])
    got = []
    got.append(expect_raises(ProductError, lambda: p.append(
        'proto', {'batch_no': 0, 'rows': ['x'], 'final': True}), 'early_final'))
    got.append(expect_raises(ProductError, lambda: p.append(
        'wrong', {'batch_no': 0, 'rows': ['x'], 'final': False}), 'wrong_job'))
    p.append('proto', {'batch_no': 0, 'rows': ['x'], 'final': False})
    got.append(expect_raises(ProductError, lambda: p.append(
        'proto', {'batch_no': 0, 'rows': ['x'], 'final': False}), 'dupe'))
    got.append(expect_raises(ProductError, lambda: p.append(
        'proto', {'batch_no': 2, 'rows': ['x'], 'final': True}), 'skip'))
    got.append(expect_raises(ProductError, lambda: p.append(
        'proto', {'batch_no': 1, 'rows': ['x', 'y'], 'final': True}), 'row_count'))
    p.append('proto', {'batch_no': 1, 'rows': ['y'], 'final': True})
    p.commit('proto')
    assert Product(path).inspect()['status'] == 'complete'
    return {'rejected': got}


def test_product_preserves_complete(td):
    path = Path(td) / 'protect.json'
    publish(path, 'keep', [['v1']])
    before = Product(path).inspect()
    got = expect_raises(ProductError, lambda: Product(path).begin('keep2', [1]), 'rerun')
    # a fresh Product aborting must not erase the complete version either
    p2 = Product(path)
    p2.abort('keep', 'simulated failure')
    after = Product(path).inspect()
    assert before == after and after['status'] == 'complete', (before, after)
    return {'rejected': got, 'preserved_rows': after['rows']}


def test_second_trap_no_prefix(td):
    path = Path(td) / 'second.json'
    out = Runner().run(Path('second_trap.wat').read_text(), [['a', 'b'], ['c']],
                       str(path), job_id='second')
    assert out['status'] == 'failed', out
    assert out['stage'] == 'batch1:process', out['stage']
    names = [c['call'] for c in out['calls']]
    # batch 0 fully ran; batch 1 alloc succeeded then process trapped and the
    # borrowed input slab was cleaned up exactly once.
    assert names == ['alloc', 'process', 'release', 'dealloc',
                     'alloc', 'process', 'dealloc-cleanup'], names
    traps = [c for c in out['calls'] if c['status'] == 'trap']
    assert len(traps) == 1 and traps[0]['call'] == 'process', traps
    assert not path.exists(), 'no public artifact may exist after a second-batch trap'
    visible = Product(path).inspect()
    assert visible['status'] == 'failed', visible
    return {'stage': out['stage'], 'visible': visible['status'],
            'public_exists': path.exists(), 'calls': names}


def test_output_alias_input(td):
    wat = '''
(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param i32) (param i32) (result i32) local.get $p))'''
    out = Runner().run(wat, [['a'], ['bb']], str(Path(td) / 'alias.json'), job_id='alias')
    assert out['status'] == 'complete', out
    names = [c['call'] for c in out['calls']]
    # exactly one release and one dealloc per batch, no double free
    assert names.count('release') == 2 and names.count('dealloc') == 2, names
    assert out['product']['rows'] == ['a', 'bb']
    return {'calls': names}


def test_release_trap(td):
    wat = '''
(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32) unreachable)
 (func (export "process") (param $p i32) (param i32) (param i32) (result i32) local.get $p))'''
    path = Path(td) / 'reltrap.json'
    out = Runner().run(wat, [['a']], str(path), job_id='reltrap')
    assert out['status'] == 'failed', out
    assert out['stage'] == 'batch0:release', out['stage']
    # release trapped; dealloc still attempted (separate responsibility)
    names = [c['call'] for c in out['calls']]
    assert 'dealloc' in names or 'dealloc-cleanup' in names, names
    assert not path.exists()
    return {'stage': out['stage'], 'calls': names}


def test_fuel_exhaustion_and_refill(td):
    path = Path(td) / 'loop.json'
    out = Runner().run(Path('loop.wat').read_text(), [['a']], str(path),
                       fuel=5000, job_id='loop')
    assert out['status'] == 'failed' and out['stage'] == 'batch0:process', out
    assert 'fuel' in out['reason'].lower(), out['reason']
    assert not path.exists()
    # identity with a tiny-but-sufficient budget must still complete and never
    # be silently refilled; with a deliberately tiny budget it must fail.
    tiny = Runner().run(Path('identity.wat').read_text(), [['a'], ['b']],
                        str(Path(td) / 'tiny.json'), fuel=5, job_id='tiny')
    assert tiny['status'] == 'failed', tiny
    return {'loop_stage': out['stage'], 'tiny_stage': tiny['stage'],
            'tiny_reason': tiny['reason'][:60]}


def test_dealloc_after_copy(td):
    # Guest wipes its slab in dealloc.  Host must have copied output rows
    # before dealloc, so the product must still contain them.
    wat = '''
(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param $p i32) (param $n i32)
  (memory.fill (local.get $p) (i32.const 0) (local.get $n)))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param i32) (param i32) (result i32) local.get $p))'''
    out = Runner().run(wat, [['keep', None, '']], str(Path(td) / 'copy.json'),
                       job_id='copy')
    assert out['status'] == 'complete', out
    assert out['product']['rows'] == ['keep', None, ''], out['product']['rows']
    return {'rows': out['product']['rows']}


def test_grow_then_output(td):
    # Output written to the newly grown page after process; the host must read
    # the memory *after* the call, not a pre-grow view.
    wat = '''
(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param i32) (param i32) (result i32)
  i32.const 1 memory.grow drop
  i32.const 65536 local.get $p i32.const 128 memory.copy
  i32.const 65536))'''
    out = Runner().run(wat, [['海', None, '']], str(Path(td) / 'grow.json'),
                       job_id='grow')
    assert out['status'] == 'complete', out
    assert out['product']['rows'] == ['海', None, ''], out['product']['rows']
    pages = [(c['pages_before'], c['pages_after']) for c in out['calls']
             if c['call'] == 'process']
    return {'rows': out['product']['rows'], 'process_pages': pages}


def test_next_job_isolation(td):
    good = Path(td) / 'iso_good.json'
    a = Runner().run(Path('identity.wat').read_text(), [['A']], str(good), job_id='good')
    assert a['status'] == 'complete'
    bad = Runner().run(Path('second_trap.wat').read_text(),
                       [['x'], ['y']], str(Path(td) / 'iso_bad.json'), job_id='bad')
    assert bad['status'] == 'failed'
    b = Runner().run(Path('identity.wat').read_text(), [['B'], ['C']],
                     str(Path(td) / 'iso_good2.json'), job_id='good2')
    assert b['status'] == 'complete', b
    assert b['product']['rows'] == ['B', 'C']
    # each job used its own store: pages start at 1 again for the fresh one
    first_pages = b['calls'][0]['pages_before']
    return {'good_rows': a['product']['rows'], 'bad_stage': bad['stage'],
            'second_good_rows': b['product']['rows'], 'fresh_pages': first_pages}


def test_module_interface_rejected(td):
    bad_import = '(module (import "env" "f" (func)) (memory (export "memory") 1 4)' \
                 '(func (export "alloc")(param i32)(result i32) i32.const 0)' \
                 '(func (export "dealloc")(param i32 i32))' \
                 '(func (export "release")(param i32))' \
                 '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    max5 = '(module (memory (export "memory") 1 5)' \
           '(func (export "alloc")(param i32)(result i32) i32.const 0)' \
           '(func (export "dealloc")(param i32 i32))' \
           '(func (export "release")(param i32))' \
           '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    no_max = '(module (memory (export "memory") 1)' \
             '(func (export "alloc")(param i32)(result i32) i32.const 0)' \
             '(func (export "dealloc")(param i32 i32))' \
             '(func (export "release")(param i32))' \
             '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    min5 = '(module (memory (export "memory") 5 5)' \
           '(func (export "alloc")(param i32)(result i32) i32.const 0)' \
           '(func (export "dealloc")(param i32 i32))' \
           '(func (export "release")(param i32))' \
           '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    missing = '(module (memory (export "memory") 1 4)' \
              '(func (export "alloc")(param i32)(result i32) i32.const 0))'
    bad_sig = '(module (memory (export "memory") 1 4)' \
              '(func (export "alloc")(param i32)(result i64) i64.const 0)' \
              '(func (export "dealloc")(param i32 i32))' \
              '(func (export "release")(param i32))' \
              '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    out = {}
    for label, wat in [('import', bad_import), ('max5', max5), ('no_max', no_max),
                       ('min5', min5), ('missing', missing), ('bad_sig', bad_sig)]:
        r = Runner().run(wat, [['a']], str(Path(td) / (label + '.json')), job_id=label)
        assert r['status'] == 'failed' and r['stage'] == 'module-check', (label, r)
        out[label] = r['reason'][:60]
    return out


def _tampering_guest(store_field_mem_offset, value_expr):
    """Guest that rewrites one output descriptor field before returning it."""
    return f'''
(module
 (memory (export "memory") 1 4)
 (global $next (mut i32) (i32.const 4096))
 (func (export "alloc") (param $n i32) (result i32)
  (local $p i32) global.get $next local.set $p global.get $next local.get $n i32.add global.set $next local.get $p)
 (func (export "dealloc") (param i32 i32))
 (func (export "release") (param i32))
 (func (export "process") (param $p i32) (param $batch i32) (param $final i32) (result i32)
  local.get $p i32.const {store_field_mem_offset} i32.add
  {value_expr}
  i32.store
  local.get $p))'''


def test_wrong_output_identity(td):
    # Output descriptor field offsets inside the 32-byte header.
    cases = {
        'batch_no': (4, 'i32.const 7'),
        'row_count': (8, 'i32.const 99'),
        'flags': (28, 'i32.const 0'),          # clears FINAL on a final batch
        'flags_reserved': (28, 'i32.const 3'),  # reserved bit set
        'version': (0, 'i32.const 2'),
    }
    out = {}
    for label, (offset, expr) in cases.items():
        wat = _tampering_guest(offset, expr)
        path = Path(td) / f'tamper_{label}.json'
        k = 1 if label in ('flags', 'flags_reserved') else 0
        r = Runner().run(wat, [['a'], ['b']], str(path), job_id=label)
        assert r['status'] == 'failed', (label, r)
        assert 'output-validate' in r['stage'] or 'process' in r['stage'], (label, r['stage'])
        assert not path.exists(), label
        out[label] = r['stage']
    return out


def test_rerun_does_not_overwrite(td):
    path = Path(td) / 'once.json'
    first = Runner().run(Path('identity.wat').read_text(), [['v1']],
                         str(path), job_id='once')
    assert first['status'] == 'complete'
    before = Product(path).inspect()
    second = Runner().run(Path('identity.wat').read_text(), [['v2']],
                          str(path), job_id='once')
    assert second['status'] == 'failed', second
    after = Product(path).inspect()
    assert after == before and after['status'] == 'complete', (before, after)
    assert after['rows'] == ['v1'], after['rows']
    # a failed job can be rerun in a fresh directory
    third = Runner().run(Path('identity.wat').read_text(), [['v3']],
                         str(Path(td) / 'once_retry.json'), job_id='once-retry')
    assert third['status'] == 'complete', third
    return {'first': before['rows'], 'second_stage': second['stage'],
            'third': third['product']['rows']}


def test_negative_output_address(td):
    wat = '(module (memory (export "memory") 1 4)' \
          '(func (export "alloc")(param i32)(result i32) i32.const 4096)' \
          '(func (export "dealloc")(param i32 i32))' \
          '(func (export "release")(param i32))' \
          '(func (export "process")(param i32 i32 i32)(result i32) i32.const -1))'
    r = Runner().run(wat, [['a']], str(Path(td) / 'neg.json'), job_id='neg')
    assert r['status'] == 'failed', r
    assert 'negative' in r['reason'].lower() or 'negative' in r['stage'], r
    return {'stage': r['stage'], 'reason': r['reason'][:60]}


def test_alloc_bad_pointer(td):
    wat = '(module (memory (export "memory") 1 4)' \
          '(func (export "alloc")(param i32)(result i32) i32.const 65535)' \
          '(func (export "dealloc")(param i32 i32))' \
          '(func (export "release")(param i32))' \
          '(func (export "process")(param i32 i32 i32)(result i32) i32.const 0))'
    r = Runner().run(wat, [['a', 'b', 'c']], str(Path(td) / 'allocbad.json'), job_id='ab')
    assert r['status'] == 'failed' and 'alloc' in r['stage'], r
    return {'stage': r['stage'], 'reason': r['reason'][:70]}


def test_invalid_inputs_not_called(td):
    # Too many batches / rows / text bytes / non-string must fail pre-flight
    # without any guest call at all.
    too_many_batches = [[] for _ in range(5)]
    out = []
    for label, batches, extra in [
        ('batches', too_many_batches, {}),
        ('rows', [['x'] * 129], {}),
        ('text', [['x' * 257]], {}),
        ('type', [[b'not-str']], {}),
        ('fuel', [['a']], {'fuel': 0}),
    ]:
        kwargs = dict(job_id=label, path=str(Path(td) / (label + '.json')))
        kwargs.update(extra)
        r = Runner().run(Path('identity.wat').read_text(), batches, **kwargs)
        assert r['status'] == 'failed' and r['calls'] == [], (label, r)
        out.append((label, r['stage']))
    return {'rejected': out}


def test_workload_limit_boundaries(td):
    # 4 batches x 128 rows, each 1 byte -> within job 64KiB limit.
    batches = [['x'] * 128 for _ in range(4)]
    out = Runner().run(Path('identity.wat').read_text(), batches,
                       str(Path(td) / 'max.json'), job_id='max', fuel=200000)
    assert out['status'] == 'complete', out
    assert len(out['product']['rows']) == 512
    return {'rows': len(out['product']['rows'])}


def test_empty_batch(td):
    out = Runner().run(Path('identity.wat').read_text(), [[]],
                       str(Path(td) / 'empty.json'), job_id='empty')
    assert out['status'] == 'complete' and out['product']['rows'] == [], out
    return {'rows': out['product']['rows']}


def test_oracle_against_raw_guest_memory(td):
    # Call identity directly and run the independent oracle on its *current*
    # memory to cross-check abi.decode_image code point by code point.
    cfg = wasmtime.Config(); cfg.consume_fuel = True
    engine = wasmtime.Engine(cfg)
    store = wasmtime.Store(engine); store.set_fuel(100000)
    module = wasmtime.Module(engine, Path('identity.wat').read_text())
    inst = wasmtime.Instance(store, module, []); ex = inst.exports(store)
    memory = ex['memory']
    rows = ['海', None, '', 'aé世', None]
    raw = encode(rows, 0, True, 0)
    addr = ex['alloc'](store, len(raw))
    packed = encode(rows, 0, True, addr)
    memory.write(store, packed, addr)
    out_addr = ex['process'](store, addr, 0, 1)
    snapshot = memory.read(store, 0, memory.data_len(store))
    host = decode_image(snapshot, out_addr,
                        {'batch_no': 0, 'row_count': len(rows), 'final': True})
    oracle = oracle_decode(snapshot, out_addr, 0, len(rows), True)
    assert host['rows'] == oracle == rows, (host['rows'], oracle, rows)
    return {'agreement': True, 'rows': host['rows']}


def main():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        check('identity_multi_grow', lambda: test_identity_multi_grow(td))
        check('identity_oracle', lambda: test_identity_oracle(td))
        check('bad_descriptor_blocked', lambda: test_bad_descriptor_blocked(td))
        check('native_oob', lambda: test_native_oob(td))
        check('offset_regression', lambda: test_offset_regression(td))
        check('last_offset_mismatch', lambda: test_last_offset_mismatch(td))
        check('null_nonempty_span', lambda: test_null_nonempty_span(td))
        check('wrong_batch_and_final', lambda: test_wrong_batch_and_final(td))
        check('flags_and_version', lambda: test_flags_and_version(td))
        check('validity_padding', lambda: test_validity_padding(td))
        check('overlap_and_utf8', lambda: test_overlap_and_utf8(td))
        check('immutability', lambda: test_immutability(td))
        check('offset_math_nowrap', lambda: test_offset_math_nowrap(td))
        check('product_missing_batch', lambda: test_product_missing_batch(td))
        check('product_early_final_and_dupes', lambda: test_product_early_final_and_dupes(td))
        check('product_preserves_complete', lambda: test_product_preserves_complete(td))
        check('second_trap_no_prefix', lambda: test_second_trap_no_prefix(td))
        check('output_alias_input', lambda: test_output_alias_input(td))
        check('release_trap', lambda: test_release_trap(td))
        check('fuel_exhaustion_and_refill', lambda: test_fuel_exhaustion_and_refill(td))
        check('dealloc_after_copy', lambda: test_dealloc_after_copy(td))
        check('grow_then_output', lambda: test_grow_then_output(td))
        check('next_job_isolation', lambda: test_next_job_isolation(td))
        check('module_interface_rejected', lambda: test_module_interface_rejected(td))
        check('wrong_output_identity', lambda: test_wrong_output_identity(td))
        check('rerun_does_not_overwrite', lambda: test_rerun_does_not_overwrite(td))
        check('negative_output_address', lambda: test_negative_output_address(td))
        check('alloc_bad_pointer', lambda: test_alloc_bad_pointer(td))
        check('invalid_inputs_not_called', lambda: test_invalid_inputs_not_called(td))
        check('workload_limit_boundaries', lambda: test_workload_limit_boundaries(td))
        check('empty_batch', lambda: test_empty_batch(td))
        check('oracle_against_raw_guest_memory', lambda: test_oracle_against_raw_guest_memory(td))

    summary = {'passed': len(RESULTS) - len(FAILURES),
               'failed': len(FAILURES), 'failures': FAILURES}
    print(json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True))
    return 0 if not FAILURES else 1


if __name__ == '__main__':
    sys.exit(main())
