"""Wire helpers shared with independent page producers; offsets are payload-relative."""
import json, struct, zlib

MAGIC = b'PCR2'
RECORD = struct.Struct('>HHH')
MAX_KEY = 64
MAX_VALUE = 512
MAX_RECORDS = 128
GROUP_MAX = 8
PAGE_LIMIT = 4096


class PageFormatError(ValueError):
    """Raised when bytes do not satisfy the PCR2 page contract."""


def split_page(raw):
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 8 or bytes(raw[:4]) != MAGIC:
        raise PageFormatError('BAD_MAGIC')
    n = struct.unpack('>I', raw[4:8])[0]
    if len(raw) < 8 + n:
        raise PageFormatError('TRUNCATED_HEADER')
    try:
        directory = json.loads(bytes(raw[8:8 + n]).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PageFormatError('BAD_DIRECTORY_JSON') from exc
    return directory, bytes(raw[8 + n:])


def join_page(directory, payload):
    header = json.dumps(directory, sort_keys=True, separators=(',', ':')).encode()
    return MAGIC + struct.pack('>I', len(header)) + header + payload


def encode_group(rows):
    out = bytearray()
    previous = b''
    for key, value in rows:
        prefix = 0
        while prefix < min(len(previous), len(key)) and previous[prefix] == key[prefix]:
            prefix += 1
        suffix = key[prefix:]
        out += RECORD.pack(prefix, len(suffix), len(value)) + suffix + value
        previous = key
    return bytes(out)


def decode_group(payload):
    """Lenient wire decode retained for compatibility; prefer decode_group_checked."""
    rows = []
    at = 0
    previous = b''
    while at < len(payload):
        prefix, suffix_len, value_len = RECORD.unpack_from(payload, at)
        at += RECORD.size
        suffix = payload[at:at + suffix_len]
        at += suffix_len
        value = payload[at:at + value_len]
        at += value_len
        key = previous[:prefix] + suffix
        rows.append((key, value))
        previous = key
    return rows


def check_key(key):
    if not isinstance(key, (bytes, bytearray)):
        raise ValueError('KEY_NOT_BYTES')
    key = bytes(key)
    if not (1 <= len(key) <= MAX_KEY):
        raise ValueError('BAD_KEY_LEN')
    try:
        key.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('KEY_NOT_UTF8') from exc
    return key


def check_value(value):
    if not isinstance(value, (bytes, bytearray)):
        raise ValueError('VALUE_NOT_BYTES')
    value = bytes(value)
    if len(value) > MAX_VALUE:
        raise ValueError('BAD_VALUE_LEN')
    return value


def _is_int(x, minimum=0):
    return isinstance(x, int) and not isinstance(x, bool) and x >= minimum


def validate_directory(directory, payload_len):
    """Check directory-level structure: typed entries, ordered firsts, spans that
    cover the whole payload contiguously without overlap or out-of-range bytes.
    Business records inside the segments are intentionally not decoded here."""
    if not isinstance(directory, list):
        raise PageFormatError('BAD_DIRECTORY')
    if not _is_int(payload_len):
        raise PageFormatError('BAD_PAYLOAD_LEN')
    cursor = 0
    previous_first = None
    for entry in directory:
        if not isinstance(entry, dict):
            raise PageFormatError('BAD_ENTRY')
        try:
            first = bytes.fromhex(entry['first'])
            offset = entry['offset']
            length = entry['length']
            count = entry['count']
            crc32 = entry['crc32']
        except (KeyError, TypeError, ValueError) as exc:
            raise PageFormatError('BAD_ENTRY') from exc
        if not _is_int(offset):
            raise PageFormatError('BAD_OFFSET')
        if not _is_int(length, 1):
            raise PageFormatError('BAD_LENGTH')
        if not (_is_int(count, 1) and count <= GROUP_MAX):
            raise PageFormatError('BAD_COUNT')
        if not (_is_int(crc32) and crc32 <= 0xFFFFFFFF):
            raise PageFormatError('BAD_CRC32')
        if not (1 <= len(first) <= MAX_KEY):
            raise PageFormatError('BAD_FIRST_LEN')
        try:
            first.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PageFormatError('FIRST_NOT_UTF8') from exc
        if previous_first is not None and first <= previous_first:
            raise PageFormatError('FIRST_NOT_ORDERED')
        if offset != cursor:
            raise PageFormatError('PAYLOAD_SPAN_GAP_OR_OVERLAP')
        if offset + length > payload_len:
            raise PageFormatError('SPAN_OUT_OF_PAYLOAD')
        cursor = offset + length
        previous_first = first
    if cursor != payload_len:
        raise PageFormatError('PAYLOAD_NOT_COVERED')


def decode_group_checked(blob, expected_count=None, expected_first=None):
    """Strictly decode one compressed segment against the PCR2 contract.

    Verifies record boundaries, first-record prefix, prefix legality against the
    previous full key, key length/UTF-8, unique ordered keys, value length, and
    optionally the directory-declared count and first key."""
    rows = []
    at = 0
    previous = b''
    while at < len(blob):
        if at + RECORD.size > len(blob):
            raise PageFormatError('TRUNCATED_RECORD_HEADER')
        prefix, suffix_len, value_len = RECORD.unpack_from(blob, at)
        at += RECORD.size
        if prefix > len(previous):
            raise PageFormatError('PREFIX_OUT_OF_RANGE')
        if not rows and prefix != 0:
            raise PageFormatError('FIRST_RECORD_PREFIX_NONZERO')
        if at + suffix_len + value_len > len(blob):
            raise PageFormatError('TRUNCATED_RECORD_BODY')
        suffix = blob[at:at + suffix_len]
        at += suffix_len
        value = blob[at:at + value_len]
        at += value_len
        key = previous[:prefix] + suffix
        if not (1 <= len(key) <= MAX_KEY):
            raise PageFormatError('BAD_KEY_LEN')
        try:
            key.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PageFormatError('KEY_NOT_UTF8') from exc
        if len(value) > MAX_VALUE:
            raise PageFormatError('BAD_VALUE_LEN')
        if rows and key <= rows[-1][0]:
            raise PageFormatError('KEYS_NOT_UNIQUE_ORDERED')
        rows.append((key, value))
        previous = key
    if at != len(blob):
        raise PageFormatError('SEGMENT_TRAILING_BYTES')
    if not rows:
        raise PageFormatError('EMPTY_SEGMENT')
    if expected_count is not None and len(rows) != expected_count:
        raise PageFormatError('COUNT_MISMATCH')
    if expected_first is not None and rows[0][0] != expected_first:
        raise PageFormatError('FIRST_KEY_MISMATCH')
    return rows
