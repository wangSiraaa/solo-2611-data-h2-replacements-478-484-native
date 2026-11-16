"""Deterministic evaluation of normalized typed nodes.

Evaluation happens in Python so that int64 overflow is detected (SQLite would
silently promote to REAL instead) and SQL three-valued logic is explicit.
All values are still written through parameterized SQL; SQLite remains the
authority for real unique constraints, types and the transaction.
"""
from binding.errors import ExecutionError
from binding.types import INT64_MAX, INT64_MIN, type_accepts

_TRUE, _FALSE, _UNKNOWN = True, False, None
_CMP = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
}


def eval_expr(node, source_no, old_row, excluded):
    if "old" in node:
        if old_row is None:
            # old is only legal against an existing target row; reaching here
            # on an insert means a corrupt plan slipped past validation.
            raise ExecutionError(source_no, "old used without a target row")
        return old_row[node["old"]]
    if "excluded" in node:
        return excluded[node["excluded"]]
    if "literal" in node:
        return node["literal"]

    op = node["op"]
    if op in ("add", "sub"):
        left = eval_expr(node["args"][0], source_no, old_row, excluded)
        if left is None:
            return None
        right = eval_expr(node["args"][1], source_no, old_row, excluded)
        if right is None:
            return None
        result = left + right if op == "add" else left - right
        if not (INT64_MIN <= result <= INT64_MAX):
            raise ExecutionError(
                source_no, "integer arithmetic overflows int64 (%d)" % result
            )
        return result
    if op == "coalesce":
        first = eval_expr(node["args"][0], source_no, old_row, excluded)
        if first is not None:
            return first
        return eval_expr(node["args"][1], source_no, old_row, excluded)
    raise ExecutionError(source_no, "unknown expression op %r" % (op,))


def eval_predicate(node, source_no, old_row, excluded):
    """Return True/False/None (SQL three-valued logic)."""
    op = node["op"]
    if op in _CMP:
        a = eval_expr(node["args"][0], source_no, old_row, excluded)
        b = eval_expr(node["args"][1], source_no, old_row, excluded)
        if a is None or b is None:
            return _UNKNOWN
        return _CMP[op](a, b)
    if op == "isnull":
        return eval_expr(node["arg"], source_no, old_row, excluded) is None
    if op == "not":
        inner = eval_predicate(node["arg"], source_no, old_row, excluded)
        return None if inner is None else not inner
    if op == "and":
        result = True
        for sub in node["args"]:
            value = eval_predicate(sub, source_no, old_row, excluded)
            if value is False:
                return False
            if value is None:
                result = None
        return result
    if op == "or":
        result = False
        for sub in node["args"]:
            value = eval_predicate(sub, source_no, old_row, excluded)
            if value is True:
                return True
            if value is None:
                result = None
        return result
    raise ExecutionError(source_no, "unknown predicate op %r" % (op,))


def check_assigned_value(column, column_type, value, source_no):
    """Defense in depth: every assigned value still fits its column."""
    if not type_accepts(column_type, value):
        raise ExecutionError(
            source_no,
            "assigned value %r does not fit column %s (%s)"
            % (value, column, column_type),
        )
