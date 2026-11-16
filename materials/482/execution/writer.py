"""Source-ordered controlled UPSERT against a bound plan.

Conflict arbitration uses an explicit lookup of *only* the plan's target
index. When the target is missed the row is inserted plainly and SQLite
enforces every unique constraint. When the target is hit the action (nothing
or a controlled update) applies; if the proposed row also hits a *different*
row through another unique index, the plain INSERT is attempted so that the
real constraint -- not this layer -- judges the action illegal. Thus one
input can never have one of two distinct conflicting rows picked for update.
"""
import sqlite3

from binding.catalog import (
    ACTIVE_EMAIL_TARGET,
    COLUMNS,
    EXTERNAL_ID_TARGET,
    describe,
)
from binding.errors import ExecutionError, PlanError
from binding.types import SOURCE_COLUMNS, type_accepts
from receipt import result

from .evaluator import check_assigned_value, eval_expr, eval_predicate
from .planner import validate_plan

MAX_ROWS = 500
_INSERT_SQL = (
    "INSERT INTO contacts(tenant,email,active,external_id,n) "
    "VALUES(:tenant,:email,:active,:external_id,:n)"
)
# Parameterized, identifier-quoted statement templates. Identifiers only ever
# come from the validated plan's assignable column names, never from input.
_UPDATE_SQL = "UPDATE contacts SET {sets} WHERE id=:__id"


def _validate_source_row(row, source_no):
    if not isinstance(row, dict):
        raise ExecutionError(source_no, "source row must be an object")
    if "id" in row:
        raise ExecutionError(source_no, "source rows must not carry id")
    extra = set(row) - set(SOURCE_COLUMNS)
    if extra:
        raise ExecutionError(source_no, "unknown source column(s) %s" % sorted(extra))
    for col in SOURCE_COLUMNS:
        if col not in row:
            raise ExecutionError(source_no, "source row missing column %r" % col)
        if not type_accepts(COLUMNS[col], row[col]):
            raise ExecutionError(source_no,
                                 "column %r rejects value %r" % (col, row[col]))


def _active_email_keys(row):
    """The proposed row can only enter the partial index when active=1 and
    its email is a non-NULL ASCII string (NULL never participates)."""
    return row["active"] == 1 and row["email"] is not None


def _target_lookup(db, target_index, row):
    """Return the target row id according to the target index semantics."""
    if target_index == EXTERNAL_ID_TARGET:
        sql = "SELECT * FROM contacts WHERE external_id=? LIMIT 1"
        params = (row["external_id"],)
    else:
        if not _active_email_keys(row):
            return None, None
        # The partial index keys (tenant, lower(email)) only for active=1.
        sql = (
            "SELECT * FROM contacts WHERE tenant=? AND active=1 "
            "AND email IS NOT NULL AND lower(email)=lower(?) LIMIT 1"
        )
        params = (row["tenant"], row["email"])
    found = db.execute(sql, params).fetchone()
    return None if found is None else found["id"], found


def _other_unique_hit(db, target_index, row):
    """Row id colliding on the unique index that is NOT the target."""
    if target_index == EXTERNAL_ID_TARGET:
        if not _active_email_keys(row):
            return None
        found = db.execute(
            "SELECT id FROM contacts WHERE tenant=? AND active=1 "
            "AND email IS NOT NULL AND lower(email)=lower(?) LIMIT 1",
            (row["tenant"], row["email"]),
        ).fetchone()
    else:
        found = db.execute(
            "SELECT id FROM contacts WHERE external_id=? LIMIT 1",
            (row["external_id"],),
        ).fetchone()
    return None if found is None else found["id"]


def _fetch(db, rid, source_no):
    current = db.execute(
        "SELECT id,tenant,email,active,external_id,n FROM contacts WHERE id=?",
        (rid,),
    ).fetchone()
    if current is None:
        raise ExecutionError(source_no, "row id=%s vanished during the batch" % rid)
    return dict(current)


