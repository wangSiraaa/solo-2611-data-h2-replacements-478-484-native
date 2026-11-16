class SecondaryIndex:
    def __init__(self, definition):
        self.definition = definition
        self.mapping = {}

    def add(self, row_id, row):
        key = tuple(row.get(c) for c in self.definition['columns'])
        self.mapping.setdefault(key, set()).add(row_id)

    def find(self, operation, key):
        key = tuple(key)
        if operation == 'eq':
            return sorted(self.mapping.get(key, set()))
        if operation == 'range':
            low, high = key
            return sorted({row_id for k, ids in self.mapping.items()
                           if low <= k[0] < high for row_id in ids})
        raise ValueError(operation)
