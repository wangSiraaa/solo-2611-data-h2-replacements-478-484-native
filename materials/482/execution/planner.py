"""Validation of a serialized BoundPlan before any row is written.

A plan may arrive from another process after a JSON round trip, so nothing
here relies on Python object identity. An unbound request (no format marker,
raw target/action shapes) is rejected explicitly.
"""
from binding.catalog import (
    ACTIVE_EMAIL_TARGET,
    EXTERNAL_ID_TARGET,
    check_catalog,
    identity,
    resolve_target,
)
from binding.errors import PlanError
from binding.types import (
    ASSIGNABLE,
    MAX_DEPTH,
    NON_NULLABLE,
    SOURCE_COLUMNS,
    TABLE_COLUMNS,
    comparable,
    is_ascii_text,
    is_int64,
)

CANONICAL_TARGETS = (EXTERNAL_ID_TARGET, ACTIVE_EMAIL_TARGET)
_EXTERNAL_TARGET = {
    "canonical": EXTERNAL_ID_TARGET,
    "columns": ["external_id"],
    "where": None,
}
_ACTIVE_EMAIL_TARGET = {
    "canonical": ACTIVE_EMAIL_TARGET,
    "columns": ["tenant", {"expr": "lower", "column": "email"}],
    "where": {"column": "active", "op": "eq", "value": 1},
}
_TYPES = ("int64", "bool01", "text", "nullable_text", "predicate")
_INT_FAMILY = ("int64", "bool01")
_TEXT_FAMILY = ("text", "nullable_text")
_CMP = ("eq", "ne", "lt", "le", "gt", "ge")


def _fail(path, message):
    raise PlanError("%s: %s" % (path, message))


def _same_family(a, b):
    return a == b or (a in _TEXT_FAMILY and b in _TEXT_FAMILY) \
        or (a in _INT_FAMILY and b in _INT_FAMILY)


def check_plan_node(node, path, depth=1):
    if depth > MAX_DEPTH:
        _fail(path, "node depth exceeds %d" % MAX_DEPTH)
    if not isinstance(node, dict):
        _fail(path, "node must be an object")
    t = node.get("t")
    if t not in _TYPES:
        _fail(path, "node has invalid type marker %r" % (t,))

    if "old" in node:
        if set(node) != {"old", "t"} or node["old"] not in TABLE_COLUMNS:
            _fail(path, "invalid old node")
        if t != _column_type(node["old"]):
            _fail(path, "type marker disagrees with column")
        return
    if "excluded" in node:
        if set(node) != {"excluded", "t"} or node["excluded"] not in SOURCE_COLUMNS:
            _fail(path, "invalid excluded node")
        if t != _column_type(node["excluded"]):
            _fail(path, "type marker disagrees with column")
        return
    if "literal" in node:
        if set(node) != {"literal", "t"}:
            _fail(path, "invalid literal node")
        value = node["literal"]
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            _fail(path, "literal must be int, ASCII string or null")
        if isinstance(value, int):
            if t not in _INT_FAMILY or not is_int64(value):
                _fail(path, "integer literal must be int64/bool01 in range")
            if t == "bool01" and value not in (0, 1):
                _fail(path, "bool01 literal must be strictly 0 or 1")
        else:
            if t not in _TEXT_FAMILY or not is_ascii_text(value):
                _fail(path, "text literal must be ASCII")
        return
    if "op" not in node or set(node) != {"op", "args", "t"}:
        _fail(path, "node must be old/excluded/literal or op")

    op, args = node["op"], node["args"]
    if op in ("add", "sub"):
        if t != "int64" or len(args) != 2:
            _fail(path, "invalid arithmetic node")
        for i, sub in enumerate(args):
            check_plan_node(sub, path + ".args[%d]" % i, depth + 1)
            if sub["t"] != "int64":
                _fail(path, "arithmetic operands must be int64")
    elif op == "coalesce":
        if len(args) != 2 or not _same_family(t, args[0]["t"]) \
                or not _same_family(args[0]["t"], args[1]["t"]):
            _fail(path, "invalid coalesce node")
        for i, sub in enumerate(args):
            check_plan_node(sub, path + ".args[%d]" % i, depth + 1)
    else:
        _fail(path, "unknown scalar op %r" % (op,))


