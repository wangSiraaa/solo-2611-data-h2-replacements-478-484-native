"""Catalog production and identity.

describe(db) inspects the real database and returns a plain JSON-able catalog.
identity(catalog) is the canonical sha256 embedded in every BoundPlan.
resolve_target maps a *validated* request target onto a unique index that
actually exists in the catalog; no algebraic guessing is performed here or
in the compiler.
"""
import hashlib
import json
import re

from .errors import BindingError

COLUMNS = {
    "id": "int64",
    "tenant": "text",
    "email": "nullable_text",
    "active": "bool01",
    "external_id": "text",
    "n": "int64",
}

# Canonical target names required by the contract.
EXTERNAL_ID_TARGET = "external_id"
ACTIVE_EMAIL_TARGET = "active_email"

# Exact normalized CREATE INDEX text for the partial expression target.
_ACTIVE_EMAIL_CANONICAL = (
    "create unique index active_email on contacts(tenant,lower(email)) where active=1"
)
_EXTERNAL_ID_COLUMNS = ["external_id"]
_ACTIVE_EMAIL_COLUMNS = ["tenant", {"expr": "lower", "column": "email"}]
_ACTIVE_EMAIL_WHERE = {"column": "active", "op": "eq", "value": 1}


def describe(db):
    indexes = [
        {"name": r[0], "sql": r[1]}
        for r in db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='contacts' ORDER BY name"
        )
    ]
    return {"table": "contacts", "columns": dict(COLUMNS), "indexes": indexes}


def identity(catalog):
    return hashlib.sha256(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_sql(sql):
    if not isinstance(sql, str):
        return None
    # Collapse whitespace so formatting differences do not matter; everything
    # else (column order, expression, predicate) must match literally.
    return re.sub(r"\s+", " ", sql).strip().lower()


def _index_imposes_external_id(index):
    """True for the table-level UNIQUE(external_id) index.

    Such an index is stored as sqlite_autoindex_* with sql=NULL; the fixed
    schema in database.py declares exactly one such constraint, on the
    external_id column.
    """
    if index.get("sql") is not None:
        return _normalize_sql(index["sql"]) in (
            "create unique index external_id on contacts(external_id)",
        )
    name = index.get("name", "")
    return isinstance(name, str) and name.startswith("sqlite_autoindex_contacts_")


def _index_is_active_email(index):
    return index.get("name") == ACTIVE_EMAIL_TARGET and _normalize_sql(
        index.get("sql")
    ) == _ACTIVE_EMAIL_CANONICAL


def check_catalog(catalog):
    if not isinstance(catalog, dict):
        raise BindingError("catalog: expected an object")
    if catalog.get("table") != "contacts":
        raise BindingError("catalog.table: must be 'contacts'")
    if catalog.get("columns") != COLUMNS:
        raise BindingError("catalog.columns: does not match the fixed contacts schema")
    indexes = catalog.get("indexes")
    if not isinstance(indexes, list) or not indexes:
        raise BindingError("catalog.indexes: expected a non-empty list")
    for i, index in enumerate(indexes):
        if not isinstance(index, dict) or not isinstance(index.get("name"), str):
            raise BindingError("catalog.indexes[%d].name: missing" % i)
        if "sql" not in index or not (index["sql"] is None or isinstance(index["sql"], str)):
            raise BindingError("catalog.indexes[%d].sql: must be string or null" % i)


def resolve_target(catalog, target):
    """Return the canonical target name for an already-shaped target spec."""
    check_catalog(catalog)
    if target["columns"] == _EXTERNAL_ID_COLUMNS:
        if any(_index_imposes_external_id(idx) for idx in catalog["indexes"]):
            return EXTERNAL_ID_TARGET
        raise BindingError("target: catalog has no unique index on external_id")
    if (
        target["columns"] == _ACTIVE_EMAIL_COLUMNS
        and target.get("where") == _ACTIVE_EMAIL_WHERE
    ):
        if any(_index_is_active_email(idx) for idx in catalog["indexes"]):
            return ACTIVE_EMAIL_TARGET
        raise BindingError(
            "target: catalog has no unique partial index "
            "(tenant,lower(email)) WHERE active=1"
        )
    # Unreachable: the compiler only passes the two canonical shapes.
    raise BindingError("target: no matching unique index in catalog")
