"""Public batch-product writer shared by native and WASM producers.

Publication model
-----------------
A job's rows and receipts are staged in memory.  ``commit`` validates the
whole-job handshake and then writes a single JSON artifact to a temporary
file in the *same* directory and ``os.replace``s it onto the final path.
``inspect`` only ever reads the final path, so a reader observes either no
artifact or one fully committed ``status=complete`` document -- never a
prefix.  Failures may leave a sibling ``<name>.diag.json`` diagnostic, which
``inspect`` reports as ``failed`` but which is never the public artifact.

This was chosen over writing per-batch files: the public read boundary is a
single atomic rename (no directory scan, no partially-linked set), and the
cleanup cost of a failure is one small in-memory buffer plus at most one
diagnostic file.  A complete artifact is never overwritten: ``begin`` on a
path that already holds a complete version is rejected outright.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

MAX_BATCHES = 4
MAX_ROWS_PER_BATCH = 128
MAX_TEXT_BYTES = 256
MAX_JOB_BYTES = 64 * 1024


class ProductError(ValueError):
    """Raised when a product operation breaks the batch protocol."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_len(value):
    try:
        return len(value.encode('utf-8'))
    except UnicodeEncodeError as exc:
        raise ProductError(f'row is not valid UTF-8: {exc}') from exc


def _validate_rows(rows):
    if not isinstance(rows, (list, tuple)):
        raise ProductError('rows must be a list')
    if len(rows) > MAX_ROWS_PER_BATCH:
        raise ProductError(f'row count {len(rows)} exceeds {MAX_ROWS_PER_BATCH}')
    total = 0
    for index, value in enumerate(rows):
        if value is None:
            continue
        if not isinstance(value, str):
            raise ProductError(f'row {index} must be None or str')
        size = _utf8_len(value)
        if size > MAX_TEXT_BYTES:
            raise ProductError(f'row {index} is {size} bytes, limit is {MAX_TEXT_BYTES}')
        total += size
    if total > MAX_JOB_BYTES:
        raise ProductError(f'batch text is {total} bytes, job limit is {MAX_JOB_BYTES}')
    return total


