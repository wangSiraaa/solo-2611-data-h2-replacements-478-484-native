"""Process A: bind offline and persist only JSON artifacts (no DB write).

Reads the catalog JSON and a typed request, writes bound_plan.json and
source_rows.json. This process never opens SQLite and never imports the
execution package.
"""
import json
import os
import sys

from binding import bind

HERE = os.path.dirname(__file__)


def main():
    catalog = json.load(open(os.path.join(HERE, "catalog.json")))
    request = {
        "target": {
            "columns": ["tenant", {"expr": "lower", "column": "email"}],
            "where": {"column": "active", "op": "eq", "value": 1},
        },
        "action": {
            "kind": "update",
            "set": {"n": {"op": "add",
                          "args": [{"old": "n"}, {"excluded": "n"}]}},
            "where": {"op": "ge",
                      "args": [{"old": "n"}, {"literal": 0}]},
        },
    }
    plan = bind(catalog, request)
    rows = [
        {"tenant": "t", "email": "p@x", "active": 1,
         "external_id": "p1", "n": 10},
        {"tenant": "t", "email": "P@X", "active": 1,
         "external_id": "p2", "n": 5},
    ]
    out_dir = os.path.join(HERE, "artifacts")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "bound_plan.json"), "w") as handle:
        json.dump(plan, handle, sort_keys=True)
    with open(os.path.join(out_dir, "source_rows.json"), "w") as handle:
        json.dump(rows, handle)
    print(json.dumps({"bound": True, "target_index": plan["target_index"],
                      "format": plan["format"]}))


if __name__ == "__main__":
    sys.exit(main())
