"""Schema compiler and row evaluator.

The compiler is deliberately independent of the storage layer.  Importers can
compile and preview a plan without creating a database, and the same compiled
plan (after ``dump``/``load``) is handed to ``storage.RowStore``.

All schema validation, type checking, dependency checking and cycle detection
happens in :meth:`Compiler.compile`.  Row evaluation is shared by
``CompiledSchema.preview`` and the storage layer, so a preflight check and a
later write use exactly the same NULL propagation, short-circuit and overflow
rules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
MAX_COLUMNS = 16
MAX_GENERATED_COLUMNS = 8
MAX_TEXT_CODEPOINTS = 128

KINDS = {"base", "stored", "virtual"}
TYPES = {"int64", "text"}

# A type descriptor is (type_name, may_be_null).  ``type_name`` is None for an
# expression which can only ever produce NULL.
TypeInfo = Tuple[Optional[str], bool]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _fail(message: str) -> None:
    raise ValueError(message)


def _check_int64(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{where}: int64 value required, got {type(value).__name__}")
    if value < INT64_MIN or value > INT64_MAX:
        _fail(f"{where}: integer {value} is outside int64 range")
    return value


def _check_text(value: Any, where: str) -> str:
    if not isinstance(value, str):
        _fail(f"{where}: text value required, got {type(value).__name__}")
    if len(value) > MAX_TEXT_CODEPOINTS:
        _fail(f"{where}: text exceeds {MAX_TEXT_CODEPOINTS} code points")
    return value


def check_value(value: Any, type_name: str, nullable: bool, where: str) -> Any:
    """Validate a concrete value against a declared column type."""

    if value is None:
        if not nullable:
            _fail(f"{where}: NULL is not allowed")
        return None
    if type_name == "int64":
        return _check_int64(value, where)
    if type_name == "text":
        return _check_text(value, where)
    _fail(f"{where}: unknown column type {type_name!r}")


def typed_equal(left: Any, right: Any) -> bool:
    """Equality which does not consider ``1`` and ``True`` equal."""

    if left is None or right is None:
        return left is None and right is None
    return type(left) is type(right) and left == right


def _unify(left: TypeInfo, right: TypeInfo, where: str) -> Optional[str]:
    """Unify two value types, treating a NULL literal as compatible."""

    lt, rt = left[0], right[0]
    if lt is None:
        return rt
    if rt is None:
        return lt
    if lt != rt:
        _fail(f"{where}: incompatible types {lt!r} and {rt!r}")
    return lt


class CompiledSchema:
    """An immutable, validated column plan."""

    def __init__(self, columns: List[dict]):
        # Keep a private JSON copy so callers cannot mutate the plan through
        # either the argument or the value returned from dump().
        self.columns: List[dict] = json.loads(canonical(columns))
        self.fingerprint: str = fingerprint(self.columns)

        self.by_name: Dict[str, dict] = {c["name"]: c for c in self.columns}
        self.base_columns: List[str] = [c["name"] for c in self.columns if c["kind"] == "base"]
        self.stored_columns: List[str] = [c["name"] for c in self.columns if c["kind"] == "stored"]
        self.virtual_columns: List[str] = [c["name"] for c in self.columns if c["kind"] == "virtual"]
        self.generated_columns: List[str] = self.stored_columns + self.virtual_columns
        # Physical order follows declaration order, restricted to the columns
        # which are actually persisted.
        self.physical_columns: List[str] = [
            c["name"] for c in self.columns if c["kind"] in ("base", "stored")
        ]
        self.layout: Dict[str, Any] = {
            "physical_columns": self.physical_columns,
            "base_columns": self.base_columns,
            "stored_columns": self.stored_columns,
            "virtual_columns": self.virtual_columns,
            "column_types": {c["name"]: c["type"] for c in self.columns},
        }
        self.layout_fingerprint: str = fingerprint(self.layout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def dump(self) -> dict:
        return {
            "columns": json.loads(canonical(self.columns)),
            "fingerprint": self.fingerprint,
        }

    def preview(self, base: dict) -> dict:
        """Return the complete logical row for a base-column input.

        Supplied base values are validated; generated and unknown input names
        are rejected.  Missing base values use their declared default, or NULL
        when no default exists.  An explicitly supplied NULL never falls back
        to the default.
        """

        if not isinstance(base, dict):
            _fail("base must be a mapping")

        allowed = set(self.base_columns)
        unknown = [name for name in base if name not in allowed]
        if unknown:
            _fail(f"base object contains non-base or unknown fields: {sorted(map(str, unknown))}")

        base_values: Dict[str, Any] = {}
        for col in self.columns:
            if col["kind"] != "base":
                continue
            name = col["name"]
            if name in base:
                value = base[name]
            elif "default" in col:
                value = col["default"]
            else:
                value = None
            check_value(value, col["type"], col["nullable"], f"base column {name!r}")
            base_values[name] = value

        return self._evaluate_all(base_values)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def _evaluate_all(self, base_values: Dict[str, Any]) -> dict:
        memo: Dict[str, Any] = dict(base_values)
        resolving: Set[str] = set()

        def resolve(name: str) -> Any:
            if name in memo:
                return memo[name]
            col = self.by_name.get(name)
            if col is None or col["kind"] == "base":
                _fail(f"unknown or unavailable column reference {name!r}")
            if name in resolving:
                _fail(f"cyclic generated-column dependency involving {name!r}")
            resolving.add(name)
            try:
                value = self._eval_generated(col, resolve)
            finally:
                resolving.discard(name)
            check_value(value, col["type"], col["nullable"], f"generated column {name!r}")
            memo[name] = value
            return value

        # Resolve in declaration order; a forward reference is simply resolved
        # recursively at the point where it is needed.
        for col in self.columns:
            resolve(col["name"])

        return {col["name"]: memo[col["name"]] for col in self.columns}

    @staticmethod
    def _eval_generated(col: dict, resolve: Callable[[str], Any]) -> Any:
        return evaluate(col["expr"], resolve)

    def physical_row(self, logical_row: dict) -> dict:
        """Project a logical row to exactly the values stored on disk."""

        return {name: logical_row[name] for name in self.physical_columns}

    def check_physical_row(self, physical: dict) -> dict:
        """Validate a persisted row and return its logical row.

        This checks the storage layout (base + stored, no virtual columns),
        validates each persisted value, recomputes all generated values, and
        verifies that the stored values agree with the recomputation.
        """

        if not isinstance(physical, dict):
            _fail("physical row must be a mapping")
        expected = set(self.physical_columns)
        actual = set(physical)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(map(str, actual - expected))
            _fail(f"physical row layout mismatch (missing={missing}, extra={extra})")

        base_values = {name: physical[name] for name in self.base_columns}
        # preview() validates base values and every generated value including
        # nullable/type constraints.
        logical = self.preview(base_values)
        for name in self.stored_columns:
            if not typed_equal(physical[name], logical[name]):
                _fail(f"stored column {name!r} does not match recomputed value")
        return logical


# ----------------------------------------------------------------------
# Expression evaluation (shared by preflight and storage)
# ----------------------------------------------------------------------
def _checked_int(value: int, where: str) -> int:
    if value < INT64_MIN or value > INT64_MAX:
        _fail(f"{where}: integer overflow outside int64 range")
    return value


def evaluate_condition(condition: dict, resolve: Callable[[str], Any]) -> bool:
    op = condition["op"]
    if op == "isnull":
        return evaluate(condition["args"][0], resolve) is None

    left = evaluate(condition["args"][0], resolve)
    right = evaluate(condition["args"][1], resolve)
    # NULL comparisons never satisfy eq/lt.
    if left is None or right is None:
        return False
    if op == "eq":
        return left == right
    if op == "lt":
        return left < right
    _fail(f"unknown condition operator {op!r}")


def evaluate(expr: Any, resolve: Callable[[str], Any]) -> Any:
    """Evaluate a value expression with the plan's short-circuit semantics."""

    if expr is None or not isinstance(expr, dict):
        # Constants were validated at compile time.  At runtime we only need
        # to return them unchanged.
        return expr

    if "col" in expr:
        return resolve(expr["col"])

    op = expr["op"]
    args = expr.get("args", [])

    if op in ("+", "-"):
        # Both operands are evaluated (only coalesce/case short-circuit); an
        # overflow in either operand rejects the row.  A NULL operand yields a
        # NULL result.
        left = evaluate(args[0], resolve)
        right = evaluate(args[1], resolve)
        if left is None or right is None:
            return None
        if op == "+":
            return _checked_int(left + right, "addition")
        return _checked_int(left - right, "subtraction")

    if op == "concat":
        left = evaluate(args[0], resolve)
        right = evaluate(args[1], resolve)
        if left is None or right is None:
            return None
        result = left + right
        if len(result) > MAX_TEXT_CODEPOINTS:
            _fail("concat: result exceeds 128 code points")
        return result

    if op == "coalesce":
        left = evaluate(args[0], resolve)
        if left is not None:
            return left
        return evaluate(args[1], resolve)

    if op == "case":
        if evaluate_condition(expr["when"], resolve):
            return evaluate(expr["then"], resolve)
        return evaluate(expr["else"], resolve)

    _fail(f"unknown value operator {op!r}")


