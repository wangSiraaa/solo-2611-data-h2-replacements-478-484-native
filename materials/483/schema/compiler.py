import hashlib
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def evaluate(expr, row):
    if isinstance(expr, (int, str)) or expr is None:
        return expr
    if 'col' in expr:
        return row.get(expr['col'])
    a = evaluate(expr['args'][0], row)
    b = evaluate(expr['args'][1], row)
    if expr['op'] == 'coalesce':
        return b if a is None else a
    if a is None or b is None:
        return None
    if expr['op'] == '+':
        return a + b
    if expr['op'] == '-':
        return a - b
    if expr['op'] == 'concat':
        return str(a) + str(b)
    raise ValueError(expr)


class CompiledSchema:
    def __init__(self, columns):
        self.columns = json.loads(canonical(columns))
        self.fingerprint = hashlib.sha256(canonical(self.columns).encode()).hexdigest()

    def preview(self, base):
        row = dict(base)
        for column in self.columns:
            name = column['name']
            if column['kind'] == 'base':
                row.setdefault(name, column.get('default'))
            else:
                row[name] = evaluate(column['expr'], row)
        return row

    def dump(self):
        return {'columns': self.columns, 'fingerprint': self.fingerprint}


class Compiler:
    def compile(self, columns):
        names = [c['name'] for c in columns]
        if len(set(names)) != len(names):
            raise ValueError('duplicate column')
        return CompiledSchema(columns)

    def load(self, payload):
        plan = self.compile(payload['columns'])
        if plan.fingerprint != payload['fingerprint']:
            raise ValueError('schema fingerprint mismatch')
        return plan
