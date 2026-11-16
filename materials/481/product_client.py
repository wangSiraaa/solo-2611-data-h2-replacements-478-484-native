"""Native producer using the same Product protocol without WASM."""
from product import Product


def publish(path, job_id, batches):
    """Publish ``batches`` (a list of row lists) through :class:`Product`.

    This is a real production entry point: it performs the same staged
    append/commit handshake a WASM host uses and never publishes a partial
    prefix.  Each batch is ``{'batch_no', 'final', 'rows'}``.
    """
    p = Product(path)
    p.begin(job_id, [len(x) for x in batches])
    try:
        for n, rows in enumerate(batches):
            p.append(job_id, {'batch_no': n, 'rows': rows,
                              'final': n == len(batches) - 1})
        p.commit(job_id)
    except Exception as exc:
        p.abort(job_id, f'{type(exc).__name__}: {exc}')
        raise
    return p.inspect()


if __name__ == '__main__':
    import tempfile, json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        print(json.dumps(publish(Path(td) / 'p.json', 'native', [['海', None, '']]),
                         ensure_ascii=False))
