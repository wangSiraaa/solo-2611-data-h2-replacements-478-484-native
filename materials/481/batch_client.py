"""Native memory-image consumer: it does not start a WASM Runner.

``consume`` is the standalone production entry for clients that already have
a snapshot of linear memory.  It delegates all validation to the shared ABI
decoder, so the native path and the WASM host enforce exactly the same
contract.
"""
from abi import decode_image


def consume(image, address, expected):
    return decode_image(image, address, expected)


if __name__ == '__main__':
    import json
    from abi import encode
    raw = encode(['海', None, ''], 0, True, 0)
    print(json.dumps(consume(raw, 0, {'batch_no': 0, 'row_count': 3, 'final': True}),
                     ensure_ascii=False))