# ----------------------------------------------------------------------
# Compile-time expression typing and dependency collection
# ----------------------------------------------------------------------
def _infer_value(expr: Any, decls: Dict[str, dict], deps: Set[str], where: str) -> TypeInfo:
    if expr is None:
        return (None, True)
    if isinstance(expr, bool):
        _fail(f"{where}: bool is not an int64 value")
    if isinstance(expr, int):
        _check_int64(expr, where)
        return ("int64", False)
    if isinstance(expr, str):
        _check_text(expr, where)
        return ("text", False)
    if not isinstance(expr, dict):
        _fail(f"{where}: invalid expression")

    if "col" in expr:
        if set(expr) != {"col"}:
            _fail(f"{where}: a column reference must contain only 'col'")
        name = expr["col"]
        if not isinstance(name, str) or name not in decls:
            _fail(f"{where}: unknown column reference {name!r}")
        deps.add(name)
        col = decls[name]
        return (col["type"], bool(col["nullable"]))

    op = expr.get("op")
    if not isinstance(op, str):
        _fail(f"{where}: expression must be a constant, column reference, or operator")

    if op in ("+", "-"):
        if set(expr) != {"op", "args"}:
            _fail(f"{where}: operator {op!r} accepts only 'op' and 'args'")
        args = expr["args"]
        if not isinstance(args, list) or len(args) != 2:
            _fail(f"{where}: operator {op!r} requires exactly two arguments")
        left = _infer_value(args[0], decls, deps, where)
        right = _infer_value(args[1], decls, deps, where)
        for info in (left, right):
            if info[0] is not None and info[0] != "int64":
                _fail(f"{where}: operator {op!r} requires int64 operands")
        if left[0] is None or right[0] is None:
            # An always-NULL operand makes the result always NULL.
            return (None, True)
        return ("int64", left[1] or right[1])

    if op == "concat":
        if set(expr) != {"op", "args"}:
            _fail(f"{where}: concat accepts only 'op' and 'args'")
        args = expr["args"]
        if not isinstance(args, list) or len(args) != 2:
            _fail(f"{where}: concat requires exactly two arguments")
        left = _infer_value(args[0], decls, deps, where)
        right = _infer_value(args[1], decls, deps, where)
        for info in (left, right):
            if info[0] is not None and info[0] != "text":
                _fail(f"{where}: concat requires text operands")
        if left[0] is None or right[0] is None:
            return (None, True)
        return ("text", left[1] or right[1])

    if op == "coalesce":
        if set(expr) != {"op", "args"}:
            _fail(f"{where}: coalesce accepts only 'op' and 'args'")
        args = expr["args"]
        if not isinstance(args, list) or len(args) != 2:
            _fail(f"{where}: coalesce requires exactly two arguments")
        left = _infer_value(args[0], decls, deps, where)
        right = _infer_value(args[1], decls, deps, where)
        result_type = _unify(left, right, f"{where}: coalesce")
        # The right side is evaluated only when the left side is NULL.
        return (result_type, left[1] and right[1])

    if op == "case":
        if set(expr) != {"op", "when", "then", "else"}:
            _fail(f"{where}: case requires exactly 'when', 'then' and 'else'")
        _infer_condition(expr["when"], decls, deps, where + ".when")
        then_info = _infer_value(expr["then"], decls, deps, where + ".then")
        else_info = _infer_value(expr["else"], decls, deps, where + ".else")
        result_type = _unify(then_info, else_info, f"{where}: case branches")
        # The condition selects exactly one branch, so either branch may make
        # the result nullable.
        return (result_type, then_info[1] or else_info[1])

    _fail(f"{where}: unknown value operator {op!r}")


