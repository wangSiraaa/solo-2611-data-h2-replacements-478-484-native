"""PCR2 page producer.

Edits rebuild the page from segments. Only the one or two restart segments
touched by an operation are CRC-checked, decoded and re-encoded; every
untouched segment is moved byte-for-byte (never decoded) and offsets are
re-laid contiguously. If the serialized result would exceed the 4096-byte
page limit (or the 128 unique-key cap), the operation returns PAGE_FULL
without mutating the caller's input bytes.
"""
import bisect
import zlib

from .format import (
    GROUP_MAX,
    MAX_RECORDS,
    PAGE_LIMIT,
    PageFormatError,
    check_key,
    check_value,
    decode_group_checked,
    encode_group,
    join_page,
    split_page,
    validate_directory,
)

PAGE_FULL = 'PAGE_FULL'


class PageWriter:
    def __init__(self):
        # Filled after every mutating call; see _assemble for the ledger.
        self.report = None

    # ------------------------------------------------------------------ build
    def create(self, rows, restart=4):
        if not isinstance(restart, int) or isinstance(restart, bool) or not (1 <= restart <= GROUP_MAX):
            raise ValueError('BAD_RESTART')
        ordered = []
        seen = set()
        for key, value in rows:
            key, value = check_key(key), check_value(value)
            if key in seen:
                raise ValueError('DUPLICATE_KEY')
            seen.add(key)
            ordered.append((key, value))
        ordered.sort()
        if len(ordered) > MAX_RECORDS:
            return PAGE_FULL
        segments = [
            dict(entry=None, blob=None, rows=ordered[i:i + restart])
            for i in range(0, len(ordered), restart)
        ]
        raw = self._assemble(segments, action='create')
        return PAGE_FULL if raw is None else raw

    # ------------------------------------------------------------------ edit
    def put(self, raw, key, value):
        """Insert or replace one key. Membership is unchanged on equal-length
        replacement, so only the located segment is re-encoded."""
        key, value = check_key(key), check_value(value)
        segments, _payload = self._load(raw)
        if not segments:
            return self._finish(
                [dict(entry=None, blob=None, rows=[(key, value)])], 'put')
        index = self._locate(segments, key)
        rows = self._rows(segments[index])
        position = bisect.bisect_left(rows, (key, b''))
        existed = position < len(rows) and rows[position][0] == key
        if existed:
            rows[position] = (key, value)
        else:
            if sum(s['entry']['count'] for s in segments) >= MAX_RECORDS:
                return PAGE_FULL
            bisect.insort(rows, (key, value))
        self._reshape(segments, index, rows)
        return self._finish(segments, 'put')

    def delete(self, raw, key):
        key = check_key(key)
        segments, _payload = self._load(raw)
        if not segments:
            raise KeyError(key)
        index = self._locate(segments, key)
        rows = self._rows(segments[index])
        position = bisect.bisect_left(rows, (key, b''))
        if position >= len(rows) or rows[position][0] != key:
            raise KeyError(key)
        del rows[position]
        if rows:
            segments[index].update(rows=rows, blob=None)
        else:
            del segments[index]
        return self._finish(segments, 'delete')

    def rename(self, raw, old_key, new_key):
        """Move old_key to new_key keeping its value. Fails when old_key is
        absent or new_key already exists (identity rename included)."""
        old_key, new_key = check_key(old_key), check_key(new_key)
        if old_key == new_key:
            raise KeyError(new_key)
        segments, _payload = self._load(raw)
        if not segments:
            raise KeyError(old_key)
        old_index = self._locate(segments, old_key)
        new_index = self._locate(segments, new_key)
        old_rows = self._rows(segments[old_index])
        new_rows = self._rows(segments[new_index])
        old_pos = bisect.bisect_left(old_rows, (old_key, b''))
        if old_pos >= len(old_rows) or old_rows[old_pos][0] != old_key:
            raise KeyError(old_key)
        new_pos = bisect.bisect_left(new_rows, (new_key, b''))
        if new_pos < len(new_rows) and new_rows[new_pos][0] == new_key:
            raise KeyError(new_key)
        value = old_rows[old_pos][1]
        del old_rows[old_pos]
        bisect.insort(new_rows, (new_key, value))

        if old_index == new_index:
            # The segment already reflects both edits in new_rows.
            assembled = list(segments)
            assembled[old_index:old_index + 1] = self._reshaped(new_rows)
        else:
            # Walk segments by identity: drop the source segment when emptied,
            # swap the target segment for its reshaped replacement, and carry
            # every untouched segment over verbatim.
            assembled = []
            for index, segment in enumerate(segments):
                if index == old_index:
                    assembled.extend(self._reshaped(old_rows))  # none if emptied
                elif index == new_index:
                    assembled.extend(self._reshaped(new_rows))
                else:
                    assembled.append(segment)
        return self._finish(assembled, 'rename')

    # ------------------------------------------------------------- persistence
    @staticmethod
    def save(raw, path):
        with open(path, 'wb') as fh:
            fh.write(raw)
        return path

    @staticmethod
    def open(path):
        with open(path, 'rb') as fh:
            return fh.read()

    # ---------------------------------------------------------------- helpers
    def _load(self, raw):
        """Split an incoming page into lazy segments. Untouched segments keep
        their original bytes and are never decoded; edited segments are
        materialized via _rows when an operation actually reaches them."""
        directory, payload = split_page(raw)
        validate_directory(directory, len(payload))
        segments = []
        for entry in directory:
            start, length = entry['offset'], entry['length']
            segments.append(dict(
                entry=entry,
                blob=payload[start:start + length],
                rows=None,
            ))
        return segments, payload

    @staticmethod
    def _locate(segments, key):
        """Rightmost segment whose directory first key is <= key."""
        firsts = [bytes.fromhex(s['entry']['first']) for s in segments]
        return max(bisect.bisect_right(firsts, key) - 1, 0)

    def _rows(self, segment):
        """Materialize one segment: verify CRC and decode its records under
        the full per-segment contract."""
        if segment['rows'] is not None:
            return segment['rows']
        entry = segment['entry']
        blob = segment['blob']
        if zlib.crc32(blob) != entry['crc32']:
            raise PageFormatError('CRC_MISMATCH')
        rows = decode_group_checked(
            blob,
            expected_count=entry['count'],
            expected_first=bytes.fromhex(entry['first']),
        )
        segment['rows'] = rows
        segment['blob'] = None
        return rows

    @staticmethod
    def _reshaped(rows):
        """Return replacement segment dicts for rows (empty list if the
        segment was emptied); split balanced when above the record cap."""
        if not rows:
            return []
        if len(rows) <= GROUP_MAX:
            return [dict(entry=None, blob=None, rows=rows)]
        half = (len(rows) + 1) // 2
        return [
            dict(entry=None, blob=None, rows=rows[:half]),
            dict(entry=None, blob=None, rows=rows[half:]),
        ]

    @staticmethod
    def _reshape(segments, index, rows):
        """In-place variant used when only one segment is touched."""
        segments[index:index + 1] = PageWriter._reshaped(rows)

    def _finish(self, segments, action):
        raw = self._assemble(segments, action=action)
        return PAGE_FULL if raw is None else raw

    def _assemble(self, segments, action):
        directory = []
        payload = bytearray()
        reencoded = 0
        moved = 0
        decoded = sum(len(s['rows']) for s in segments if s['rows'] is not None)
        for segment in segments:
            if segment['rows'] is None:
                # Untouched segment: relocate its original bytes verbatim.
                entry = segment['entry']
                blob = segment['blob']
                first_hex = entry['first']
                count = entry['count']
                moved += count
            else:
                rows = segment['rows']
                blob = encode_group(rows)
                first_hex = rows[0][0].hex()
                count = len(rows)
                reencoded += count
            directory.append(dict(
                first=first_hex,
                offset=len(payload),
                length=len(blob),
                count=count,
                crc32=zlib.crc32(blob),
            ))
            payload += blob
        raw = join_page(directory, bytes(payload))
        if len(raw) > PAGE_LIMIT:
            self.report = dict(
                action=action, decoded=decoded, reencoded=reencoded, moved=moved,
                payload_bytes=len(payload), page_bytes=len(raw),
                would_exceed=PAGE_LIMIT,
            )
            return None
        # Self-check: the page we hand out satisfies the same directory
        # contract an independent reader enforces (metadata only).
        check_directory, check_payload = split_page(raw)
        validate_directory(check_directory, len(check_payload))
        self.report = dict(
            action=action,
            decoded=decoded,
            reencoded=reencoded,
            moved=moved,
            payload_bytes=len(payload),
            page_bytes=len(raw),
            segments=len(segments),
        )
        return raw
