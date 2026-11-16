"""Offline request compiler used by preview and database import clients.

bind(catalog, request) never opens a database. It validates the typed request
strictly against the catalog produced by describe() and returns a BoundPlan
that is plain JSON data: no cursors, connections, closures or executable SQL
text. An illegal request raises BindingError with a concrete request path.
"""
from .catalog import (
    ACTIVE_EMAIL_TARGET,
    EXTERNAL_ID_TARGET,
    check_catalog,
    identity,
    resolve_target,
)
from .errors import BindingError
from .expr import check_assignment, check_predicate
from .types import NON_NULLABLE

FORMAT = 1

_EXTERNAL_TARGET = {"columns": ["external_id"]}
_ACTIVE_EMAIL_TARGET = {
    "columns": ["tenant", {"expr": "lower", "column": "email"}],
    "where": {"column": "active", "op": "eq", "value": 1},
}
_ALLOWED_TARGET_KEYS = frozenset(("columns", "where"))


def _fail(path, message):
    raise BindingError("%s: %s" % (path, message))


def _check_target_shape(target):
    """Match one of the two canonical target specs exactly.

    Case, column order and expressions are never guessed: a missing partial
    predicate, unknown column or non-equivalent expression is rejected here.
    """
    if not isinstance(target, dict):
        _fail("target", "must be an object")
    extra = set(target) - _ALLOWED_TARGET_KEYS
    if extra:
        _fail("target", "unknown field(s) %s" % sorted(extra))
    columns = target.get("columns")
    if not isinstance(columns, list) or not columns:
        _fail("target.columns", "must be a non-empty list")

    if columns == _EXTERNAL_TARGET["columns"]:
        if "where" in target:
            _fail("target.where", "external_id target must not carry a predicate")
        return {
            "canonical": EXTERNAL_ID_TARGET,
            "columns": ["external_id"],
            "where": None,
        }

    if columns == _ACTIVE_EMAIL_TARGET["columns"]:
        # The partial predicate is mandatory and must match exactly; a target
        # without it must never be silently treated as active_email.
        where = target.get("where")
        if where is None:
            _fail("target.where", "missing partial predicate active=1; an "
                                  "expression target without it is rejected")
        if where != _ACTIVE_EMAIL_TARGET["where"]:
            _fail("target.where", "only {active eq 1} is supported; any other "
                                  "expression or value is rejected")
        return {
            "canonical": ACTIVE_EMAIL_TARGET,
            "columns": [
                "tenant",
                {"expr": "lower", "column": "email"},
            ],
            "where": {"column": "active", "op": "eq", "value": 1},
        }

    _fail("target.columns", "only external_id or (tenant, lower(email)) "
                            "WHERE active=1 are accepted; no algebraic guessing")


def _check_action(action):
    if not isinstance(action, dict):
        _fail("action", "must be an object")
    kind = action.get("kind")
    if kind == "nothing":
        if set(action) != {"kind"}:
            _fail("action", "nothing action must be exactly {\"kind\":\"nothing\"}")
        return {"kind": "nothing"}
    if kind != "update":
        _fail("action.kind", "must be 'nothing' or 'update'")
    if "set" not in action:
        _fail("action.set", "update action requires a non-empty set object")
    raw_set = action["set"]
    if not isinstance(raw_set, dict) or not raw_set:
        _fail("action.set", "must be a non-empty object")
    extra = set(action) - {"kind", "set", "where"}
    if extra:
        _fail("action", "unknown field(s) %s" % sorted(extra))
    normalized_set = {}
    for column in raw_set:
        normalized_set[column] = check_assignment(
            column, raw_set[column], "action.set.%s" % column
        )
    where = None
    if "where" in action:
        if action["where"] is not None:
            where = check_predicate(action["where"], "action.where")
    result = {"kind": "update", "set": normalized_set, "where": where}
    # A non-nullable column may only be assigned a value that cannot be NULL:
    # old references to non-nullable columns, or non-null literals. coalesce
    # may itself return NULL, so it is rejected for NOT NULL targets.
    for column, node in normalized_set.items():
        if column in NON_NULLABLE and _can_be_null(node):
            _fail("action.set.%s" % column,
                  "column is NOT NULL but the expression can yield NULL")
    return result


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
        # NULL propagates through integer arithmetic.
        return _can_be_null(node["args"][0]) or _can_be_null(node["args"][1])
    return False


def bind(catalog, request):
    check_catalog(catalog)
    if not isinstance(request, dict):
        _fail("request", "must be an object")
    if "target" not in request:
        _fail("request.target", "missing")
    if "action" not in request:
        _fail("request.action", "missing")
    extra = set(request) - {"target", "action"}
    if extra:
        _fail("request", "unknown field(s) %s" % sorted(extra))

    target_shape = _check_target_shape(request["target"])
    target_index = resolve_target(catalog, {
        "columns": target_shape["columns"],
        **({"where": target_shape["where"]} if target_shape["where"] else {}),
    })
    action = _check_action(request["action"])

    return {
        "format": FORMAT,
        "catalog_sha256": identity(catalog),
        "target_index": target_index,
        "target": target_shape,
        "action": action,
    }