def apply(db, plan, rows):
    catalog = describe(db)
    validate_plan(plan, catalog)

    if not isinstance(rows, list):
        raise PlanError("rows: must be a list of source rows")
    if len(rows) > MAX_ROWS:
        raise PlanError("rows: batch exceeds the %d row limit" % MAX_ROWS)
    # Validate every source row before opening the write transaction so an
    # illegal row can never produce a partial batch.
    for source_no, row in enumerate(rows):
        _validate_source_row(row, source_no)

    target_index = plan["target_index"]
    action = plan["action"]
    receipts = []

    try:
        with db:
            for source_no, row in enumerate(rows):
                rid, target_row = _target_lookup(db, target_index, row)

                if rid is None:
                    # Target miss -> INSERT. SQLite is the sole authority for
                    # every unique constraint here, including a simultaneous
                    # collision on the other index (one or two distinct rows).
                    try:
                        cursor = db.execute(_INSERT_SQL, row)
                    except sqlite3.Error as exc:
                        raise ExecutionError(
                            source_no, "insert violated a database constraint", exc
                        )
                    new_id = cursor.lastrowid
                    receipts.append(
                        result(source_no, new_id, "insert", _fetch(db, new_id, source_no))
                    )
                    continue

                # Target hit. Arbitrate every other unique constraint before
                # applying the action: if the proposed row also collides on
                # another unique index (with the target row itself or a
                # distinct row), a plain INSERT hands the decision to SQLite's
                # real constraint. This mirrors native ON CONFLICT, where such
                # a conflict never reaches DO UPDATE / DO NOTHING, and makes
                # it impossible to pick one of two distinct rows to update.
                other_id = _other_unique_hit(db, target_index, row)
                if other_id is not None:
                    try:
                        db.execute(_INSERT_SQL, row)
                    except sqlite3.Error as exc:
                        raise ExecutionError(
                            source_no,
                            "another unique index also conflicts "
                            "(other_id=%s, target_id=%s)" % (other_id, rid),
                            exc,
                        )
                    raise ExecutionError(
                        source_no,
                        "expected constraint failure did not occur",
                    )

                if action["kind"] == "nothing":
                    # Only the target conflict is skipped. The distinct-row
                    # case above was handed to the real constraint already.
                    receipts.append(
                        result(source_no, rid, "skipped", _fetch(db, rid, source_no))
                    )
                    continue

                where = action["where"]
                if where is not None and eval_predicate(
                    where, source_no, target_row, row
                ) is not True:
                    # Three-valued WHERE: only TRUE updates; FALSE and NULL skip.
                    receipts.append(
                        result(source_no, rid, "skipped", _fetch(db, rid, source_no))
                    )
                    continue

                assignments = {}
                for column, node in action["set"].items():
                    value = eval_expr(node, source_no, target_row, row)
                    check_assigned_value(
                        column, COLUMNS[column], value, source_no
                    )
                    assignments[column] = value

                sets = ", ".join('"%s"=:%s' % (col, col) for col in assignments)
                params = dict(assignments)
                params["__id"] = rid
                try:
                    cursor = db.execute(_UPDATE_SQL.format(sets=sets), params)
                except sqlite3.Error as exc:
                    raise ExecutionError(
                        source_no,
                        "update violated a database constraint or type rule",
                        exc,
                    )
                if cursor.rowcount != 1:
                    raise ExecutionError(
                        source_no,
                        "update affected %d rows, expected exactly target id=%s"
                        % (cursor.rowcount, rid),
                    )
                # Snapshot at action end: later source rows must not rewrite it.
                receipts.append(
                    result(source_no, rid, "update", _fetch(db, rid, source_no))
                )
    except ExecutionError:
        raise
    except sqlite3.Error as exc:
        raise ExecutionError(-1, "batch aborted by database error", exc)

    # Commit has succeeded. Batch-end values are read separately from the
    # per-action snapshots, over the ids touched by the batch, id ordered.
    touched = sorted({receipt["id"] for receipt in receipts})
    final_rows = [
        dict(
            db.execute(
                "SELECT id,tenant,email,active,external_id,n "
                "FROM contacts WHERE id=?",
                (rid,),
            ).fetchone()
        )
        for rid in touched
    ]
    return {"receipts": receipts, "final_rows": final_rows}


def inspect(db):
    """Read-only view of the full current table for post-commit checks."""
    return [
        dict(row)
        for row in db.execute(
            "SELECT id,tenant,email,active,external_id,n FROM contacts ORDER BY id"
        )
    ]
