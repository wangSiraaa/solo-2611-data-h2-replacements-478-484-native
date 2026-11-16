"""Typed expression and predicate validation/normalization (offline).

Every node is checked against the contract grammar, type rules and the depth
limit, then returned in a normalized form that carries its resolved type in
the "t" field. The normalized nodes are pure JSON data: they survive a JSON
round trip and need no Python closures to execute.
"""
from .errors import BindingError
from .types import (
    ASSIGNABLE,
    MAX_DEPTH,
    SOURCE_COLUMNS,
    TABLE_COLUMNS,
    comparable,
    is_ascii_text,
    is_bool01,
    is_int64,
)

_EXPR_KEYS = frozenset(("old", "excluded", "literal", "op", "args"))
_CMP_OPS = frozenset(("eq", "ne", "lt", "le", "gt", "ge"))
_ARITH_OPS = frozenset(("add", "sub"))
_TEXT_FAMILY = frozenset(("text", "nullable_text"))
_INT_FAMILY = frozenset(("int64", "bool01"))


def _fail(path, message):
    raise BindingError("%s: %s" % (path, message))


def is_bare_null(node):
    return isinstance(node, dict) and node == {"literal": None}


def _infer_literal(value, path, ctx_type):
    if value is None:
        if ctx_type is None:
            _fail(path, "literal NULL needs a context type (comparison, "
                        "assignment or coalesce sibling); bare NULL is rejected")
        return {"literal": None, "t": ctx_type}, ctx_type
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail(path, "literal must be an int64 integer, ASCII string or null; "
                    "booleans, floats and other types are rejected")
    if isinstance(value, str):
        if not is_ascii_text(value):
            _fail(path, "literal text must be ASCII")
        t = "text"
    else:
        if not is_int64(value):
            _fail(path, "literal integer outside int64 range")
        # An integer literal in a bool01 assignment/comparison context is
        # accepted only as strict 0/1 and adopts that context type.
        if ctx_type == "bool01":
            if not is_bool01(value):
                _fail(path, "bool01 literal must be strictly 0 or 1")
            t = "bool01"
        else:
            t = "int64"
    if ctx_type is not None and not _same_family(t, ctx_type):
        _fail(path, "literal type %s does not fit context %s" % (t, ctx_type))
    return {"literal": value, "t": t}, t


def check_expr(node, path, ctx_type=None, depth=1):
    if depth > MAX_DEPTH:
        _fail(path, "expression depth exceeds %d" % MAX_DEPTH)
    if not isinstance(node, dict):
        _fail(path, "expected an expression object")
    extra = set(node) - _EXPR_KEYS
    if extra:
        _fail(path, "unknown field(s) %s" % sorted(extra))

    if "old" in node:
        if set(node) != {"old"}:
            _fail(path, "old expression must be exactly {\"old\": column}")
        col = node["old"]
        if col not in TABLE_COLUMNS:
            _fail(path, "old references unknown column %r" % (col,))
        return {"old": col, "t": _column_type(col)}

    if "excluded" in node:
        if set(node) != {"excluded"}:
            _fail(path, "excluded expression must be exactly {\"excluded\": column}")
        col = node["excluded"]
        if col == "id":
            _fail(path, "excluded cannot reference database-generated id")
        if col not in SOURCE_COLUMNS:
            _fail(path, "excluded references unknown column %r" % (col,))
        return {"excluded": col, "t": _column_type(col)}

    if "literal" in node:
        if set(node) != {"literal"}:
            _fail(path, "literal expression must be exactly {\"literal\": value}")
        return _infer_literal(node["literal"], path, ctx_type)[0]

    if "op" in node:
        op = node["op"]
        args = node.get("args")
        if set(node) != {"op", "args"}:
            _fail(path, "op expression must be exactly {\"op\":..., \"args\":[...]}")
        if not isinstance(args, list):
            _fail(path, "args must be a list")
        if op in _ARITH_OPS:
            if len(args) != 2:
                _fail(path, "%s needs exactly 2 args" % op)
            left = check_expr(args[0], path + ".args[0]", "int64", depth + 1)
            right = check_expr(args[1], path + ".args[1]", "int64", depth + 1)
            if left["t"] != "int64" or right["t"] != "int64":
                _fail(path, "%s only accepts int64 operands (NULL propagates)" % op)
            return {"op": op, "args": [left, right], "t": "int64"}
        if op == "coalesce":
            if len(args) != 2:
                _fail(path, "coalesce needs exactly 2 args")
            return _check_coalesce(args, path, ctx_type, depth)
        _fail(path, "unknown expression op %r" % (op,))

    _fail(path, "expression must be old/excluded/literal or an op node")


