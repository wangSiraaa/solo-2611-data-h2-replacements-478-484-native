"""End-to-end verification for the schema compiler and row store.

Run with ``python3 verify.py``.  It prints one JSON record per scenario and
fails loudly if any expected behaviour is not observed.  Nothing is written
outside temporary directories.

The comparison in ``independent-interpreter`` deliberately uses a second,
independent expression evaluator (see ``MiniInterpreter``) rather than the
schema module's evaluator.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from schema import Compiler
from schema.compiler import INT64_MAX, INT64_MIN
from storage import RowStore

RECORDS = []


def record(scenario, **values):
    entry = {"scenario": scenario, **values}
    RECORDS.append(entry)
    print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return entry


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def raises(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the point is to inspect failures
        return type(exc).__name__, str(exc)
    return None, None


def col(name, type_name="int64", kind="base", nullable=True, **extra):
    definition = {"name": name, "type": type_name, "kind": kind, "nullable": nullable}
    definition.update(extra)
    return definition


def base_plan():
    return [
        col("x"),
        col("y", kind="virtual", expr={"op": "+", "args": [{"col": "x"}, 1]}),
        col("z", kind="stored", expr={"op": "+", "args": [{"col": "y"}, 1]}),
    ]


def forward_plan():
    plan = base_plan()
    return [plan[0], plan[2], plan[1]]


# ----------------------------------------------------------------------
# A second, independent AST interpreter used only for cross-checking.
# ----------------------------------------------------------------------
class MiniInterpreter:
    def __init__(self, columns):
        self.columns = columns
        self.declarations = {c["name"]: c for c in columns}

    def full_row(self, base):
        memo = {}
        for column in self.columns:
            if column["kind"] == "base":
                if column["name"] in base:
                    memo[column["name"]] = base[column["name"]]
                elif "default" in column:
                    memo[column["name"]] = column["default"]
                else:
                    memo[column["name"]] = None

        active = set()

        def resolve(name):
            if name in memo:
                return memo[name]
            if name in active:
                raise ValueError("cycle")
            active.add(name)
            try:
                value = self.expr(self.declarations[name]["expr"], resolve)
            finally:
                active.discard(name)
            memo[name] = value
            return value

        for column in self.columns:
            resolve(column["name"])
        return {c["name"]: memo[c["name"]] for c in self.columns}

    def expr(self, node, resolve):
        if node is None or not isinstance(node, dict):
            return node
        if "col" in node:
            return resolve(node["col"])
        op = node["op"]
        args = node.get("args", [])
        if op in ("+", "-"):
            # Arithmetic and concat evaluate both operands (no short-circuit);
            # coalesce/case are the only short-circuiting forms.
            left = self.expr(args[0], resolve)
            right = self.expr(args[1], resolve)
            if left is None or right is None:
                return None
            value = left + right if op == "+" else left - right
            if not (INT64_MIN <= value <= INT64_MAX):
                raise OverflowError("int64 overflow")
            return value
        if op == "concat":
            left = self.expr(args[0], resolve)
            right = self.expr(args[1], resolve)
            if left is None or right is None:
                return None
            return left + right
        if op == "coalesce":
            left = self.expr(args[0], resolve)
            return left if left is not None else self.expr(args[1], resolve)
        if op == "case":
            if self.condition(node["when"], resolve):
                return self.expr(node["then"], resolve)
            return self.expr(node["else"], resolve)
        raise ValueError(op)

    def condition(self, node, resolve):
        op = node["op"]
        args = node["args"]
        if op == "isnull":
            return self.expr(args[0], resolve) is None
        left = self.expr(args[0], resolve)
        right = self.expr(args[1], resolve)
        if left is None or right is None:
            return False
        return left == right if op == "eq" else left < right


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------
def scenario_preflight_and_identity():
    compiler = Compiler()
    normal = compiler.compile(base_plan())
    forward = compiler.compile(forward_plan())

    actual_normal = normal.preview({"x": 2})
    actual_forward = forward.preview({"x": 2})
    expect(actual_normal == actual_forward == {"x": 2, "y": 3, "z": 4}, "forward reference")

    # dump -> JSON -> load must rebuild identical semantics and identity.
    serialized = json.dumps(normal.dump(), ensure_ascii=False, sort_keys=True)
    loaded = compiler.load(json.loads(serialized))
    expect(loaded.fingerprint == normal.fingerprint, "fingerprint changed after load")
    expect(loaded.preview({"x": 2}) == actual_normal, "semantics changed after load")
    expect(loaded.layout_fingerprint == normal.layout_fingerprint, "layout changed after load")

    record(
        "preflight-identity",
        normal=actual_normal,
        forward=actual_forward,
        fingerprint=normal.fingerprint,
        reloaded_fingerprint=loaded.fingerprint,
        dump_keys=sorted(normal.dump()),
        physical_columns=normal.physical_columns,
    )


def scenario_reject_bad_definitions():
    compiler = Compiler()

    cyclic = [
        col("a", kind="stored", expr={"col": "b"}),
        col("b", kind="virtual", expr={"col": "a"}),
    ]
    unknown = [col("v", kind="stored", expr={"col": "missing"})]
    bad_branch = [
        col("x"),
        col(
            "v",
            kind="virtual",
            expr={
                "op": "case",
                "when": {"op": "isnull", "args": [{"col": "x"}]},
                "then": {"col": "x"},
                "else": "text",
            },
        ),
    ]
    mixed_eq = [
        col("x"),
        col("t", type_name="text"),
        col(
            "v",
            kind="virtual",
            expr={
                "op": "case",
                "when": {"op": "eq", "args": [{"col": "x"}, {"col": "t"}]},
                "then": 1,
                "else": 0,
            },
        ),
    ]
    bool_constant = [col("v", kind="stored", expr=True)]
    base_with_expr = [col("x", expr=1)]
    bad_default = [col("x", default="nope")]
    too_many_columns = [col(f"c{i}") for i in range(17)]
    too_many_generated = [col(f"g{i}", kind="stored", expr=i) for i in range(9)]

    rejected = {}
    for label, definition in {
        "cycle": cyclic,
        "unknown-column": unknown,
        "bad-branch-type": bad_branch,
        "mixed-eq-types": mixed_eq,
        "bool-constant": bool_constant,
        "base-expression": base_with_expr,
        "bad-default": bad_default,
        "too-many-columns": too_many_columns,
        "too-many-generated": too_many_generated,
    }.items():
        error, message = raises(compiler.compile, definition)
        expect(error is not None, f"{label} was accepted")
        rejected[label] = error

    # A valid all-branch type check still permits a forward reference.
    compiler.compile(forward_plan())
    record("reject-bad-definitions", rejected=rejected, forward_reference="accepted")


def scenario_defaults_and_nulls():
    plan = Compiler().compile(
        [
            col("x", default=7),
            col("nx"),
            col("s", type_name="text", default="hey"),
            col("sum", kind="virtual", expr={"op": "+", "args": [{"col": "x"}, {"col": "nx"}]}),
            col(
                "label",
                type_name="text",
                kind="virtual",
                expr={"op": "concat", "args": [{"col": "s"}, "!"]},
            ),
            col(
                "picked",
                kind="virtual",
                expr={
                    "op": "coalesce",
                    "args": [
                        {"col": "nx"},
                        {"op": "+", "args": [{"col": "x"}, 1]},
                    ],
                },
            ),
        ]
    )
    missing = plan.preview({})
    explicit_null = plan.preview({"x": None})
    expect(missing["x"] == 7 and missing["s"] == "hey", "defaults not applied")
    expect(explicit_null["x"] is None, "explicit NULL fell back to default")
    expect(missing["sum"] is None and missing["label"] == "hey!", "NULL propagation")
    expect(missing["picked"] == 8, "coalesce did not pick the non-NULL side")
    expect(explicit_null["picked"] is None, "coalesce evaluated wrong side")
    record("defaults-nulls", missing_defaults=missing, explicit_null=explicit_null)


def scenario_short_circuit_and_overflow():
    # The unchosen branch contains an overflow; it must not be executed.
    plan = Compiler().compile(
        [
            col("x"),
            col(
                "guarded",
                kind="virtual",
                expr={
                    "op": "case",
                    "when": {"op": "lt", "args": [{"col": "x"}, 0]},
                    "then": {"op": "+", "args": [{"col": "x"}, INT64_MIN]},
                    "else": 5,
                },
            ),
            col(
                "fallback",
                kind="virtual",
                expr={
                    "op": "coalesce",
                    "args": [
                        {"col": "x"},
                        {"op": "-", "args": [INT64_MIN, 1]},
                    ],
                },
            ),
        ]
    )
    safe = plan.preview({"x": 1})
    expect(safe["guarded"] == 5, "unselected case branch executed")
    expect(safe["fallback"] == 1, "coalesce evaluated the non-needed side")

    error, message = raises(plan.preview, {"x": -1})
    expect(error is not None, "selected overflow branch was accepted")
    record(
        "short-circuit-overflow",
        unselected_case=safe,
        selected_overflow=error,
        selected_overflow_message=message,
    )


def scenario_input_permissions():
    plan = Compiler().compile(base_plan())
    error_generated, message_generated = raises(plan.preview, {"x": 1, "y": 2})
    error_unknown, message_unknown = raises(plan.preview, {"x": 1, "wat": 2})
    error_bool, _ = raises(plan.preview, {"x": True})

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(path, plan, [{"name": "by_x", "columns": ["x"], "unique": False}])
        expect(raises(store.apply, [{"op": "insert", "id": "a", "base": {"x": 1, "y": 2}}])[0] is not None,
               "insert accepted a generated field")
        expect(raises(store.apply, [{"op": "insert", "id": "a", "base": {"x": 1, "nope": 2}}])[0] is not None,
               "insert accepted an unknown field")
        store.apply([{"op": "insert", "id": "a", "base": {"x": 1}}])
        expect(raises(store.apply, [{"op": "patch", "id": "a", "base": {"y": 9}}])[0] is not None,
               "patch accepted a generated field")
        expect(store.scan()[0]["row"] == {"x": 1, "y": 2, "z": 3}, "failed writes leaked")
        store.close()

    record(
        "input-permissions",
        preview_generated=error_generated,
        preview_unknown=error_unknown,
        bool_rejected=error_bool,
        messages=[message_generated, message_unknown],
    )


def scenario_store_lifecycle():
    plan = Compiler().compile(base_plan())
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(
            path,
            plan,
            [
                {"name": "by_y", "columns": ["y"], "unique": False},
                {"name": "by_z", "columns": ["z"], "unique": True},
            ],
        )
        store.apply(
            [
                {"op": "insert", "id": "b", "base": {"x": 10}},
                {"op": "insert", "id": "a", "base": {"x": 1}},
            ]
        )
        virtual_eq = store.query("by_y", "eq", [2], ["x", "y", "z"])
        virtual_range = store.query("by_z", "range", [3, 12], ["z"])
        store.close()

        reopened = RowStore.open(path)
        reopened_virtual = reopened.query("by_y", "eq", [2], ["x", "y", "z"])
        reopened_range = reopened.query("by_z", "range", [3, 12], ["z"])
        scanned = reopened.scan()
        physical = json.loads(path.read_text(encoding="utf-8"))["rows"]
        expect(reopened_virtual == virtual_eq, "reopened query differs")
        expect(reopened_range == virtual_range, "reopened range differs")
        physical_columns = {row_id: sorted(row) for row_id, row in physical.items()}
        expect(physical_columns == {"a": ["x", "z"], "b": ["x", "z"]}, "virtual column persisted")
        expect(all("y" not in row for row in physical.values()), "virtual column persisted")
        # z(a)=3 is included; z(b)=12 is the exclusive upper bound.
        expect([entry["id"] for entry in reopened_range] == ["a"], "range result")

        # Patch: old index key must be withdrawn, new key added, same commit.
        reopened.apply([{"op": "patch", "id": "a", "base": {"x": 20}}])
        stale = reopened.query("by_y", "eq", [2])
        fresh = reopened.query("by_y", "eq", [21])
        inspected = reopened.inspect()
        expect(stale == [], "stale index key survived patch")
        expect([r["id"] for r in fresh] == ["a"], "new index key missing")
        expect(
            inspected["index_directory"]["entries"]["by_y"]
            == [{"ids": ["b"], "key": [11]}, {"ids": ["a"], "key": [21]}],
            "index directory content mismatch",
        )

        record(
            "store-lifecycle",
            virtual_eq=virtual_eq,
            virtual_range=virtual_range,
            reopened_scan=scanned,
            physical_columns=physical_columns,
            stale_after_patch=stale,
            fresh_after_patch=fresh,
            index_entries=inspected["index_directory"]["entries"],
            business_reads=inspected["business_reads"],
            index_reads=inspected["index_reads"],
        )
        reopened.close()


def scenario_patch_preserves_and_explicit_null():
    plan = Compiler().compile(
        [
            col("x", default=4),
            col("t", type_name="text", default="d"),
            col("v", kind="virtual", expr={"col": "x"}),
        ]
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(path, plan, [])
        store.apply([{"op": "insert", "id": "a", "base": {"x": 9}}])
        store.apply([{"op": "patch", "id": "a", "base": {"t": "q"}}])
        kept = store.scan()
        store.apply([{"op": "patch", "id": "a", "base": {"x": None}}])
        nulled = store.scan()
        expect(kept[0]["row"] == {"x": 9, "t": "q", "v": 9}, "omitted base not preserved")
        expect(nulled[0]["row"] == {"x": None, "t": "q", "v": None}, "explicit NULL not applied")
        record("patch-preserve-vs-null", after_partial=kept, after_explicit_null=nulled)
        store.close()


def scenario_unique_composite_nulls():
    plan = Compiler().compile(
        [
            col("a"),
            col("b"),
            col(
                "v",
                kind="virtual",
                expr={
                    "op": "case",
                    "when": {"op": "isnull", "args": [{"col": "a"}]},
                    "then": None,
                    "else": {"col": "a"},
                },
            ),
        ]
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(
            path,
            plan,
            [
                {"name": "ab", "columns": ["a", "b"], "unique": True},
                {"name": "v", "columns": ["v"], "unique": False},
            ],
        )
        store.apply(
            [
                {"op": "insert", "id": "n1", "base": {"a": None, "b": 1}},
                {"op": "insert", "id": "n2", "base": {"a": None, "b": 1}},
                {"op": "insert", "id": "n3", "base": {"a": 1, "b": None}},
                {"op": "insert", "id": "n4", "base": {"a": 1, "b": None}},
                {"op": "insert", "id": "u1", "base": {"a": 2, "b": 2}},
            ]
        )
        conflict, message = raises(
            store.apply, [{"op": "insert", "id": "u2", "base": {"a": 2, "b": 2}}]
        )
        expect(conflict is not None, "non-NULL duplicate unique key accepted")
        expect(store.query("ab", "eq", [None, 1]) and len(store.query("ab", "eq", [None, 1])) == 2,
               "NULL composite key not queryable")
        expect(len(store.query("ab", "eq", [1, None])) == 2, "NULL composite key not queryable")
        expect([r["id"] for r in store.query("ab", "eq", [None, None])] == [], "unexpected NULL key")

        record(
            "unique-composite-nulls",
            null_keys=[r["id"] for r in store.query("ab", "eq", [None, 1])],
            unique_conflict=conflict,
            message=message,
            rows_after=[entry["id"] for entry in store.scan()],
        )
        store.close()


def scenario_batch_atomicity():
    plan = Compiler().compile(base_plan())
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(
            path,
            plan,
            [
                {"name": "by_y", "columns": ["y"], "unique": True},
                {"name": "by_z", "columns": ["z"], "unique": False},
            ],
        )
        store.apply(
            [
                {"op": "insert", "id": "a", "base": {"x": 1}},
                {"op": "insert", "id": "b", "base": {"x": 5}},
            ]
        )
        before_rows = store.scan()
        before_entries = store.inspect()["index_directory"]["entries"]
        before_file = path.read_text(encoding="utf-8")

        # First two patches succeed; the third moves "a" onto b's *new* key
        # inside the same batch.  The whole batch must roll back: a keeps x=1
        # and both index directories stay put.
        error, message = raises(
            store.apply,
            [
                {"op": "patch", "id": "a", "base": {"x": 2}},
                {"op": "patch", "id": "b", "base": {"x": 4}},
                {"op": "patch", "id": "a", "base": {"x": 4}},
            ],
        )
        after_rows = store.scan()
        after_entries = store.inspect()["index_directory"]["entries"]
        after_file = path.read_text(encoding="utf-8")
        expect(error is not None, "conflicting final patch accepted")
        expect(after_rows == before_rows, "failed batch changed business rows")
        expect(after_entries == before_entries, "failed batch changed indexes")
        expect(after_file == before_file, "failed batch changed the file")
        expect([r["id"] for r in store.query("by_y", "eq", [2])] == ["a"], "rolled-back key wrong")
        expect([r["id"] for r in store.query("by_y", "eq", [6])] == ["b"], "old key lost")
        expect(store.query("by_y", "eq", [5]) == [], "orphan key left behind")

        # Ops in one batch see earlier ops in the same batch.
        store.apply(
            [
                {"op": "insert", "id": "c", "base": {"x": 10}},
                {"op": "patch", "id": "c", "base": {"x": 20}},
            ]
        )
        seen_by_later_op = store.query("by_y", "eq", [21])
        expect([r["id"] for r in seen_by_later_op] == ["c"], "later op did not see earlier op")

        too_many = [
            {"op": "insert", "id": f"x{i}", "base": {"x": i}} for i in range(101)
        ]
        limit_error, _ = raises(store.apply, too_many)
        expect(limit_error is not None, "101 operations accepted")
        expect(len(store.scan()) == 3, "over-limit batch mutated rows")

        record(
            "batch-atomicity",
            conflict=error,
            message=message,
            rows_unchanged=after_rows == before_rows,
            indexes_unchanged=after_entries == before_entries,
            file_unchanged=after_file == before_file,
            same_batch_visibility=[r["id"] for r in seen_by_later_op],
            over_limit=limit_error,
            final_ids=[entry["id"] for entry in store.scan()],
        )
        store.close()


def scenario_range_and_selectivity():
    plan = Compiler().compile(base_plan())
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(
            path,
            plan,
            [{"name": "by_y", "columns": ["y"], "unique": False}],
        )
        operations = [
            {"op": "insert", "id": f"r{i:04d}", "base": {"x": i}} for i in range(1000) if i != 500
        ]
        for start in range(0, len(operations), 100):
            store.apply(operations[start:start + 100])
        store.apply([{"op": "insert", "id": "null", "base": {"x": None}}])

        null_row = store.query("by_y", "eq", [None])
        expect([r["id"] for r in null_row] == ["null"], "NULL key not queryable")
        window = store.query("by_y", "range", [100, 103], ["x", "y"])
        expect([r["id"] for r in window] == ["r0099", "r0100", "r0101"], "range mismatch")
        expect(all(r["row"]["y"] is not None for r in window), "NULL entered range result")
        bad_bound, _ = raises(store.query, "by_y", "range", [None, 5])
        expect(bad_bound is not None, "NULL range bound accepted")
        mixed_bound, _ = raises(store.query, "by_y", "range", [1, "5"])
        expect(mixed_bound is not None, "mixed-type range bound accepted")

        selective_query_reads = store.business_reads
        selective_index_reads = store.index_reads
        one = store.query("by_y", "eq", [n for n in [999] if n != 500], ["x"])
        # x=998 -> y=999, exactly one row.
        business_delta = store.business_reads - selective_query_reads
        index_delta = store.index_reads - selective_index_reads
        expect(len(one) == 1, "selective query did not return one row")
        expect(business_delta <= 2, f"selective query read {business_delta} business rows")
        expect(index_delta >= 1, "index access was not recorded")

        record(
            "range-and-selectivity",
            null_keys=[r["id"] for r in null_row],
            range_ids=[r["id"] for r in window],
            range_rows=[r["row"] for r in window],
            select_one=one,
            business_rows_read=business_delta,
            index_entries_examined=index_delta,
            null_bound_rejected=bad_bound,
            mixed_bound_rejected=mixed_bound,
        )
        store.close()


def scenario_corruption_rejected():
    plan = Compiler().compile(base_plan())
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(path, plan, [{"name": "by_y", "columns": ["y"], "unique": False}])
        store.apply([{"op": "insert", "id": "a", "base": {"x": 2}}])
        store.close()
        original = json.loads(path.read_text(encoding="utf-8"))

        def tampered(mutator):
            payload = copy.deepcopy(original)
            mutator(payload)
            bad_path = Path(td) / "bad.json"
            bad_path.write_text(json.dumps(payload), encoding="utf-8")
            return raises(RowStore.open, bad_path)

        cases = {
            "schema-fingerprint": tampered(
                lambda p: p["schema"].__setitem__("fingerprint", "0" * 64)
            ),
            "layout": tampered(lambda p: p["layout"].__setitem__("physical_columns", ["x"])),
            "index-entries": tampered(
                lambda p: p["index_directory"].__setitem__("entries_fingerprint", "0" * 64)
            ),
            "storage-identity": tampered(
                lambda p: p.__setitem__("storage_fingerprint", "0" * 64)
            ),
            "row-layout": tampered(
                lambda p: p["rows"]["a"].pop("z")
            ),
            "row-type": tampered(
                lambda p: p["rows"]["a"].__setitem__("x", "two")
            ),
            "stored-mismatch": tampered(
                lambda p: p["rows"]["a"].__setitem__("z", 99)
            ),
            "virtual-on-disk": tampered(
                lambda p: p["rows"]["a"].__setitem__("y", 3)
            ),
        }
        for label, (error, _message) in cases.items():
            expect(error is not None, f"corrupt {label} was accepted")

        truncated = Path(td) / "truncated.json"
        truncated.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")
        trunc_error, _ = raises(RowStore.open, truncated)

        record(
            "corruption-rejected",
            rejected={label: error for label, (error, _message) in cases.items()},
            truncated=trunc_error,
        )


def scenario_independent_interpreter():
    columns = [
        col("x"),
        col("t", type_name="text"),
        col("nx"),
        col("both", kind="virtual", expr={"op": "coalesce", "args": [{"col": "nx"}, {"col": "x"}]}),
        col(
            "tag",
            type_name="text",
            kind="stored",
            expr={"op": "concat", "args": [{"col": "t"}, "!"]},
        ),
        col(
            "pick",
            kind="stored",
            expr={
                "op": "case",
                "when": {"op": "isnull", "args": [{"col": "nx"}]},
                "then": {"col": "both"},
                "else": {"op": "-", "args": [{"col": "x"}, 1]},
            },
        ),
        col(
            "deep",
            kind="virtual",
            expr={"op": "+", "args": [{"col": "pick"}, {"col": "both"}]},
        ),
    ]
    # Declare the dependency graph out of order to exercise forward references.
    declared = [columns[0], columns[1], columns[2], columns[6], columns[4], columns[5], columns[3]]
    plan = Compiler().compile(declared)
    mini = MiniInterpreter(declared)

    inputs = [
        {"x": 0, "t": "a", "nx": None},
        {"x": 4, "t": "b", "nx": 9},
        {"x": None, "t": "c", "nx": None},
        {"t": "d"},
        {"x": 7, "t": "", "nx": None},
    ]

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(
            path,
            plan.dump(),
            [
                {"name": "by_deep", "columns": ["deep"], "unique": False},
                {"name": "by_pair", "columns": ["tag", "pick"], "unique": False},
            ],
        )
        for position, base in enumerate(inputs):
            expected = mini.full_row(base)
            actual = plan.preview(base)
            expect(actual == expected, f"preview mismatch for {base}")
            store.apply([{"op": "insert", "id": f"i{position}", "base": base}])

        store.close()
        reopened = RowStore.open(path)
        scanned_rows = {entry["id"]: entry["row"] for entry in reopened.scan()}
        for position, base in enumerate(inputs):
            row_id = f"i{position}"
            expected = mini.full_row(base)
            expect(scanned_rows[row_id] == expected, f"scan mismatch for {row_id}")
            by_deep = reopened.query("by_deep", "eq", [expected["deep"]])
            expect(row_id in [r["id"] for r in by_deep], f"index query mismatch for {row_id}")
            by_pair = reopened.query("by_pair", "eq", [expected["tag"], expected["pick"]])
            expect(row_id in [r["id"] for r in by_pair], f"composite index query mismatch for {row_id}")
            stored = reopened.inspect()["physical_rows"][row_id]
            expect(stored == {"x": base.get("x"), "t": base.get("t", None),
                              "nx": base.get("nx"), "tag": expected["tag"], "pick": expected["pick"]},
                   f"physical row mismatch for {row_id}")

        record(
            "independent-interpreter",
            inputs=inputs,
            previews=[mini.full_row(base) for base in inputs],
            scan_matches=True,
            index_matches=True,
            physical_rows=reopened.inspect()["physical_rows"],
            physical_columns=reopened.inspect()["physical_columns"],
        )
        reopened.close()


def scenario_capacity_and_six_indexes():
    # The declared envelope: 2000 rows, 6 indexes (including a virtual column
    # and a two-column index).
    plan = Compiler().compile(
        [
            col("x"),
            col("t", type_name="text"),
            col("y", kind="virtual", expr={"op": "+", "args": [{"col": "x"}, 1]}),
            col("z", kind="stored", expr={"op": "+", "args": [{"col": "y"}, 1]}),
            col("tag", type_name="text", kind="stored",
                expr={"op": "concat", "args": [{"col": "t"}, "!"]}),
        ]
    )
    indexes = [
        {"name": "by_y", "columns": ["y"], "unique": False},
        {"name": "by_z", "columns": ["z"], "unique": True},
        {"name": "by_x_t", "columns": ["x", "t"], "unique": False},
        {"name": "by_tag", "columns": ["tag"], "unique": False},
        {"name": "by_x_tag", "columns": ["x", "tag"], "unique": False},
        {"name": "by_z_y", "columns": ["z", "y"], "unique": False},
    ]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(path, plan, indexes)
        operations = [
            {"op": "insert", "id": f"r{i:04d}", "base": {"x": i, "t": f"t{i % 3}"}}
            for i in range(2000)
        ]
        for start in range(0, 2000, 100):
            store.apply(operations[start:start + 100])
        expect(len(store.scan()) == 2000, "capacity insert failed")
        store.close()

        reopened = RowStore.open(path)
        before_business = reopened.business_reads
        before_index = reopened.index_reads
        one = reopened.query("by_z", "eq", [1234], ["x", "y", "z"])
        business_delta = reopened.business_reads - before_business
        index_delta = reopened.index_reads - before_index
        expect([r["id"] for r in one] == ["r1232"], "unique select failed")
        expect(business_delta <= 2, "selective read exceeded 2 business rows")

        virtual_hit = reopened.query("by_y", "eq", [1501], ["z"])
        composite = reopened.query("by_x_t", "eq", [1500, "t0"], ["tag"])
        range_hit = reopened.query("by_y", "range", [1000, 1003], ["x", "y"])
        entries = {name: len(snapshot) for name, snapshot in
                   reopened.inspect()["index_directory"]["entries"].items()}
        expect(len(entries) == 6, "index directory count")

        record(
            "capacity-six-indexes",
            rows=len(reopened.scan()),
            index_bucket_counts=entries,
            selective=one,
            business_rows_read=business_delta,
            index_entries_examined=index_delta,
            virtual_hit=virtual_hit,
            composite_hit=composite,
            range_ids=[r["id"] for r in range_hit],
        )
        reopened.close()


def scenario_generated_constraints_at_write():
    # NULL/type constraints on generated values are enforced when a row is
    # evaluated, including writes through the store.
    required_plan = Compiler().compile(
        [
            col("nx"),
            col("required", kind="virtual", nullable=False, expr={"col": "nx"}),
        ]
    )
    nullable_error, nullable_message = raises(required_plan.preview, {"nx": None})
    expect(nullable_error is not None, "non-nullable generated column produced NULL")
    plan = Compiler().compile(
        [
            col("x"),
            col("nx"),
            col("big", kind="stored", expr={"op": "+", "args": [{"col": "x"}, INT64_MAX]}),
            col("s", type_name="text"),
            col("long", type_name="text", kind="stored",
                expr={"op": "concat", "args": [{"col": "s"}, {"col": "s"}]}),
        ]
    )
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "store.json"
        store = RowStore.create(path, plan, [{"name": "by_big", "columns": ["big"], "unique": False}])
        before = path.read_text(encoding="utf-8")

        # Stored overflow in the *second* operation rolls back the first.
        error, message = raises(
            store.apply,
            [
                {"op": "insert", "id": "ok", "base": {"x": 0, "nx": 1, "s": "a"}},
                {"op": "insert", "id": "bad", "base": {"x": 1, "nx": 1, "s": "a"}},
            ],
        )
        expect(error is not None, "stored overflow accepted")
        expect(store.scan() == [], "failed generated batch committed a row")
        expect(path.read_text(encoding="utf-8") == before, "failed generated batch touched disk")
        expect(store.query("by_big", "eq", [INT64_MAX]) == [], "index side effect after failure")

        # Concat over 128 code points is rejected.
        text_error, text_message = raises(
            store.apply, [{"op": "insert", "id": "t", "base": {"x": 0, "nx": 1, "s": "a" * 100}}]
        )
        expect(text_error is not None, "over-long concat accepted")

        store.apply([{"op": "insert", "id": "ok", "base": {"x": 0, "nx": 1, "s": "a"}}])
        record(
            "generated-write-constraints",
            overflow=error,
            overflow_message=message,
            nullable_violation=nullable_error,
            nullable_message=nullable_message,
            length_violation=text_error,
            length_message=text_message,
            rows_after=[entry["id"] for entry in store.scan()],
            physical_rows=store.inspect()["physical_rows"],
        )
        store.close()


def scenario_cross_kind_dependencies():
    # stored and virtual may depend on each other in either direction as long
    # as the whole graph is acyclic, and declaration order may be reversed.
    columns = [
        col("x"),
        col("sv", kind="stored", expr={"op": "+", "args": [{"col": "vy"}, 1]}),
        col("vy", kind="virtual", expr={"op": "-", "args": [{"col": "x"}, 1]}),
        col("zv", kind="virtual", expr={"op": "+", "args": [{"col": "sv"}, {"col": "vy"}]}),
    ]
    plan = Compiler().compile(columns)
    preview = plan.preview({"x": 10})
    expect(preview == {"x": 10, "sv": 10, "vy": 9, "zv": 19}, "cross-kind acyclic plan")

    tampered = plan.dump()
    tampered["fingerprint"] = "0" * 64
    error, message = raises(Compiler().load, tampered)

    record(
        "cross-kind-dependencies",
        preview=preview,
        physical_columns=plan.physical_columns,
        tampered_load=error,
        tampered_message=message,
    )


def scenario_column_limits():
    sixteen = Compiler().compile(
        [col(f"c{i}") for i in range(8)]
        + [col(f"g{i}", kind="virtual", expr=i) for i in range(8)]
    )
    record(
        "column-limits",
        columns=len(sixteen.columns),
        generated=len(sixteen.generated_columns),
        physical_columns=sixteen.physical_columns,
    )


def main():
    scenario_preflight_and_identity()
    scenario_reject_bad_definitions()
    scenario_defaults_and_nulls()
    scenario_short_circuit_and_overflow()
    scenario_input_permissions()
    scenario_store_lifecycle()
    scenario_patch_preserves_and_explicit_null()
    scenario_unique_composite_nulls()
    scenario_batch_atomicity()
    scenario_range_and_selectivity()
    scenario_corruption_rejected()
    scenario_independent_interpreter()
    scenario_capacity_and_six_indexes()
    scenario_generated_constraints_at_write()
    scenario_cross_kind_dependencies()
    scenario_column_limits()
    print(json.dumps({"scenario": "summary", "records": len(RECORDS), "status": "ok"}))


if __name__ == "__main__":
    main()
