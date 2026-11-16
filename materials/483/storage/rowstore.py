"""A small, crash-consistent row store backed by a single JSON document.

Only base and stored column values are written for a row.  Virtual columns are
recomputed whenever a logical row is needed (projection, scan, or an index
key).  The schema plan, the physical layout and a content identity for the
index directory are persisted alongside the rows so that ``open`` can refuse a
mismatched or corrupt database instead of silently presenting it as intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from schema import Compiler, CompiledSchema
from schema.compiler import canonical, fingerprint

from .index import SecondaryIndex

FORMAT = "rowstore-json-1"
MAX_OPERATIONS = 100
MAX_INDEXES = 6


def _normalize_plan(plan: Any) -> CompiledSchema:
    if isinstance(plan, CompiledSchema):
        return plan
    if isinstance(plan, (dict, str)):
        return Compiler().load(plan)
    raise ValueError("plan must be a CompiledSchema or a schema dump payload")


def _normalize_index_definitions(plan: CompiledSchema, indexes: Any) -> List[dict]:
    if not isinstance(indexes, list):
        raise ValueError("indexes must be a list")
    if len(indexes) > MAX_INDEXES:
        raise ValueError(f"at most {MAX_INDEXES} indexes are allowed")

    normalized: List[dict] = []
    names = set()
    for position, raw in enumerate(indexes):
        where = f"index {position}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: definition must be an object")
        if set(raw) != {"name", "columns", "unique"}:
            raise ValueError(f"{where}: exactly name, columns and unique are required")
        name = raw["name"]
        if not isinstance(name, str) or not name:
            raise ValueError(f"{where}: name must be a non-empty string")
        if name in names:
            raise ValueError(f"{where}: duplicate index name {name!r}")
        names.add(name)
        columns = raw["columns"]
        if not isinstance(columns, list) or len(columns) not in (1, 2):
            raise ValueError(f"{where}: columns must contain one or two names")
        if len(set(columns)) != len(columns):
            raise ValueError(f"{where}: columns must be distinct")
        for column in columns:
            if not isinstance(column, str) or column not in plan.by_name:
                raise ValueError(f"{where}: unknown column {column!r}")
        if not isinstance(raw["unique"], bool):
            raise ValueError(f"{where}: unique must be a boolean")
        normalized.append({"name": name, "columns": list(columns), "unique": raw["unique"]})
    return normalized


def _entries_snapshot(indexes: Dict[str, SecondaryIndex]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {}
    for name in sorted(indexes):
        index = indexes[name]
        entries = []
        for key in sorted(index.mapping, key=lambda item: canonical(list(item))):
            entries.append({"key": list(key), "ids": sorted(index.mapping[key])})
        snapshot[name] = entries
    return snapshot


def _entries_fingerprint(indexes: Dict[str, SecondaryIndex]) -> str:
    return fingerprint(_entries_snapshot(indexes))


def _storage_identity(plan: CompiledSchema, definitions_fingerprint: str) -> str:
    return fingerprint(
        {
            "schema": plan.fingerprint,
            "layout": plan.layout_fingerprint,
            "indexes": definitions_fingerprint,
        }
    )


class RowStore:
    def __init__(
        self,
        path: Path,
        plan: CompiledSchema,
        index_definitions: List[dict],
        physical_rows: Dict[str, dict],
    ):
        self.path = Path(path)
        self.plan = plan
        self.index_definitions = [dict(d) for d in index_definitions]
        self.physical_rows: Dict[str, dict] = {
            row_id: dict(row) for row_id, row in physical_rows.items()
        }
        self.indexes: Dict[str, SecondaryIndex] = {
            definition["name"]: SecondaryIndex(definition, plan)
            for definition in self.index_definitions
        }
        self.business_reads = 0
        self.index_reads = 0
        for row_id, physical in self.physical_rows.items():
            logical = plan.check_physical_row(physical)
            for index in self.indexes.values():
                index.add(row_id, index.key_for(logical))

    # ------------------------------------------------------------------
    # Construction / persistence
    # ------------------------------------------------------------------
    @classmethod
    def create(cls, path, plan, indexes) -> "RowStore":
        plan = _normalize_plan(plan)
        definitions = _normalize_index_definitions(plan, indexes)
        store = cls(Path(path), plan, definitions, {})
        store._persist()
        return store

    @classmethod
    def open(cls, path) -> "RowStore":
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read row store") from exc

        if not isinstance(payload, dict):
            raise ValueError("row store payload must be an object")
        required = {"format", "schema", "layout", "index_directory", "storage_fingerprint", "rows"}
        if set(payload) != required:
            raise ValueError("row store payload has missing or unknown fields")
        if payload["format"] != FORMAT:
            raise ValueError("unsupported row store format")

        plan = Compiler().load(payload["schema"])

        layout = payload["layout"]
        if not isinstance(layout, dict) or "fingerprint" not in layout:
            raise ValueError("invalid layout record")
        if layout["fingerprint"] != plan.layout_fingerprint:
            raise ValueError("storage layout fingerprint mismatch")
        if canonical({k: v for k, v in layout.items() if k != "fingerprint"}) != canonical(plan.layout):
            raise ValueError("storage layout does not match compiled schema")

        directory = payload["index_directory"]
        if not isinstance(directory, dict):
            raise ValueError("invalid index directory")
        if set(directory) != {"definitions", "definitions_fingerprint", "entries_fingerprint"}:
            raise ValueError("invalid index directory fields")
        definitions = _normalize_index_definitions(plan, directory["definitions"])
        definitions_fingerprint = fingerprint(definitions)
        if directory["definitions_fingerprint"] != definitions_fingerprint:
            raise ValueError("index directory fingerprint mismatch")
        if payload["storage_fingerprint"] != _storage_identity(plan, definitions_fingerprint):
            raise ValueError("storage identity mismatch")

        rows = payload["rows"]
        if not isinstance(rows, dict):
            raise ValueError("rows must be an object")
        for row_id, physical in rows.items():
            if not isinstance(row_id, str) or not row_id:
                raise ValueError("row ids must be non-empty strings")
            if not isinstance(physical, dict):
                raise ValueError(f"row {row_id!r} must be an object")

        store = cls(path, plan, definitions, rows)
        if _entries_fingerprint(store.indexes) != directory["entries_fingerprint"]:
            raise ValueError("index directory content mismatch")
        return store

    def _persist(self) -> None:
        definitions = sorted(
            (dict(d) for d in self.index_definitions), key=lambda item: item["name"]
        )
        definitions_fingerprint = fingerprint(definitions)
        payload = {
            "format": FORMAT,
            "schema": self.plan.dump(),
            "layout": dict(self.plan.layout, fingerprint=self.plan.layout_fingerprint),
            "index_directory": {
                "definitions": definitions,
                "definitions_fingerprint": definitions_fingerprint,
                "entries_fingerprint": _entries_fingerprint(self.indexes),
            },
            "storage_fingerprint": _storage_identity(self.plan, definitions_fingerprint),
            "rows": {
                row_id: dict(row)
                for row_id, row in sorted(self.physical_rows.items())
            },
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def _evaluate_operation(self, operation: Any, rows: Dict[str, dict],
                            indexes: Dict[str, SecondaryIndex]) -> None:
        if not isinstance(operation, dict):
            raise ValueError("operation must be an object")
        if set(operation) - {"op", "id", "base"}:
            raise ValueError("operation has unknown fields")
        if "op" not in operation or "id" not in operation:
            raise ValueError("operation requires op and id")
        op = operation["op"]
        row_id = operation["id"]
        if not isinstance(row_id, str) or not row_id:
            raise ValueError("id must be a non-empty string")
        base_patch = operation.get("base", {})
        if not isinstance(base_patch, dict):
            raise ValueError("base must be an object")

        if op == "insert":
            if row_id in rows:
                raise ValueError(f"duplicate id {row_id!r}")
            # preview() enforces that only base columns are supplied.
            logical = self.plan.preview(base_patch)
            rows[row_id] = self.plan.physical_row(logical)
            for index in indexes.values():
                index.add(row_id, index.key_for(logical))
            return

        if op == "patch":
            if row_id not in rows:
                raise ValueError(f"unknown id {row_id!r}")
            old_logical = self.plan.check_physical_row(rows[row_id])
            # Omitted base fields retain their old value; an explicit NULL
            # replaces the old value.
            base = {name: old_logical[name] for name in self.plan.base_columns}
            base.update(base_patch)
            logical = self.plan.preview(base)
            new_physical = self.plan.physical_row(logical)

            old_keys = {name: index.key_for(old_logical) for name, index in indexes.items()}
            new_keys = {name: index.key_for(logical) for name, index in indexes.items()}
            for name, index in indexes.items():
                index.remove(row_id, old_keys[name])
            for name, index in indexes.items():
                index.add(row_id, new_keys[name])
            rows[row_id] = new_physical
            return

        raise ValueError(f"unknown operation {op!r}")

    def apply(self, operations: List[dict]) -> None:
        """Apply a batch atomically, in order.

        The batch is limited to 100 operations.  Either every operation is
        reflected in both rows and indexes, or nothing is changed.
        """

        if not isinstance(operations, list):
            raise ValueError("operations must be a list")
        if len(operations) > MAX_OPERATIONS:
            raise ValueError(f"at most {MAX_OPERATIONS} operations are allowed per batch")

        staged_rows = {row_id: dict(row) for row_id, row in self.physical_rows.items()}
        staged_indexes = {name: index.copy() for name, index in self.indexes.items()}
        for operation in operations:
            # A failure leaves self.physical_rows and self.indexes untouched.
            self._evaluate_operation(operation, staged_rows, staged_indexes)

        # The in-memory state is swapped only after every operation succeeded;
        # persistence is a single atomic replacement.
        previous_rows = self.physical_rows
        previous_indexes = self.indexes
        self.physical_rows = staged_rows
        self.indexes = staged_indexes
        try:
            self._persist()
        except Exception:
            self.physical_rows = previous_rows
            self.indexes = previous_indexes
            raise

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def _logical_row(self, row_id: str) -> dict:
        self.business_reads += 1
        return self.plan.check_physical_row(self.physical_rows[row_id])

    def query(self, index_name, operation, key, projection=None):
        if index_name not in self.indexes:
            raise ValueError(f"unknown index {index_name!r}")
        if projection is not None:
            if not isinstance(projection, (list, tuple)):
                raise ValueError("projection must be a list of column names")
            for name in projection:
                if name not in self.plan.by_name:
                    raise ValueError(f"unknown projection column {name!r}")

        row_ids, examined = self.indexes[index_name].find(operation, key)
        self.index_reads += examined

        result = []
        for row_id in row_ids:
            logical = self._logical_row(row_id)
            if projection is None:
                row = logical
            else:
                row = {name: logical[name] for name in projection}
            result.append({"id": row_id, "row": row})
        return result

    def scan(self):
        self.business_reads += len(self.physical_rows)
        result = []
        for row_id in sorted(self.physical_rows):
            result.append(
                {"id": row_id, "row": self.plan.check_physical_row(self.physical_rows[row_id])}
            )
        return result

    def inspect(self):
        definitions = sorted(
            (dict(d) for d in self.index_definitions), key=lambda item: item["name"]
        )
        definitions_fingerprint = fingerprint(definitions)
        return {
            "format": FORMAT,
            "schema_fingerprint": self.plan.fingerprint,
            "layout_fingerprint": self.plan.layout_fingerprint,
            "storage_fingerprint": _storage_identity(self.plan, definitions_fingerprint),
            "physical_columns": list(self.plan.physical_columns),
            "physical_rows": {
                row_id: dict(row) for row_id, row in sorted(self.physical_rows.items())
            },
            "index_directory": {
                "definitions": definitions,
                "definitions_fingerprint": definitions_fingerprint,
                "entries_fingerprint": _entries_fingerprint(self.indexes),
                "entries": _entries_snapshot(self.indexes),
            },
            "business_reads": self.business_reads,
            "index_reads": self.index_reads,
        }

    def close(self) -> None:
        self._persist()
