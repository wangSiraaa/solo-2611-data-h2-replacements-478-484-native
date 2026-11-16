"""Nullable UTF-8 single-column batch ABI, little-endian 32-bit fields.

The wire format is a 32-byte descriptor followed by three buffers whose
absolute addresses are stored in the descriptor::

    uint32 abi_version
    uint32 batch_no
    uint32 row_count
    uint32 offsets_ptr      row_count + 1 uint32 offsets
    uint32 validity_ptr     ceil(row_count / 8) bytes (bit i == row i valid)
    uint32 data_ptr         data_len bytes
    uint32 data_len
    uint32 flags            bit 0 is FINAL, every other bit is reserved

``decode_image`` is a standalone production entry point.  It deliberately
does not know anything about Wasmtime: ``image`` is one frozen snapshot of
the currently accessible linear memory and its length is the complete
address space for that call.  Every field is range-checked *before* it is
used to slice, so a truncated or overflowing range is an error rather than
silently becoming a short / empty row.
"""
from __future__ import annotations

import struct
from collections.abc import Mapping

HEADER = struct.Struct('<8I')
ABI_VERSION = 1
FLAG_FINAL = 1
MAX_ROWS_PER_BATCH = 128


class ABIError(ValueError):
    """Raised when a batch image violates the ABI contract."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _byte_view(image):
    """Return a flat one-byte-per-item view without assuming image length."""
    if isinstance(image, (bytes, bytearray)):
        return memoryview(image).cast('B')
    try:
        view = memoryview(image)
    except TypeError as exc:
        raise ABIError('image must be bytes, bytearray or a memoryview') from exc
    if view.ndim == 1 and view.itemsize == 1:
        if not view.contiguous:
            # struct.unpack_from needs a contiguous buffer; copying the view
            # does not extend the caller-provided accessible range.
            return memoryview(bytes(view))
        return view
    try:
        return view.cast('B')
    except (TypeError, ValueError):
        try:
            flat = memoryview(bytes(view))
        except Exception as exc:  # pragma: no cover - defensive
            raise ABIError('image must be a flat byte buffer') from exc
        return flat


def _checked_range(name, start, length, total):
    """Validate ``[start, start+length)`` using wide integers.

    Python integers do not overflow, so this also catches the 32-bit
    ``ptr + len`` wrap that a machine-word implementation could suffer.
    """
    if not _is_int(start) or start < 0:
        raise ABIError(f'{name} start must be a non-negative integer')
    if not _is_int(length) or length < 0:
        raise ABIError(f'{name} length must be a non-negative integer')
    end = start + length
    if end > total:
        raise ABIError(f'{name} range [{start}, {end}) exceeds image length {total}')
    return start, end


def _validate_expected(expected):
    if not isinstance(expected, Mapping):
        raise ABIError('expected must be a mapping with batch_no, row_count, final')
    missing = [k for k in ('batch_no', 'row_count', 'final') if k not in expected]
    if missing:
        raise ABIError('expected is missing: ' + ', '.join(missing))
    batch_no = expected['batch_no']
    row_count = expected['row_count']
    final = expected['final']
    if not _is_int(batch_no) or batch_no < 0:
        raise ABIError('expected.batch_no must be a non-negative integer')
    if not _is_int(row_count) or not (0 <= row_count <= MAX_ROWS_PER_BATCH):
        raise ABIError(
            f'expected.row_count must be an integer in 0..{MAX_ROWS_PER_BATCH}')
    if not isinstance(final, bool):
        if _is_int(final) and final in (0, 1):
            final = bool(final)
        else:
            raise ABIError('expected.final must be a boolean')
    return batch_no, row_count, final


def _overlap(segments):
    """Reject overlapping non-empty segments.

    ``segments`` is a list of (name, start, end); zero-length intervals are
    ignored.  Touching endpoints are allowed.
    """
    active = sorted((s, e, n) for n, s, e in segments if e > s)
    for (pstart, pend, pname), (sstart, send, sname) in zip(active, active[1:]):
        if sstart < pend:
            raise ABIError(f'{pname} and {sname} segments overlap')


def decode_image(image, address, expected):
    """Decode and fully validate one batch from a current-memory image.

    ``address`` is relative to the start of ``image``; ``len(image)`` is the
    only accessible address space.  ``expected`` supplies the host-side
    identity of this batch (``batch_no``, ``row_count``, ``final``).  On
    success a JSON-serialisable ``{'batch_no', 'final', 'rows'}`` is
    returned; rows are already copied into immutable Python strings, so a
    later mutation of a mutable ``image`` cannot change them.  Any contract
    violation raises :class:`ABIError` and no partial rows are returned.
    """
    view = _byte_view(image)
    total = view.nbytes
    if not _is_int(address) or address < 0:
        raise ABIError('address must be a non-negative integer')

    exp_batch, exp_rows, exp_final = _validate_expected(expected)

    # Descriptor first: everything else is only reachable through it.
    _checked_range('descriptor', address, HEADER.size, total)
    version, batch_no, row_count, offsets_ptr, validity_ptr, data_ptr, data_len, flags = \
        HEADER.unpack(bytes(view[address:address + HEADER.size]))

    if version != ABI_VERSION:
        raise ABIError(f'unsupported ABI version {version}')
    if flags & ~FLAG_FINAL:
        raise ABIError(f'reserved flag bits set: 0x{flags:08x}')
    if row_count > MAX_ROWS_PER_BATCH:
        raise ABIError(f'row_count {row_count} exceeds {MAX_ROWS_PER_BATCH}')
    if batch_no != exp_batch:
        raise ABIError(f'batch_no mismatch: descriptor {batch_no}, expected {exp_batch}')
    if row_count != exp_rows:
        raise ABIError(f'row_count mismatch: descriptor {row_count}, expected {exp_rows}')
    final = bool(flags & FLAG_FINAL)
    if final != exp_final:
        raise ABIError(f'final mismatch: descriptor {final}, expected {exp_final}')

    offsets_len = 4 * (row_count + 1)
    validity_len = (row_count + 7) // 8
    off_start, off_end = _checked_range('offsets', offsets_ptr, offsets_len, total)
    val_start, val_end = _checked_range('validity', validity_ptr, validity_len, total)
    dat_start, dat_end = _checked_range('data', data_ptr, data_len, total)

    _overlap([
        ('descriptor', address, address + HEADER.size),
        ('offsets', off_start, off_end),
        ('validity', val_start, val_end),
        ('data', dat_start, dat_end),
    ])

    offsets = struct.unpack_from(f'<{row_count + 1}I', view, off_start)
    if offsets[0] != 0:
        raise ABIError(f'offsets[0] must be 0, got {offsets[0]}')
    previous = 0
    for index, offset in enumerate(offsets):
        if offset < previous:
            raise ABIError(f'offsets are not monotonic at index {index}: '
                           f'{previous} then {offset}')
        previous = offset
    if offsets[-1] != data_len:
        raise ABIError(f'final offset {offsets[-1]} does not equal data_len {data_len}')

    validity = bytes(view[val_start:val_end])
    data = bytes(view[dat_start:dat_end])

    if row_count % 8 and validity:
        used_mask = (1 << (row_count % 8)) - 1
        if validity[-1] & ~used_mask:
            raise ABIError('unused high padding bits in the validity byte are not zero')

    rows = []
    for index in range(row_count):
        start, end = offsets[index], offsets[index + 1]
        if start > data_len or end > data_len:  # defensive; monotonic + last check
            raise ABIError(f'row {index} span exceeds data length')
        valid = bool(validity[index // 8] & (1 << (index % 8)))
        if not valid:
            if end != start:
                raise ABIError(f'NULL row {index} has a non-empty span')
            rows.append(None)
            continue
        # A valid row may be empty.  A non-empty span must be complete UTF-8
        # with no truncated sequences; decode() enforces that for the whole
        # span (surrogates, overlongs and >U+10FFFF are rejected too).
        try:
            rows.append(data[start:end].decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise ABIError(f'row {index} is not valid UTF-8: {exc}') from exc

    return {'batch_no': batch_no, 'final': final, 'rows': rows}


def encode(rows, batch_no, final, address):
    """Encode a host input slab (descriptor + offsets + validity + data).

    The returned bytes are contiguous starting at ``address``.  The host
    always validates the slab it produced before handing it to a guest and
    validates the guest's output through :func:`decode_image`; ``encode``
    itself rejects values that cannot be represented in the wire format.
    """
    if not _is_int(batch_no) or batch_no < 0:
        raise ABIError('batch_no must be a non-negative integer')
    if not isinstance(final, bool):
        raise ABIError('final must be a boolean')
    if not _is_int(address) or not (0 <= address <= 0xffffffff):
        raise ABIError('address must fit in a uint32')
    if not isinstance(rows, (list, tuple)):
        raise ABIError('rows must be a sequence')
    if len(rows) > MAX_ROWS_PER_BATCH:
        raise ABIError(f'row_count {len(rows)} exceeds {MAX_ROWS_PER_BATCH}')

    validity = bytearray((len(rows) + 7) // 8)
    data = bytearray()
    offsets = [0]
    for n, value in enumerate(rows):
        if value is not None:
            if not isinstance(value, str):
                raise ABIError(f'row {n} must be None or str')
            try:
                encoded = value.encode('utf-8')
            except UnicodeEncodeError as exc:
                raise ABIError(f'row {n} is not valid UTF-8: {exc}') from exc
            validity[n // 8] |= 1 << (n % 8)
            data.extend(encoded)
        offsets.append(len(data))

    if address + HEADER.size > 0xffffffff:
        raise ABIError('address leaves no room for the descriptor')
    offptr = address + HEADER.size
    validptr = offptr + 4 * len(offsets)
    dataptr = validptr + len(validity)
    if dataptr > 0xffffffff or dataptr + len(data) > 0x100000000:
        raise ABIError('input slab does not fit in 32-bit address space')
    header = HEADER.pack(1, batch_no, len(rows), offptr, validptr, dataptr,
                         len(data), int(final))
    return (header + struct.pack(f'<{len(offsets)}I', *offsets)
            + validity + data)
