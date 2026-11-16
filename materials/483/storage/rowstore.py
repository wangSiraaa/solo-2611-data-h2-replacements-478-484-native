import json
from pathlib import Path
from schema import Compiler
from .index import SecondaryIndex


class RowStore:
    def __init__(self, path, plan, indexes, rows=None):
        self.path = Path(path)
        self.plan = plan
        self.rows = dict(rows or {})
        self.indexes = {d['name']: SecondaryIndex(d) for d in indexes}
        self.business_reads = 0
        for row_id, row in self.rows.items():
            for index in self.indexes.values():
                index.add(row_id, row)

    @classmethod
    def create(cls, path, plan, indexes):
        store = cls(path, plan, indexes)
        store._save()
        return store

    @classmethod
    def open(cls, path):
        saved = json.loads(Path(path).read_text())
        return cls(path, Compiler().load(saved['schema']), saved['indexes'], saved['rows'])

    def _save(self):
        payload = {'schema': self.plan.dump(), 'indexes': [i.definition for i in self.indexes.values()],
                   'rows': self.rows}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def apply(self, operations):
        for operation in operations:
            row_id = operation['id']
            if operation['op'] == 'insert':
                if row_id in self.rows:
                    raise ValueError('duplicate id')
                base = operation['base']
            elif operation['op'] == 'patch':
                old = self.rows[row_id]
                base = {c['name']: old.get(c['name']) for c in self.plan.columns if c['kind'] == 'base'}
                base.update(operation['base'])
            else:
                raise ValueError(operation['op'])
            row = self.plan.preview(base)
            self.rows[row_id] = row
            for index in self.indexes.values():
                index.add(row_id, row)
            self._save()

    def query(self, index_name, operation, key, projection=None):
        ids = self.indexes[index_name].find(operation, key)
        self.business_reads += len(ids)
        result = []
        for row_id in ids:
            row = dict(self.rows[row_id])
            if projection is not None:
                row = {name: row[name] for name in projection}
            result.append({'id': row_id, 'row': row})
        return result

    def scan(self):
        self.business_reads += len(self.rows)
        return [{'id': k, 'row': dict(v)} for k, v in sorted(self.rows.items())]

    def inspect(self):
        return {'schema_fingerprint': self.plan.fingerprint,
                'physical_rows': self.rows, 'business_reads': self.business_reads}

    def close(self):
        self._save()
