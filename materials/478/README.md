Run `python3 probe.py` from this directory. `storage.writer.PageWriter` and
`query.reader.PageReader` are separately called production APIs.
`storage.format` defines PCR2 framing; the reader fixture is assembled without
invoking PageWriter.

## Layout

- `storage/format.py` — PCR2 wire helpers shared with independent producers:
  framing (`split_page`/`join_page`), prefix-compressed group codec, contract
  constants, directory-structure validation (`validate_directory`) and the
  strict per-segment decoder (`decode_group_checked`: CRC is checked by the
  caller; boundaries, count, first key, prefix legality, unique ordered keys,
  key length/UTF-8 and value length are checked here).
- `storage/writer.py` — `PageWriter`. Edits decode only the one or two
  restart segments they touch; untouched segments keep their original bytes
  and are relocated verbatim, with offsets re-laid contiguously. The writer
  ledger (`PageWriter.report`) records `decoded`, `reencoded`, `moved`,
  `payload_bytes`, `page_bytes` per operation.
- `query/reader.py` — `PageReader`. Only the directory is parsed at
  construction. `get`/`range` decode at most the located segment (eight
  records) plus later intersecting segments for ranges; `audit` sequentially
  visits every segment. `inspect()` reports `groups_read`, `decoded` and
  `bytes_read` (directory parsing is never counted as record decoding).

## API

- `PageWriter().create(rows, restart=4)` / `.put(raw, key, value)` /
  `.delete(raw, key)` / `.rename(raw, old_key, new_key)` — each returns a
  complete new serialized page, or the `PAGE_FULL` sentinel when the result
  would exceed 4096 bytes or 128 unique keys (the input bytes stay readable).
  `delete` on an unknown key and `rename` with a missing old key or an
  existing new key raise `KeyError`.
- `PageWriter.save(raw, path)` / `PageWriter.open(path)` and
  `PageReader.open(path)` — save a returned page and reopen the exact bytes
  through the independent reader.
- `PageReader(raw).get(key)` (returns `None` when absent),
  `.range(start, end)` for the `[start, end)` raw-byte interval (empty when
  `start >= end`), `.audit()` (full sequential verification, returns all
  rows) and `.inspect()`. Corrupt segments raise
  `storage.format.PageFormatError` / `query.reader.CorruptPage`.

## Re-runnable checks

- `python3 probe.py` — the original four scenarios.
- `python3 verify.py` — 15 contract scenarios. `verify.wire_build` /
  `wire_parse` are an independent producer/oracle written straight from the
  format spec (they never call the production classes). Real pages land in
  `artifacts/pages/` and the full metrics ledger in
  `artifacts/metrics.json`.
- `python3 fuzz.py` — 4000 randomized put/delete/rename steps with
  independent full-page re-parse and file-reopen verification after every
  step.

On the 64 short-key benchmark page, an equal-length value replacement
decodes/re-encodes exactly one 8-record segment (budget: 16) and moves the
other 56 records byte-for-byte.
