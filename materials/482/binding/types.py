"""Type rules shared by the offline binder and the plan executor.

The four type names are exactly the ones used in catalog columns:

* int64         - Python int in [-2**63, 2**63-1], bool excluded
* bool01        - Python int strictly equal to 0 or 1
* text          - non-NULL ASCII str
* nullable_text - ASCII str or NULL

Literals carrying JSON null get their type from context (the other end of a
comparison, the assignment target, or a non-null coalesce branch).
"""

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

# Columns an update action is allowed to assign.
ASSIGNABLE = ("email", "active", "external_id", "n")
# Columns physically stored on a source row (id is database-generated).
SOURCE_COLUMNS = ("tenant", "email", "active", "external_id", "n")
TABLE_COLUMNS = ("id", "tenant", "email", "active", "external_id", "n")
NON_NULLABLE = ("id", "tenant", "active", "external_id", "n")
MAX_DEPTH = 8


def is_int64(value):
    return isinstance(value, int) and not isinstance(value, bool) and INT64_MIN <= value <= INT64_MAX


def is_bool01(value):
    # bool must not be accepted as int: isinstance(True, int) is True in Python.
    return isinstance(value, int) and not isinstance(value, bool) and value in (0, 1)


def is_ascii_text(value):
    return isinstance(value, str) and value.isascii()


def type_accepts(column_type, value):
    """Runtime check of a source-row value against a catalog column type."""
    if value is None:
        return column_type == "nullable_text"
    if column_type == "int64":
        return is_int64(value)
    if column_type == "bool01":
        return is_bool01(value)
    if column_type in ("text", "nullable_text"):
        return is_ascii_text(value)
    return False


def comparable(a, b):
    """Two typed operands may be compared only within the same type family."""
    ints = ("int64", "bool01")
    texts = ("text", "nullable_text")
    return (a in ints and b in ints) or (a in texts and b in texts)
