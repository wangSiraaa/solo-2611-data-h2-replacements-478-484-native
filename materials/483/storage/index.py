"""Secondary index maintenance.

Indexes store *logical* key tuples.  A virtual column is therefore never
persisted just to support an index: its key is computed from the logical row
which the store already knows.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from schema.compiler import check_value


class SecondaryIndex:
    def __init__(self, definition: dict, plan):
        self.definition = {
            "name": definition["name"],
            "columns": list(definition["columns"]),
            "unique": definition["unique"],
        }
        self.columns: List[str] = self.definition["columns"]
        self.unique: bool = self.definition["unique"]
        self.plan = plan
        self.mapping: Dict[Tuple[Any, ...], set] = {}

    # ------------------------------------------------------------------
    def copy(self) -> "SecondaryIndex":
        clone = SecondaryIndex(self.definition, self.plan)
        clone.mapping = {key: set(ids) for key, ids in self.mapping.items()}
        return clone

    def key_for(self, logical_row: dict) -> Tuple[Any, ...]:
        return tuple(logical_row[name] for name in self.columns)

    def add(self, row_id: str, key: Tuple[Any, ...]) -> None:
        if self.unique and all(value is not None for value in key):
            existing = self.mapping.get(key)
            if existing:
                others = existing - {row_id}
                if others:
                    raise ValueError(
                        f"unique index {self.definition['name']!r} conflict on key {key!r}"
                    )
        self.mapping.setdefault(key, set()).add(row_id)

    def remove(self, row_id: str, key: Tuple[Any, ...]) -> None:
        ids = self.mapping.get(key)
        if not ids:
            return
        ids.discard(row_id)
        if not ids:
            del self.mapping[key]

    # ------------------------------------------------------------------
    def _column_type(self, name: str) -> str:
        return self.plan.by_name[name]["type"]

    def _validate_key_value(self, position: int, value: Any) -> None:
        # NULL is a queryable standalone key value, even for a non-nullable
        # column.  Non-NULL values must match the indexed column's type.
        type_name = self._column_type(self.columns[position])
        check_value(value, type_name, True, f"query key for index {self.definition['name']!r}")

    def find(self, operation: str, key: Any) -> Tuple[List[str], int]:
        """Return ``(row_ids, index_entries_examined)`` sorted by id."""

        if operation == "eq":
            if not isinstance(key, (list, tuple)):
                raise ValueError("eq key must be an array of values")
            if len(key) != len(self.columns):
                raise ValueError(
                    f"eq key for index {self.definition['name']!r} must have "
                    f"{len(self.columns)} value(s)"
                )
            values = tuple(key)
            for position, value in enumerate(values):
                self._validate_key_value(position, value)
            ids = self.mapping.get(values, set())
            return sorted(ids), 1

        if operation == "range":
            if len(self.columns) != 1:
                raise ValueError("range queries require a single-column index")
            if not isinstance(key, (list, tuple)) or len(key) != 2:
                raise ValueError("range key must be [lower, upper]")
            lower, upper = key
            if lower is None or upper is None:
                raise ValueError("range bounds must be non-NULL")
            self._validate_key_value(0, lower)
            self._validate_key_value(0, upper)
            if type(lower) is not type(upper):
                raise ValueError("range bounds must have the same type")
            examined = 0
            result = set()
            for values, ids in self.mapping.items():
                examined += 1
                value = values[0]
                if value is None:
                    continue
                if type(value) is not type(lower):
                    continue
                if lower <= value < upper:
                    result.update(ids)
            return sorted(result), examined

        raise ValueError(f"unknown query operation {operation!r}")
