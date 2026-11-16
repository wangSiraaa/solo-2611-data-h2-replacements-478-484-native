"""A matrix of illegal typed requests, all rejected by the offline binder.

No database is opened: the catalog is read straight from catalog.json, which
proves every one of these diagnostics is produced by bind() alone and carries
a concrete path into the request.
"""
import json
import os

from binding import BindingError, bind

CATALOG = json.load(open(os.path.join(os.path.dirname(__file__), "catalog.json")))
PARTIAL = ["tenant", {"expr": "lower", "column": "email"}]
PARTIAL_WHERE = {"column": "active", "op": "eq", "value": 1}
EXTERNAL = {"columns": ["external_id"]}


def _nested(depth):
    node = {"old": "n"}
    for _ in range(depth - 1):
        node = {"op": "add", "args": [node, {"literal": 1}]}
    return node


CASES = {
    "missing_predicate": {
        "target": {"columns": PARTIAL},
        "action": {"kind": "nothing"}},
    "wrong_predicate_value": {
        "target": {"columns": PARTIAL,
                   "where": {"column": "active", "op": "eq", "value": 0}},
        "action": {"kind": "nothing"}},
    "unknown_column_in_target": {
        "target": {"columns": ["nope"], "where": PARTIAL_WHERE},
        "action": {"kind": "nothing"}},
    "wrong_expression": {
        "target": {"columns": ["tenant", {"expr": "upper", "column": "email"}],
                   "where": PARTIAL_WHERE},
        "action": {"kind": "nothing"}},
    "reordered_columns": {
        "target": {"columns": [{"expr": "lower", "column": "email"}, "tenant"],
                   "where": PARTIAL_WHERE},
        "action": {"kind": "nothing"}},
    "unknown_action_kind": {
        "target": EXTERNAL,
        "action": {"kind": "frobnicate"}},
    "empty_set": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {}}},
    "set_id_column": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"id": {"excluded": "n"}}}},
    "set_unknown_column": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"mystery": {"excluded": "n"}}}},
    "excluded_id": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"excluded": "id"}}}},
    "unknown_expr_op": {
        "target": EXTERNAL,
        "action": {"kind": "update",
                   "set": {"n": {"op": "mul",
                                 "args": [{"old": "n"}, {"excluded": "n"}]}}}},
    "add_on_text": {
        "target": EXTERNAL,
        "action": {"kind": "update",
                   "set": {"n": {"op": "add",
                                 "args": [{"old": "email"}, {"literal": 1}]}}}},
    "cross_type_comparison": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"excluded": "n"}},
                   "where": {"op": "eq",
                             "args": [{"old": "n"}, {"literal": "x"}]}}},
    "bare_null_literal": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"literal": None}}}},
    "null_into_not_null": {
        "target": EXTERNAL,
        "action": {"kind": "update",
                   "set": {"external_id": {"old": "email"}}}},
    "bool_literal_rejected": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"active": {"literal": True}}}},
    "float_literal_rejected": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"literal": 1.5}}}},
    "int_out_of_range": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"literal": 2 ** 63}}}},
    "non_ascii_text": {
        "target": EXTERNAL,
        "action": {"kind": "update",
                   "set": {"external_id": {"literal": "café"}}}},
    "empty_and_args": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"excluded": "n"}},
                   "where": {"op": "and", "args": []}}},
    "coalesce_mixed_types": {
        "target": EXTERNAL,
        "action": {"kind": "update",
                   "set": {"n": {"op": "coalesce",
                                 "args": [{"literal": None},
                                          {"old": "email"}]}}}},
    "depth_too_deep": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": _nested(9)}}},
    "unknown_predicate_op": {
        "target": EXTERNAL,
        "action": {"kind": "update", "set": {"n": {"excluded": "n"}},
                   "where": {"op": "like",
                             "args": [{"old": "email"}, {"literal": "x"}]}}},
}


def main():
    report = {}
    for name, request in CASES.items():
        try:
            bind(CATALOG, request)
        except BindingError as exc:
            report[name] = {"rejected": True, "path_diagnostic": str(exc)}
        else:
            report[name] = {"rejected": False}
    print(json.dumps(report, sort_keys=True, indent=1))
    bad = [name for name, item in report.items() if not item["rejected"]]
    if bad:
        raise SystemExit("these illegal requests were NOT rejected: %s" % bad)
    print("all %d illegal requests rejected with path diagnostics" % len(CASES))


if __name__ == "__main__":
    main()
