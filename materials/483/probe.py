import json
from pathlib import Path
import tempfile
from schema import Compiler
from storage import RowStore


def columns(forward=False):
    base = {'name': 'x', 'type': 'int64', 'kind': 'base', 'nullable': True}
    y = {'name': 'y', 'type': 'int64', 'kind': 'virtual', 'nullable': True,
         'expr': {'op': '+', 'args': [{'col': 'x'}, 1]}}
    z = {'name': 'z', 'type': 'int64', 'kind': 'stored', 'nullable': True,
         'expr': {'op': '+', 'args': [{'col': 'y'}, 1]}}
    return [base, z, y] if forward else [base, y, z]


def main():
    compiler = Compiler()
    normal = compiler.compile(columns())
    print(json.dumps({'scenario': 'preview-normal', 'actual': normal.preview({'x': 2}),
                      'expected': {'x': 2, 'y': 3, 'z': 4}}))
    forward = compiler.compile(columns(True))
    print(json.dumps({'scenario': 'preview-forward', 'actual': forward.preview({'x': 2}),
                      'expected': {'x': 2, 'y': 3, 'z': 4}}))
    bad = [{'name': 'v', 'type': 'int64', 'kind': 'stored', 'nullable': True,
            'expr': {'col': 'missing'}}]
    try:
        compiler.compile(bad)
        invalid = 'accepted'
    except Exception as exc:
        invalid = type(exc).__name__
    print(json.dumps({'scenario': 'unknown-column', 'actual': invalid, 'expected': 'rejected'}))
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'rows.json'
        store = RowStore.create(path, normal, [{'name': 'by_y', 'columns': ['y'], 'unique': False}])
        store.apply([{'op': 'insert', 'id': 'a', 'base': {'x': 2}}])
        store.close()
        reopened = RowStore.open(path)
        print(json.dumps({'scenario': 'storage-reopen-normal', 'actual': reopened.query('by_y', 'eq', [3]),
                          'expected_ids': ['a']}))
        reopened.apply([{'op': 'patch', 'id': 'a', 'base': {'x': 8}}])
        print(json.dumps({'scenario': 'stale-index', 'actual': reopened.query('by_y', 'eq', [3]),
                          'expected': []}))
        physical = json.loads(path.read_text())['rows']['a']
        print(json.dumps({'scenario': 'virtual-on-disk', 'physical_columns': sorted(physical),
                          'expected_physical_columns': ['x', 'z']}))


if __name__ == '__main__':
    main()
