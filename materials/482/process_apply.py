"""Process B: consume only the JSON plan artifacts and write the real DB.

This process does not call bind(); it reloads the BoundPlan from disk, proves
it is plain data (json round trip once more), then hands it to execution.apply.
The database is created fresh here, so its catalog identity is checked against
the digest stored inside the plan.
"""
import json
import os
import sqlite3
import tempfile

from database import create
from execution import apply, inspect

HERE = os.path.dirname(__file__)


def main():
    with open(os.path.join(HERE, "artifacts", "bound_plan.json")) as handle:
        plan = json.load(handle)
    with open(os.path.join(HERE, "artifacts", "source_rows.json")) as handle:
        rows = json.load(handle)
    # Simulate arrival in yet another process: serialize/deserialize again.
    plan = json.loads(json.dumps(plan))

    db_path = os.path.join(tempfile.mkdtemp(), "contacts.sqlite")
    db = create(db_path)
    try:
        result = apply(db, plan, rows)
        table = inspect(db)
    finally:
        db.close()

    out = {
        "db_path": db_path,
        "receipts": result["receipts"],
        "final_rows": result["final_rows"],
        "table": table,
        "snapshot_n_history": [r["value"]["n"] for r in result["receipts"]],
    }
    print(json.dumps(out, sort_keys=True, indent=1))


if __name__ == "__main__":
    main()
