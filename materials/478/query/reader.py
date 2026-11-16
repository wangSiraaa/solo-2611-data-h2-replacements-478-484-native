"""Page consumer used without PageWriter by the reporting application.

Only the directory is parsed at construction; segment payloads are CRC-checked
and strictly decoded when a get/range/audit actually visits them, so random
reads touch at most the located restart segment (up to eight records)."""
import bisect
import zlib

from storage.format import (
    MAX_RECORDS,
    PageFormatError,
    decode_group_checked,
    split_page,
    validate_directory,
)


class CorruptPage(PageFormatError):
    """A visited segment violates the PCR2 contract (CRC/boundary/records)."""


class PageReader:
    def __init__(self, raw):
        self.raw = bytes(raw)
        self.directory, self.payload = split_page(self.raw)
        # Metadata-only validation; no business record is decoded here.
        validate_directory(self.directory, len(self.payload))
        self._firsts = [bytes.fromhex(e['first']) for e in self.directory]
        self._cache = {}
        self.decoded = 0          # business records actually decoded
        self.groups_read = []     # segment indices whose payload was visited
        self.bytes_read = 0       # payload bytes of visited segments

    @classmethod
    def open(cls, path):
        with open(path, 'rb') as fh:
            return cls(fh.read())

    def _load(self, index):
        """Return the decoded rows of one segment, verifying CRC and the full
        per-segment contract. Cached revisits are not counted again."""
        if index in self._cache:
            return self._cache[index]
        entry = self.directory[index]
        start, length = entry['offset'], entry['length']
        blob = self.payload[start:start + length]
        if len(blob) != length:
            raise CorruptPage('SPAN_OUT_OF_PAYLOAD')
        if zlib.crc32(blob) != entry['crc32']:
            raise CorruptPage('CRC_MISMATCH')
        try:
            rows = decode_group_checked(
                blob,
                expected_count=entry['count'],
                expected_first=self._firsts[index],
            )
        except PageFormatError as exc:
            raise CorruptPage(str(exc)) from exc
        # Boundary against the next restart point: every key in this segment
        # must stay below the following segment's first key.
        if index + 1 < len(self.directory) and rows[-1][0] >= self._firsts[index + 1]:
            raise CorruptPage('SEGMENT_CROSSES_NEXT_FIRST')
        self._cache[index] = rows
        bisect.insort(self.groups_read, index)
        self.bytes_read += length
        self.decoded += len(rows)
        return rows

    def _locate(self, key):
        """Rightmost segment whose first key is <= key."""
        pos = bisect.bisect_right(self._firsts, key) - 1
        return max(pos, 0)

    def get(self, key):
        if not isinstance(key, (bytes, bytearray)):
            raise ValueError('KEY_NOT_BYTES')
        key = bytes(key)
        if not self.directory:
            return None
        return dict(self._load(self._locate(key))).get(key)

    def range(self, start, end):
        if not isinstance(start, (bytes, bytearray)) or not isinstance(end, (bytes, bytearray)):
            raise ValueError('RANGE_KEY_NOT_BYTES')
        start, end = bytes(start), bytes(end)
        if start >= end or not self.directory:
            return []
        index = self._locate(start)
        out = []
        while index < len(self.directory):
            if self._firsts[index] >= end:
                break
            for key, value in self._load(index):
                if key >= end:
                    break
                if key >= start:
                    out.append((key, value))
            index += 1
        return out

    def audit(self):
        """Sequentially visit every segment: CRC, boundaries, record contract,
        page-wide record cap and ordered firsts. Returns the full key list."""
        all_rows = []
        for index in range(len(self.directory)):
            rows = self._load(index)
            all_rows.extend(rows)
        if len(all_rows) > MAX_RECORDS:
            raise CorruptPage('TOO_MANY_RECORDS')
        return all_rows

    def inspect(self):
        return dict(
            directory=self.directory,
            groups_read=self.groups_read,
            decoded=self.decoded,
            bytes_read=self.bytes_read,
        )