def _check_coalesce(args, path, ctx_type, depth):
    first, second = args
    if is_bare_null(first):
        # The first branch contributes no type; the second must provide one.
        right = check_expr(second, path + ".args[1]", None, depth + 1)
        left = check_expr(first, path + ".args[0]", right["t"], depth + 1)
    else:
        left = check_expr(first, path + ".args[0]", ctx_type, depth + 1)
        right = check_expr(second, path + ".args[1]", left["t"], depth + 1)
    if not _same_family(left["t"], right["t"]):
        _fail(path, "coalesce branches must share a type (got %s and %s)"
                    % (left["t"], right["t"]))
    t = left["t"]
    if ctx_type is not None and not _same_family(t, ctx_type):
        _fail(path, "coalesce type %s does not fit context %s" % (t, ctx_type))
    return {"op": "coalesce", "args": [left, right], "t": t}


def _same_family(a, b):
    return a == b or ({a, b} <= _TEXT_FAMILY) or ({a, b} <= _INT_FAMILY)


def _column_type(col):
    from .catalog import COLUMNS
    return COLUMNS[col]


def check_predicate(node, path, depth=1):
    if depth > MAX_DEPTH:
        _fail(path, "predicate depth exceeds %d" % MAX_DEPTH)
    if not isinstance(node, dict) or "op" not in node:
        _fail(path, "expected a predicate object with op")
    op = node["op"]

    if op in _CMP_OPS:
        if set(node) != {"op", "args"} or not isinstance(node.get("args"), list) \
                or len(node["args"]) != 2:
            _fail(path, "comparison must be {\"op\": cmp, \"args\":[expr,expr]}")
        a_raw, b_raw = node["args"]
        if is_bare_null(a_raw):
            b = check_expr(b_raw, path + ".args[1]", None, depth + 1)
            a = check_expr(a_raw, path + ".args[0]", b["t"], depth + 1)
        elif is_bare_null(b_raw):
            a = check_expr(a_raw, path + ".args[0]", None, depth + 1)
            b = check_expr(b_raw, path + ".args[1]", a["t"], depth + 1)
        else:
            a = check_expr(a_raw, path + ".args[0]", None, depth + 1)
            b = check_expr(b_raw, path + ".args[1]", None, depth + 1)
        if not comparable(a["t"], b["t"]):
            _fail(path, "comparison operands must share a type family "
                        "(got %s and %s)" % (a["t"], b["t"]))
        return {"op": op, "args": [a, b], "t": "predicate"}

    if op == "isnull":
        if set(node) != {"op", "arg"}:
            _fail(path, "isnull must be {\"op\":\"isnull\",\"arg\":expr}")
        arg = check_expr(node["arg"], path + ".arg", None, depth + 1)
        return {"op": "isnull", "arg": arg, "t": "predicate"}

    if op in ("and", "or"):
        if set(node) != {"op", "args"} or not isinstance(node.get("args"), list):
            _fail(path, "%s must be {\"op\":...,\"args\":[predicate,...]}" % op)
        if len(node["args"]) == 0:
            _fail(path, "%s args list must be non-empty" % op)
        items = [
            check_predicate(sub, path + ".args[%d]" % i, depth + 1)
            for i, sub in enumerate(node["args"])
        ]
        return {"op": op, "args": items, "t": "predicate"}

    if op == "not":
        if set(node) != {"op", "arg"}:
            _fail(path, "not must be {\"op\":\"not\",\"arg\":predicate}")
        inner = check_predicate(node["arg"], path + ".arg", depth + 1)
        return {"op": "not", "arg": inner, "t": "predicate"}

    _fail(path, "unknown predicate op %r" % (op,))


def check_assignment(column, expr_node, path):
    if column not in ASSIGNABLE:
        _fail(path, "set may only write %s, not %r" % (",".join(ASSIGNABLE), column))
    target_type = _column_type(column)
    normalized = check_expr(expr_node, path, target_type)
    if not _same_family(normalized["t"], target_type):
        _fail(path, "value type %s cannot assign to %s (%s)"
                    % (normalized["t"], column, target_type))
    if column == "active" and normalized["t"] != "bool01":
        _fail(path, "active requires bool01 (strict 0/1), got %s" % normalized["t"])
    if column == "n" and normalized["t"] != "int64":
        _fail(path, "n requires int64, got %s" % normalized["t"])
    return normalized
