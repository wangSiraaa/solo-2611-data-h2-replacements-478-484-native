"""Real Wasmtime host for the batch ABI.

Each ``run`` call creates a fresh ``Store`` and ``Instance``.  The host
validates all inputs and the module interface *before* invoking the guest,
sets the fuel budget once for the whole job (never refills per batch), and
records every actual guest call together with memory-page and fuel changes.
Verified per-batch results are staged in :class:`product.Product`; only a
fully successful job is atomically published as ``complete``.
"""
from __future__ import annotations

from pathlib import Path

import wasmtime

from abi import decode_image, encode
from product import Product, ProductError
from product.store import MAX_BATCHES, MAX_JOB_BYTES, MAX_ROWS_PER_BATCH, MAX_TEXT_BYTES

_I32 = wasmtime.ValType.i32()


class JobFailure(Exception):
    """A failed job stage, carrying the underlying cause."""

    def __init__(self, stage, cause):
        super().__init__(f'{stage}: {cause}')
        self.stage = stage
        self.cause = cause


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Runner:
    def __init__(self):
        cfg = wasmtime.Config()
        cfg.consume_fuel = True
        cfg.wasm_threads = False
        cfg.wasm_memory64 = False
        cfg.wasm_multi_memory = False
        self.engine = wasmtime.Engine(cfg)

    # -- pre-flight validation ------------------------------------------
    @staticmethod
    def _validate_inputs(batches, fuel, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise JobFailure('validate-input', ProductError('job_id must be a non-empty string'))
        if not _is_int(fuel) or fuel <= 0:
            raise JobFailure('validate-input', ProductError('fuel must be a positive integer'))
        if not isinstance(batches, (list, tuple)):
            raise JobFailure('validate-input', ProductError('batches must be a sequence'))
        if not (1 <= len(batches) <= MAX_BATCHES):
            raise JobFailure('validate-input',
                             ProductError(f'job must have 1..{MAX_BATCHES} batches'))
        total = 0
        for n, rows in enumerate(batches):
            if not isinstance(rows, (list, tuple)):
                raise JobFailure('validate-input', ProductError(f'batch {n} rows must be a sequence'))
            if len(rows) > MAX_ROWS_PER_BATCH:
                raise JobFailure('validate-input',
                                 ProductError(f'batch {n} has {len(rows)} rows, '
                                              f'limit is {MAX_ROWS_PER_BATCH}'))
            for index, value in enumerate(rows):
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise JobFailure('validate-input',
                                     ProductError(f'batch {n} row {index} must be None or str'))
                try:
                    size = len(value.encode('utf-8'))
                except UnicodeEncodeError as exc:
                    raise JobFailure('validate-input',
                                     ProductError(f'batch {n} row {index} is not valid UTF-8: {exc}'))
                if size > MAX_TEXT_BYTES:
                    raise JobFailure('validate-input',
                                     ProductError(f'batch {n} row {index} is {size} bytes, '
                                                  f'limit is {MAX_TEXT_BYTES}'))
                total += size
        if total > MAX_JOB_BYTES:
            raise JobFailure('validate-input',
                             ProductError(f'job text is {total} bytes, limit is {MAX_JOB_BYTES}'))

    @staticmethod
    def _validate_module(module):
        try:
            imports = list(module.imports)
        except Exception as exc:  # pragma: no cover - defensive
            raise JobFailure('module-check', exc)
        if imports:
            names = ', '.join(f'{i.module}::{i.name}' for i in imports)
            raise JobFailure('module-check', ProductError(f'module has imports: {names}'))

        exports = {e.name: e.type for e in module.exports}
        required = {'alloc', 'dealloc', 'process', 'release', 'memory'}
        missing = required - exports.keys()
        if missing:
            raise JobFailure('module-check',
                             ProductError('missing exports: ' + ', '.join(sorted(missing))))

        memtype = exports['memory']
        if not isinstance(memtype, wasmtime.MemoryType):
            raise JobFailure('module-check', ProductError('export "memory" is not a memory'))
        if memtype.is_64:
            raise JobFailure('module-check', ProductError('memory64 is not supported'))
        if memtype.is_shared:
            raise JobFailure('module-check', ProductError('shared memory is not supported'))
        limits = memtype.limits
        if limits.min > 4:
            raise JobFailure('module-check',
                             ProductError(f'initial memory {limits.min} pages exceeds 4'))
        if limits.max is None:
            raise JobFailure('module-check',
                             ProductError('memory must declare a maximum of at most 4 pages'))
        if limits.max > 4:
            raise JobFailure('module-check',
                             ProductError(f'maximum memory {limits.max} pages exceeds 4'))

        signatures = {
            'alloc': ([_I32], [_I32]),
            'dealloc': ([_I32, _I32], []),
            'process': ([_I32, _I32, _I32], [_I32]),
            'release': ([_I32], []),
        }
        for name, (params, results) in signatures.items():
            ftype = exports[name]
            if not isinstance(ftype, wasmtime.FuncType):
                raise JobFailure('module-check', ProductError(f'export "{name}" is not a function'))
            if list(ftype.params) != params or list(ftype.results) != results:
                raise JobFailure('module-check',
                                 ProductError(f'export "{name}" has the wrong signature'))

    # -- invocation with evidence ---------------------------------------
    @staticmethod
    def _invoke(calls, name, func, store, memory, *args, stage=None):
        try:
            pages_before = memory.size(store)
            fuel_before = store.get_fuel()
        except Exception as exc:  # pragma: no cover - defensive
            raise JobFailure(stage or f'{name}-trap', exc)
        try:
            result = func(store, *args)
        except Exception as exc:
            record = {'call': name, 'args': list(args), 'status': 'trap',
                      'error': type(exc).__name__,
                      'pages_before': pages_before}
            try:
                record['pages_after'] = memory.size(store)
            except Exception:
                record['pages_after'] = None
            try:
                record['fuel_before'] = fuel_before
                record['fuel_after'] = store.get_fuel()
            except Exception:
                record['fuel_after'] = None
            calls.append(record)
            raise JobFailure(stage or f'{name}-trap', exc)
        record = {'call': name, 'args': list(args), 'status': 'ok', 'result': result,
                  'pages_before': pages_before, 'pages_after': memory.size(store),
                  'fuel_before': fuel_before, 'fuel_after': store.get_fuel()}
        calls.append(record)
        return result

    @staticmethod
    def _check_address(name, address, data_len, need=0):
        if not _is_int(address) or address < 0:
            raise JobFailure(name, ProductError(f'guest returned negative address {address}'))
        if address + need > data_len:
            raise JobFailure(name,
                             ProductError(f'guest returned address {address}'
                                          f' (+{need}) beyond memory size {data_len}'))

    # -- job driver ------------------------------------------------------
    def run(self, wat, batches, path, fuel=100000, job_id='job'):
        calls = []
        product = Product(path)
        store = None
        instance = None
        memory = None

        slab_addr = None
        slab_len = 0
        slab_active = False
        out_result = None
        out_pending = False
        stage = 'validate-input'

        try:
            self._validate_inputs(batches, fuel, job_id)

            stage = 'begin'
            product.begin(job_id, [len(rows) for rows in batches])

            stage = 'module-check'
            source = wat
            if not isinstance(wat, (str, bytes, bytearray)):
                source = Path(wat).read_bytes()
            module = wasmtime.Module(self.engine, source)
            self._validate_module(module)

            stage = 'instantiate'
            store = wasmtime.Store(self.engine)
            store.set_fuel(fuel)
            instance = wasmtime.Instance(store, module, [])
            exports = instance.exports(store)
            memory = exports['memory']
            alloc = exports['alloc']
            dealloc = exports['dealloc']
            process = exports['process']
            release = exports['release']

            for n, rows in enumerate(batches):
                final = n == len(batches) - 1

                stage = f'batch{n}:encode'
                provisional = encode(rows, n, final, 0)
                slab_len = len(provisional)

                stage = f'batch{n}:alloc'
                address = self._invoke(calls, 'alloc', alloc, store, memory, slab_len,
                                       stage=stage)
                self._check_address(f'batch{n}:alloc', address,
                                    memory.data_len(store), slab_len)
                slab_addr = address
                slab_active = True

                stage = f'batch{n}:write'
                packed = encode(rows, n, final, address)
                if len(packed) != slab_len:
                    raise JobFailure(stage, ProductError('slab length changed between encodes'))
                memory.write(store, packed, address)

                # Validate exactly what the guest will read, from the current
                # memory, before the guest is called.
                stage = f'batch{n}:input-validate'
                snapshot = memory.read(store, 0, memory.data_len(store))
                decode_image(snapshot, address,
                             {'batch_no': n, 'row_count': len(rows), 'final': final})

                stage = f'batch{n}:process'
                result = self._invoke(calls, 'process', process, store, memory,
                                      address, n, int(final), stage=stage)
                # From here on the guest has returned an output descriptor, so
                # the host owes exactly one release attempt even if validation
                # below rejects the address or the bytes.
                data_len = memory.data_len(store)
                out_result = result
                out_pending = True
                self._check_address(f'batch{n}:output', result, data_len, 0)

                # Must re-read the *current* memory: the guest may have grown
                # it during process.  decode_image copies the rows, so the
                # returned values survive later guest activity.
                stage = f'batch{n}:output-validate'
                snapshot = memory.read(store, 0, data_len)
                batch = decode_image(snapshot, result,
                                     {'batch_no': n, 'row_count': len(rows), 'final': final})
                product.append(job_id, batch)

                stage = f'batch{n}:release'
                # The release responsibility is discharged by *attempting* it
                # once; if it traps we do not try again, we destroy the Store
                # in the failure path instead.
                out_pending = False
                self._invoke(calls, 'release', release, store, memory, result, stage=stage)

                stage = f'batch{n}:dealloc'
                # Likewise the single dealloc attempt is the host's obligation.
                slab_active = False
                self._invoke(calls, 'dealloc', dealloc, store, memory, slab_addr, slab_len,
                             stage=stage)

            stage = 'commit'
            product.commit(job_id)
            return {'status': 'complete', 'job_id': job_id,
                    'product': product.inspect(), 'calls': calls}

        except Exception as exc:
            fail_stage = exc.stage if isinstance(exc, JobFailure) else stage
            cause = exc.cause if isinstance(exc, JobFailure) else exc
            cleanup = []
            broken = False

            # Cleanup responsibilities: release a returned output once, and
            # dealloc the input slab once.  They are kept separate because a
            # successful output may alias the input.  If either traps (often
            # due to exhausted fuel) the Store is destroyed.
            if out_pending and store is not None and memory is not None:
                try:
                    self._invoke(calls, 'release-cleanup', release, store, memory, out_result)
                    out_pending = False
                except Exception as cleanup_exc:
                    broken = True
                    cause_name = (type(cleanup_exc.cause).__name__
                                  if isinstance(cleanup_exc, JobFailure)
                                  else type(cleanup_exc).__name__)
                    cleanup.append(('release', cause_name))
            if slab_active and store is not None and memory is not None:
                try:
                    self._invoke(calls, 'dealloc-cleanup', dealloc, store, memory,
                                 slab_addr, slab_len)
                    slab_active = False
                except Exception as cleanup_exc:
                    broken = True
                    cause_name = (type(cleanup_exc.cause).__name__
                                  if isinstance(cleanup_exc, JobFailure)
                                  else type(cleanup_exc).__name__)
                    cleanup.append(('dealloc', cause_name))

            store_closed = False
            if store is not None:
                try:
                    store.close()
                except Exception:  # pragma: no cover - defensive
                    pass
                store_closed = True
                store = None
                memory = None

            try:
                product.abort(job_id, f'{fail_stage}: {type(cause).__name__}: {cause}',
                              detail={'stage': fail_stage, 'calls': calls,
                                      'cleanup': cleanup,
                                      'cleanup_broken': broken})
            except Exception as abort_exc:  # pragma: no cover - defensive
                cleanup.append(('abort', type(abort_exc).__name__))

            return {
                'status': 'failed',
                'job_id': job_id,
                'stage': fail_stage,
                'reason': f'{type(cause).__name__}: {cause}',
                'cleanup': cleanup,
                'cleanup_broken': broken,
                'store_closed': store_closed,
                'calls': calls,
                'product': product.inspect(),
            }
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # pragma: no cover - defensive
                    pass