def _atomic_write_json(path: Path, payload: dict):
    """Write ``payload`` to ``path`` with a same-directory atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class Product:
    """Staged, all-or-nothing batch product for one job at ``path``."""

    def __init__(self, path):
        self.path = Path(path)
        self._diag_path = self.path.with_name(self.path.name + '.diag.json')
        self.job_id = None
        self.row_counts = []
        self.rows = []
        self.receipts = []
        self._seen = set()
        self._committed = False
        self._aborted = False

    # -- helpers ---------------------------------------------------------
    def _final_status(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'status': 'failed', 'reason': 'unreadable artifact'}
        if not isinstance(data, dict):
            return {'status': 'failed', 'reason': 'malformed artifact'}
        return data

    def _has_complete(self):
        existing = self._final_status()
        return bool(existing and existing.get('status') == 'complete')

    def _require_active(self):
        if self.job_id is None:
            raise ProductError('begin must be called before append/commit/abort')
        if self._committed:
            raise ProductError('product is already committed')
        if self._aborted:
            raise ProductError('product was aborted')

    # -- public protocol -------------------------------------------------
    def begin(self, job_id, row_counts):
        if not isinstance(job_id, str) or not job_id:
            raise ProductError('job_id must be a non-empty string')
        if self._has_complete():
            raise ProductError(f'refusing to overwrite complete artifact {self.path}')
        if not isinstance(row_counts, (list, tuple)):
            raise ProductError('row_counts must be a sequence')
        if not (1 <= len(row_counts) <= MAX_BATCHES):
            raise ProductError(f'job must have 1..{MAX_BATCHES} batches')
        counts = []
        for index, count in enumerate(row_counts):
            if not _is_int(count) or not (0 <= count <= MAX_ROWS_PER_BATCH):
                raise ProductError(
                    f'row_counts[{index}] must be an integer in 0..{MAX_ROWS_PER_BATCH}')
            counts.append(count)
        self.job_id = job_id
        self.row_counts = counts
        self.rows = []
        self.receipts = []
        self._seen = set()
        self._committed = False
        self._aborted = False
        # A previous failure diagnostic must not masquerade as this run.
        try:
            self._diag_path.unlink()
        except FileNotFoundError:
            pass
        return self.inspect()

    def append(self, job_id, batch):
        self._require_active()
        if job_id != self.job_id:
            raise ProductError(f'job_id mismatch: {job_id!r} vs {self.job_id!r}')
        if not isinstance(batch, dict):
            raise ProductError('batch must be a mapping')
        for key in ('batch_no', 'final', 'rows'):
            if key not in batch:
                raise ProductError(f'batch is missing {key!r}')
        batch_no = batch['batch_no']
        final = batch['final']
        rows = batch['rows']
        if not _is_int(batch_no):
            raise ProductError('batch_no must be an integer')
        if batch_no in self._seen:
            raise ProductError(f'duplicate batch_no {batch_no}')
        expected_no = len(self.receipts)
        if batch_no != expected_no:
            raise ProductError(f'expected batch_no {expected_no}, got {batch_no}')
        if not isinstance(final, bool):
            if _is_int(final) and final in (0, 1):
                final = bool(final)
            else:
                raise ProductError('final must be a boolean')
        expected_final = batch_no == len(self.row_counts) - 1
        if final != expected_final:
            raise ProductError(
                f'batch {batch_no} has final={final}, expected {expected_final}')
        if len(rows) != self.row_counts[batch_no]:
            raise ProductError(
                f'batch {batch_no} has {len(rows)} rows, declared {self.row_counts[batch_no]}')
        already = sum(_utf8_len(r) for r in self.rows if r is not None)
        added = _validate_rows(rows)
        if already + added > MAX_JOB_BYTES:
            raise ProductError(
                f'job text would be {already + added} bytes, limit is {MAX_JOB_BYTES}')

        self._seen.add(batch_no)
        self.rows.extend(rows)
        self.receipts.append({
            'batch_no': batch_no,
            'final': final,
            'row_count': len(rows),
        })
        return self.inspect()

    def commit(self, job_id):
        self._require_active()
        if job_id != self.job_id:
            raise ProductError(f'job_id mismatch: {job_id!r} vs {self.job_id!r}')
        if len(self.receipts) != len(self.row_counts):
            raise ProductError(
                f'cannot commit: have {len(self.receipts)} of {len(self.row_counts)} batches')
        for index, receipt in enumerate(self.receipts):
            if receipt['batch_no'] != index:
                raise ProductError(f'batch {index} missing or out of order')
            expected_final = index == len(self.row_counts) - 1
            if receipt['final'] != expected_final:
                raise ProductError(f'batch {index} FINAL handshake is wrong')
        if self._has_complete():
            raise ProductError(f'refusing to overwrite complete artifact {self.path}')

        payload = {
            'status': 'complete',
            'job_id': self.job_id,
            'row_counts': list(self.row_counts),
            'rows': list(self.rows),
            'batches': list(self.receipts),
        }
        _atomic_write_json(self.path, payload)
        self._committed = True
        try:
            self._diag_path.unlink()
        except FileNotFoundError:
            pass
        return self.inspect()

    def abort(self, job_id, reason, detail=None):
        if self.job_id is not None and job_id != self.job_id:
            raise ProductError(f'job_id mismatch: {job_id!r} vs {self.job_id!r}')
        if job_id is None:
            raise ProductError('abort requires the job_id')
        # Never touch an existing complete artifact: a failed rerun must not
        # rewrite a previously successful version.
        if self._has_complete():
            return {'status': 'complete', 'reason': 'existing complete artifact preserved'}
        diagnostic = {
            'status': 'failed',
            'job_id': job_id,
            'reason': str(reason),
            'row_counts': list(self.row_counts),
            'batches': list(self.receipts),
        }
        if detail is not None:
            diagnostic['detail'] = detail
        _atomic_write_json(self._diag_path, diagnostic)
        self._aborted = True
        self._committed = False
        return self.inspect()

    def inspect(self):
        existing = self._final_status()
        if existing and existing.get('status') == 'complete':
            return existing
        if self._diag_path.exists() and not self._committed:
            try:
                diag = json.loads(self._diag_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {'status': 'failed', 'reason': 'unreadable diagnostic'}
            if isinstance(diag, dict):
                return diag
        if existing is not None:
            return existing
        return {'status': 'unpublished'}