def _infer_condition(expr: Any, decls: Dict[str, dict], deps: Set[str], where: str) -> None:
    if not isinstance(expr, dict) or not isinstance(expr.get("op"), str):
        _fail(f"{where}: condition must be eq, lt or isnull")
    op = expr["op"]

    if op == "isnull":
        if set(expr) != {"op", "args"}:
            _fail(f"{where}: isnull accepts only 'op' and 'args'")
        args = expr["args"]
        if not isinstance(args, list) or len(args) != 1:
            _fail(f"{where}: isnull requires exactly one argument")
        _infer_value(args[0], decls, deps, where)
        return

    if op in ("eq", "lt"):
        if set(expr) != {"op", "args"}:
            _fail(f"{where}: {op} accepts only 'op' and 'args'")
        args = expr["args"]
        if not isinstance(args, list) or len(args) != 2:
            _fail(f"{where}: {op} requires exactly two arguments")
        left = _infer_value(args[0], decls, deps, where)
        right = _infer_value(args[1], decls, deps, where)
        if left[0] is not None and right[0] is not None and left[0] != right[0]:
            _fail(f"{where}: {op} operands must have the same type")
        return

    _fail(f"{where}: unknown condition operator {op!r}")


# ----------------------------------------------------------------------
# Compiler
# ----------------------------------------------------------------------
class Compiler:
    """Validate column definitions and build :class:`CompiledSchema` objects."""

    def compile(self, columns: List[dict]) -> CompiledSchema:
        normalized, dependencies = self._validate(columns)
        self._check_cycles(dependencies)
        return CompiledSchema(normalized)

    def load(self, payload: Any) -> CompiledSchema:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid schema payload") from exc
        if not isinstance(payload, dict):
            _fail("schema payload must be an object")
        if "columns" not in payload or "fingerprint" not in payload:
            _fail("schema payload must contain 'columns' and 'fingerprint'")
        if set(payload) - {"columns", "fingerprint"}:
            _fail("schema payload contains unknown fields")
        plan = self.compile(payload["columns"])
        if payload["fingerprint"] != plan.fingerprint:
            _fail("schema fingerprint mismatch")
        return plan

    # ------------------------------------------------------------------
    def _validate(self, columns: Any) -> Tuple[List[dict], Dict[str, Set[str]]]:
        if not isinstance(columns, list):
            _fail("columns must be a list")
        if len(columns) > MAX_COLUMNS:
            _fail(f"at most {MAX_COLUMNS} columns are allowed")

        names: Set[str] = set()
        decls: Dict[str, dict] = {}
        normalized: List[dict] = []
        generated_count = 0

        for index, raw in enumerate(columns):
            where = f"column {index}"
            if not isinstance(raw, dict):
                _fail(f"{where}: definition must be an object")

            allowed = {"name", "type", "kind", "nullable", "expr", "default"}
            unknown = set(raw) - allowed
            if unknown:
                _fail(f"{where}: unknown definition fields {sorted(unknown)}")
            for required in ("name", "type", "kind", "nullable"):
                if required not in raw:
                    _fail(f"{where}: missing required field {required!r}")

            name = raw["name"]
            if not isinstance(name, str) or not name:
                _fail(f"{where}: name must be a non-empty string")
            if name in names:
                _fail(f"{where}: duplicate column name {name!r}")
            names.add(name)

            type_name = raw["type"]
            if type_name not in TYPES:
                _fail(f"{where}: type must be int64 or text")
            kind = raw["kind"]
            if kind not in KINDS:
                _fail(f"{where}: kind must be base, stored or virtual")
            nullable = raw["nullable"]
            if not isinstance(nullable, bool):
                _fail(f"{where}: nullable must be a boolean")

            col: Dict[str, Any] = {
                "name": name,
                "type": type_name,
                "kind": kind,
                "nullable": nullable,
            }

            if kind == "base":
                if "expr" in raw:
                    _fail(f"{where}: base columns cannot have an expression")
                if "default" in raw:
                    default = raw["default"]
                    # A default is a literal, never an expression.
                    if default is not None:
                        check_value(default, type_name, nullable, f"{where} default")
                    elif not nullable:
                        _fail(f"{where}: non-nullable column cannot default to NULL")
                    col["default"] = default
            else:
                generated_count += 1
                if "default" in raw:
                    _fail(f"{where}: generated columns cannot have a default")
                if "expr" not in raw:
                    _fail(f"{where}: generated column requires an expression")
                col["expr"] = raw["expr"]

            decls[name] = {
                "name": name,
                "type": type_name,
                "kind": kind,
                "nullable": nullable,
            }
            normalized.append(col)

        if generated_count > MAX_GENERATED_COLUMNS:
            _fail(f"at most {MAX_GENERATED_COLUMNS} generated columns are allowed")

        dependencies: Dict[str, Set[str]] = {}
        for col in normalized:
            if col["kind"] == "base":
                continue
            deps: Set[str] = set()
            info = _infer_value(col["expr"], decls, deps, f"column {col['name']!r}")
            if info[0] is None:
                if not col["nullable"]:
                    _fail(f"column {col['name']!r}: expression always yields NULL but is not nullable")
            elif info[0] != col["type"]:
                _fail(
                    f"column {col['name']!r}: expression type {info[0]!r} does not match declared type {col['type']!r}"
                )
            dependencies[col["name"]] = deps

        return normalized, dependencies

    @staticmethod
    def _check_cycles(dependencies: Dict[str, Set[str]]) -> None:
        state: Dict[str, int] = {}

        def visit(name: str) -> None:
            current = state.get(name, 0)
            if current == 1:
                _fail(f"cyclic generated-column dependency involving {name!r}")
            if current == 2:
                return
            state[name] = 1
            for dependency in dependencies.get(name, ()):
                if dependency in dependencies:
                    visit(dependency)
            state[name] = 2

        for name in dependencies:
            visit(name)