def check_plan_predicate(node, path, depth=1):
    if depth > MAX_DEPTH:
        _fail(path, "predicate depth exceeds %d" % MAX_DEPTH)
    if not isinstance(node, dict) or node.get("t") != "predicate":
        _fail(path, "expected a normalized predicate")
    op = node.get("op")
    if op in _CMP:
        if set(node) != {"op", "args", "t"} or len(node["args"]) != 2:
            _fail(path, "invalid comparison predicate")
        a, b = node["args"]
        check_plan_node(a, path + ".args[0]", depth + 1)
        check_plan_node(b, path + ".args[1]", depth + 1)
        if not comparable(a["t"], b["t"]):
            _fail(path, "comparison operands share no type family")
    elif op == "isnull":
        if set(node) != {"op", "arg", "t"}:
            _fail(path, "invalid isnull predicate")
        check_plan_node(node["arg"], path + ".arg", depth + 1)
    elif op in ("and", "or"):
        if set(node) != {"op", "args", "t"} or not node["args"]:
            _fail(path, "invalid %s predicate" % op)
        for i, sub in enumerate(node["args"]):
            check_plan_predicate(sub, path + ".args[%d]" % i, depth + 1)
    elif op == "not":
        if set(node) != {"op", "arg", "t"}:
            _fail(path, "invalid not predicate")
        check_plan_predicate(node["arg"], path + ".arg", depth + 1)
    else:
        _fail(path, "unknown predicate op %r" % (op,))


def _column_type(col):
    from binding.catalog import COLUMNS
    return COLUMNS[col]


def validate_plan(plan, catalog):
    """Structural validation plus live-catalog identity check."""
    if not isinstance(plan, dict):
        _fail("plan", "must be an object")
    # The format marker distinguishes a BoundPlan from a raw request.
    if "format" not in plan:
        _fail("plan.format", "missing: an unbound request is not an execution plan")
    if plan["format"] != 1:
        _fail("plan.format", "unknown format %r" % (plan["format"],))
    if not isinstance(plan.get("catalog_sha256"), str):
        _fail("plan.catalog_sha256", "missing or not a string")
    check_catalog(catalog)
    if plan["catalog_sha256"] != identity(catalog):
        _fail("plan.catalog_sha256",
              "does not match the live database catalog; refusing stale plan")
    target_index = plan.get("target_index")
    if target_index not in CANONICAL_TARGETS:
        _fail("plan.target_index", "unknown target %r" % (target_index,))

    target = plan.get("target")
    expected_target = (
        _EXTERNAL_TARGET if target_index == EXTERNAL_ID_TARGET else _ACTIVE_EMAIL_TARGET
    )
    if target != expected_target:
        _fail("plan.target", "does not match target_index %s" % target_index)
    # Must correspond to a real index in the live catalog.
    if resolve_target(catalog, {
        "columns": target["columns"],
        **({"where": target["where"]} if target["where"] else {}),
    }) != target_index:
        _fail("plan.target_index", "no matching unique index in the live catalog")

    action = plan.get("action")
    if not isinstance(action, dict):
        _fail("plan.action", "missing or not an object")
    kind = action.get("kind")
    if kind == "nothing":
        if action != {"kind": "nothing"}:
            _fail("plan.action", "invalid nothing action")
    elif kind == "update":
        raw_set = action.get("set")
        if not isinstance(raw_set, dict) or not raw_set:
            _fail("plan.action.set", "must be a non-empty object")
        if set(action) - {"kind", "set", "where"}:
            _fail("plan.action", "unknown fields")
        for col, node in raw_set.items():
            if col not in ASSIGNABLE:
                _fail("plan.action.set.%s" % col, "column not assignable")
            check_plan_node(node, "plan.action.set.%s" % col)
            if not _same_family(node["t"], _column_type(col)):
                _fail("plan.action.set.%s" % col, "type does not fit column")
            if col in NON_NULLABLE and _can_be_null(node):
                _fail("plan.action.set.%s" % col,
                      "NOT NULL column cannot receive a nullable expression")
        if action.get("where") is not None:
            check_plan_predicate(action["where"], "plan.action.where")
    else:
        _fail("plan.action.kind", "must be nothing or update")
    return plan


def _can_be_null(node):
    if "literal" in node and node["literal"] is None:
        return True
    if "old" in node:
        return node["old"] not in NON_NULLABLE
    if "excluded" in node:
        return node["excluded"] not in NON_NULLABLE
    if node.get("op") == "coalesce":
        return _can_be_null(node["args"][1])
    if node.get("op") in ("add", "sub"):
        return _can_be_null(node["args"][0]) or _can_be_null(node["args"][1])
    return False
