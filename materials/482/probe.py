"""End-to-end verification of the offline binding / controlled UPSERT pair.

Runs entirely against a real SQLite database plus an independent offline
binding client. Every scenario asserts on per-source receipts (action-end
snapshots) and on post-commit SELECT values, so action history and
batch-end state are checked independently.
"""
import copy
import json
import os
import tempfile

from binding import BindingError, PlanError, bind, describe, identity
from binding.errors import ExecutionError
from database import create
from execution import apply, inspect
from importer import import_rows
from preview import preview

ACTIVE_EMAIL_TARGET = {
    "columns": ["tenant", {"expr": "lower", "column": "email"}],
    "where": {"column": "active", "op": "eq", "value": 1},
}
EXTERNAL_TARGET = {"columns": ["external_id"]}
UPDATE_N = {"kind": "update", "set": {"n": {"excluded": "n"}}}
RESULTS = []
ARTIFACT_DIR = os.path.join(os.path.dirname(__file__), "artifacts")


def check(name, condition, detail):
    RESULTS.append({"scenario": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError("%s failed: %s" % (name, detail))


def row(email, external, n, tenant="t", active=1):
    return dict(tenant=tenant, email=email, active=active,
                external_id=external, n=n)


def expect_error(name, fn, error_type):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - assertion needs any failure
        ok = type(exc).__name__ == error_type
        check(name, ok, {"raised": type(exc).__name__, "message": str(exc)})
        return exc
    check(name, False, {"raised": None})


def save_artifact(label, payload):
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR, label + ".json"), "w") as handle:
        json.dump(payload, handle, sort_keys=True, indent=1)


def scenario_baseline(db):
    """Original probe flow: two source rows, two action snapshots."""
    request = {"target": ACTIVE_EMAIL_TARGET, "action": UPDATE_N}
    out = import_rows(db, request, [row("a@x", "x1", 1), row("A@X", "x2", 2)])
    receipts = out["receipts"]
    check("baseline.receipt_actions",
          [r["action"] for r in receipts] == ["insert", "update"],
          [r["action"] for r in receipts])
    check("baseline.snapshot_n_history",
          [r["value"]["n"] for r in receipts] == [1, 2],
          [r["value"]["n"] for r in receipts])
    check("baseline.same_target_id", receipts[0]["id"] == receipts[1]["id"],
          [r["id"] for r in receipts])
    # lower(email) is ASCII case-insensitive: second row hit the same target.
    check("baseline.final_n_is_2", out["final_rows"] == inspect(db),
          {"final_rows": out["final_rows"], "table": inspect(db)})
    check("baseline.final_single_row",
          inspect(db) == [{
              "id": 1, "tenant": "t", "email": "a@x", "active": 1,
              "external_id": "x1", "n": 2,
          }], inspect(db))
    save_artifact("baseline_plan", bind(describe(db), request))
    save_artifact("baseline_receipts", out)


def scenario_offline_missing_predicate(catalog):
    """Independent preview client, no DB: missing partial predicate rejected."""
    bad = {"target": {"columns": ["tenant", {"expr": "lower", "column": "email"}]},
           "action": UPDATE_N}
    exc = expect_error("offline.missing_predicate",
                       lambda: preview(catalog, bad), "BindingError")
    check("offline.missing_predicate_path", "target.where" in str(exc), str(exc))


def scenario_non_target_conflict(db):
    """Plan round-trips through JSON; non-target unique conflict rolls back."""
    before = inspect(db)
    plan = json.loads(json.dumps(bind(describe(db), {
        "target": ACTIVE_EMAIL_TARGET, "action": UPDATE_N})))
    exc = expect_error("apply.non_target_unique_conflict",
                       lambda: apply(db, plan, [row("b@x", "x1", 9)]),
                       "ExecutionError")
    check("apply.non_target_conflict_message",
          "constraint" in str(exc), str(exc))
    after = inspect(db)
    check("apply.non_target_conflict_rollback", after == before,
          {"before": before, "after": after})
    existing = next(r for r in after if r["external_id"] == "x1")
    check("apply.original_n_unchanged", existing["n"] == 2, existing)
    # No success receipts survive a failed batch.
    check("apply.no_receipts_on_failure", not hasattr(exc, "receipts"),
          {"type": type(exc).__name__})


def scenario_inactive_rows(db):
    """Inactive rows are outside the partial index: an inactive duplicate of
    an active email coexists, while a second ACTIVE duplicate is rejected by
    the real active_email index. Here external_id is the arbitration target,
    making active_email the independently enforced 'other' unique index."""
    target = bind(describe(db), {"target": EXTERNAL_TARGET,
                                 "action": {"kind": "nothing"}})
    # Existing active row (a@x / x1) already present from the baseline.
    # Inactive row with the same email but a fresh external id: the partial
    # index excludes active=0, so the real constraints allow it.
    out = apply(db, target, [row("a@x", "d1", 5, active=0)])
    check("inactive.duplicate_coexists",
          out["receipts"][0]["action"] == "insert", out["receipts"])
    # Another ACTIVE row with the same email: target external_id is fresh
    # (target miss -> plain INSERT), the partial unique index then rejects it.
    expect_error("inactive.active_duplicate_rejected",
                 lambda: apply(db, target, [row("a@x", "d2", 6, active=1)]),
                 "ExecutionError")
    check("inactive.rollback_leaves_table",
          len([r for r in inspect(db) if r["external_id"] == "d2"]) == 0,
          inspect(db))
    # Same row routed through the active_email target: target hit + the
    # non-target external_id (d2) being fresh means nothing correctly skips.
    partial = bind(describe(db), {"target": ACTIVE_EMAIL_TARGET,
                                  "action": {"kind": "nothing"}})
    out = apply(db, partial, [row("a@x", "d2", 6, active=1)])
    check("inactive.partial_target_skips_own_conflict",
          out["receipts"][0]["action"] == "skipped", out["receipts"])


def scenario_null_and_empty(db):
    """NULL emails never collide; empty strings do (and participate in key)."""
    plan = bind(describe(db), {"target": ACTIVE_EMAIL_TARGET,
                               "action": UPDATE_N})
    out = apply(db, plan, [
        row(None, "null1", 10), row(None, "null2", 11)])
    check("null.two_inserts",
          [r["action"] for r in out["receipts"]] == ["insert", "insert"],
          out["receipts"])
    # Empty string: first insert, second collides on active_email target.
    out = apply(db, plan, [row("", "e1", 12), row("", "e2", 13)])
    check("empty.collides_on_active_email",
          [r["action"] for r in out["receipts"]] == ["insert", "update"],
          [r["action"] for r in out["receipts"]])
    check("empty.update_kept_first_external_id",
          out["receipts"][1]["value"]["external_id"] == "e1",
          out["receipts"][1]["value"])


def scenario_two_distinct_targets(db):
    """One input hits two different rows on two unique indexes: the real
    constraint must reject it; no row may be picked for update."""
    seed = bind(describe(db), {"target": EXTERNAL_TARGET, "action": UPDATE_N})
    apply(db, seed, [row("c@x", "x3", 30)])          # id for active_email
    apply(db, seed, [row("z@x", "x4", 31)])          # id for external_id
    before = inspect(db)
    plan = bind(describe(db), {"target": ACTIVE_EMAIL_TARGET,
                               "action": UPDATE_N})
    # email c@x -> active_email row; external x4 -> a different row.
    expect_error("two_distinct.rejected",
                 lambda: apply(db, plan, [row("c@x", "x4", 99)]),
                 "ExecutionError")
    after = inspect(db)
    check("two_distinct.fully_rolled_back", after == before,
          {"before": before, "after": after})
    untouched = next(r for r in after if r["external_id"] == "x3")
    check("two_distinct.no_row_updated", untouched["n"] == 30, untouched)


def scenario_nothing_action(db):
    """nothing skips only the target conflict; other unique errors still fire."""
    plan = bind(describe(db), {"target": ACTIVE_EMAIL_TARGET,
                               "action": {"kind": "nothing"}})
    out = apply(db, plan, [row("A@X", "other", 77)])
    check("nothing.target_skip",
          out["receipts"][0]["action"] == "skipped", out["receipts"])
    check("nothing.value_is_unchanged_row",
          out["receipts"][0]["value"]["n"] == 2, out["receipts"][0]["value"])
    # Distinct other-unique collision is not swallowed by nothing.
    expect_error("nothing.other_unique_still_fails",
                 lambda: apply(db, plan, [row("new@x", "x1", 78)]),
                 "ExecutionError")


def scenario_where_three_valued(db):
    """Optional WHERE: TRUE updates, FALSE and NULL both skip."""
    request = {
        "target": EXTERNAL_TARGET,
        "action": {
            "kind": "update",
            "set": {"n": {"excluded": "n"}},
            "where": {"op": "lt", "args": [{"old": "n"}, {"literal": 100}]},
        },
    }
    plan = bind(describe(db), request)
    out = apply(db, plan, [
        {"tenant": "t", "email": None, "active": 1, "external_id": "x1", "n": 100}])
    check("where.true_updates", out["receipts"][0]["action"] == "update",
          out["receipts"])
    # old n is now 100; 100 < 100 is FALSE -> skip despite the new value.
    out = apply(db, plan, [
        {"tenant": "t", "email": None, "active": 1, "external_id": "x1", "n": 101}])
    check("where.false_skips", out["receipts"][0]["action"] == "skipped",
          out["receipts"])
    check("where.skipped_value_preserved",
          out["receipts"][0]["value"]["n"] == 100, out["receipts"][0]["value"])
    # NULL WHERE via comparison against a NULL old email.
    null_request = {
        "target": EXTERNAL_TARGET,
        "action": {"kind": "update", "set": {"n": {"excluded": "n"}},
                   "where": {"op": "eq",
                             "args": [{"old": "email"}, {"literal": None}]}},
    }
    null_plan = bind(describe(db), null_request)
    out = apply(db, null_plan, [
        {"tenant": "t", "email": "m@x", "active": 1,
         "external_id": "null1", "n": 200}])
    check("where.null_skips", out["receipts"][0]["action"] == "skipped",
          out["receipts"])
    check("where.null_row_unchanged",
          next(r for r in inspect(db) if r["external_id"] == "null1")["n"] == 10,
          inspect(db))


def scenario_consecutive_hits(db):
    """Later source rows see earlier results; receipts keep each snapshot."""
    request = {
        "target": EXTERNAL_TARGET,
        "action": {"kind": "update", "set": {
            "n": {"op": "add", "args": [{"old": "n"}, {"excluded": "n"}]}}},
    }
    plan = bind(describe(db), request)
    out = apply(db, plan, [
        {"tenant": "t", "email": None, "active": 1, "external_id": "x1", "n": 1},
        {"tenant": "t", "email": None, "active": 1, "external_id": "x1", "n": 10},
        {"tenant": "t", "email": None, "active": 1, "external_id": "x1", "n": 100},
    ])
    ns = [r["value"]["n"] for r in out["receipts"]]
    check("consecutive.accumulating_snapshots", ns == [101, 111, 211], ns)
    final = next(r for r in inspect(db) if r["external_id"] == "x1")["n"]
    check("consecutive.batch_end_value", final == 211, final)
    # Snapshot history must not be rewritten by the later updates.
    check("consecutive.receipts_immutable",
          [r["value"]["n"] for r in out["receipts"]] == [101, 111, 211],
          out["receipts"])
    save_artifact("consecutive_receipts", out)


def scenario_last_row_failure(db):
    """Failure on the last row rolls back the entire batch atomically."""
    before = inspect(db)
    # The target is external_id. The first two rows insert cleanly. The last
    # row has a fresh external_id (target miss -> plain INSERT) but reuses an
    # active email already in the table, so the other unique index rejects it.
    existing_email = next(r["email"] for r in inspect(db)
                          if r["email"] and r["active"] == 1)
    plan = bind(describe(db), {"target": EXTERNAL_TARGET, "action": UPDATE_N})
    rows = [
        {"tenant": "t", "email": "last1", "active": 1,
         "external_id": "last1", "n": 1},
        {"tenant": "t", "email": "last2", "active": 1,
         "external_id": "last2", "n": 2},
        {"tenant": "t", "email": existing_email, "active": 1,
         "external_id": "last3", "n": 3},
    ]
    expect_error("last_row.failure_raised",
                 lambda: apply(db, plan, rows), "ExecutionError")
    check("last_row.whole_batch_rolled_back", inspect(db) == before,
          {"before_len": len(before), "after_len": len(inspect(db))})


def scenario_bulk_500(db):
    """Up to the 500-row limit, source order and all receipts are retained."""
    plan = bind(describe(db), {"target": EXTERNAL_TARGET,
                               "action": {"kind": "nothing"}})
    rows = [
        {"tenant": "t", "email": "bulk%d@x" % i, "active": 1,
         "external_id": "bulk%03d" % i, "n": i}
        for i in range(500)
    ]
    out = apply(db, plan, rows)
    check("bulk.five_hundred_receipts", len(out["receipts"]) == 500,
          len(out["receipts"]))
    check("bulk.source_no_order",
          [r["source_no"] for r in out["receipts"]] == list(range(500)),
          "source_no sequence")
    check("bulk.final_rows_id_sorted",
          [r["id"] for r in out["final_rows"]]
          == sorted(r["id"] for r in out["final_rows"]),
          "final_rows ordering")
    over = copy.deepcopy(rows)
    over.append(rows[0])
    expect_error("bulk.over_limit_rejected",
                 lambda: apply(db, plan, over), "PlanError")
    save_artifact("bulk_receipts_sample",
                  {"receipts": out["receipts"][:5],
                   "final_count": len(out["final_rows"])})


def scenario_external_id_target(db):
    """external_id target: external collision updates, email collision is the
    'other' unique and must still be enforced."""
    plan = bind(describe(db), {"target": EXTERNAL_TARGET, "action": UPDATE_N})
    # Same external id, different email -> target hit, update only n.
    out = apply(db, plan, [
        {"tenant": "t", "email": "renamed@x", "active": 1,
         "external_id": "x1", "n": 300}])
    check("external.target_update", out["receipts"][0]["action"] == "update",
          out["receipts"])
    check("external.email_untouched_by_n_only",
          out["receipts"][0]["value"]["email"] == "a@x",
          out["receipts"][0]["value"])
    check("external.n_updated",
          out["receipts"][0]["value"]["n"] == 300,
          out["receipts"][0]["value"])
    # Fresh external id but an active email already present -> other unique.
    expect_error("external.other_unique_enforced",
                 lambda: apply(db, plan, [row("a@x", "fresh", 1)]),
                 "ExecutionError")


def scenario_expressions_and_overflow(db):
    """coalesce, old references incl. id, and int64 overflow batch failure."""
    coalesce_request = {
        "target": EXTERNAL_TARGET,
        "action": {"kind": "update", "set": {
            "n": {"op": "coalesce",
                  "args": [{"literal": None},
                           {"op": "add", "args": [{"old": "n"},
                                                  {"excluded": "n"}]}]}}},
    }
    plan = bind(describe(db), coalesce_request)
    out = apply(db, plan, [
        {"tenant": "t", "email": None, "active": 1,
         "external_id": "x1", "n": 5}])
    check("coalesce.null_then_add",
          out["receipts"][0]["value"]["n"] == 305, out["receipts"][0]["value"])
    # old.id is legal; excluded.id is rejected offline.
    bind(describe(db), {"target": EXTERNAL_TARGET, "action": {"kind": "update",
        "set": {"external_id": {"op": "coalesce",
                "args": [{"literal": None}, {"old": "external_id"}]}}}})
    expect_error("bind.excluded_id_rejected",
                 lambda: bind(describe(db), {"target": EXTERNAL_TARGET,
                     "action": {"kind": "update",
                                "set": {"n": {"excluded": "id"}}}}),
                 "BindingError")
    # Overflow: old n near max plus an excluded delta must fail the batch.
    big = 2 ** 63 - 1
    seed_plan = bind(describe(db), {"target": EXTERNAL_TARGET, "action": UPDATE_N})
    apply(db, seed_plan, [
        {"tenant": "t", "email": "ov", "active": 1,
         "external_id": "ov", "n": big}])
    before = inspect(db)
    expect_error("overflow.batch_fails",
                 lambda: apply(db, plan, [
                     {"tenant": "t", "email": None, "active": 1,
                      "external_id": "ov", "n": 1}]),
                 "ExecutionError")
    check("overflow.rolled_back", inspect(db) == before, "no partial change")


def scenario_plan_guardrails(db, catalog):
    """Unbound requests and stale/corrupt plans are never executable."""
    raw_request = {"target": EXTERNAL_TARGET, "action": UPDATE_N}
    expect_error("guard.raw_request_rejected",
                 lambda: apply(db, raw_request, []), "PlanError")
    plan = bind(catalog, {"target": EXTERNAL_TARGET, "action": UPDATE_N})
    corrupt = json.loads(json.dumps(plan))
    corrupt["format"] = 9
    expect_error("guard.unknown_format",
                 lambda: apply(db, corrupt, []), "PlanError")
    stale = json.loads(json.dumps(plan))
    stale["catalog_sha256"] = "0" * 64
    expect_error("guard.stale_catalog",
                 lambda: apply(db, stale, []), "PlanError")
    tampered = json.loads(json.dumps(plan))
    tampered["target_index"] = "active_email"
    expect_error("guard.tampered_target",
                 lambda: apply(db, tampered, []), "PlanError")
    # Source row guards: id forbidden, bool rejected as int.
    expect_error("guard.source_id_forbidden",
                 lambda: apply(db, plan, [
                     {"id": 1, "tenant": "t", "email": "g", "active": 1,
                      "external_id": "g", "n": 1}]),
                 "ExecutionError")
    expect_error("guard.bool_not_int",
                 lambda: apply(db, plan, [
                     {"tenant": "t", "email": "g2", "active": True,
                      "external_id": "g2", "n": 1}]),
                 "ExecutionError")


def main():
    with tempfile.TemporaryDirectory() as td:
        db = create(os.path.join(td, "contacts.sqlite"))
        catalog = describe(db)
        save_artifact("catalog", catalog)
        try:
            scenario_baseline(db)
            scenario_offline_missing_predicate(catalog)
            scenario_non_target_conflict(db)
            scenario_inactive_rows(db)
            scenario_null_and_empty(db)
            scenario_two_distinct_targets(db)
            scenario_nothing_action(db)
            scenario_where_three_valued(db)
            scenario_consecutive_hits(db)
            scenario_last_row_failure(db)
            scenario_bulk_500(db)
            scenario_external_id_target(db)
            scenario_expressions_and_overflow(db)
            scenario_plan_guardrails(db, catalog)
            save_artifact("final_table", inspect(db))
        finally:
            db.close()

    summary = {
        "passed": sum(1 for r in RESULTS if r["ok"]),
        "failed": sum(1 for r in RESULTS if not r["ok"]),
        "results": RESULTS,
    }
    print(json.dumps(summary, sort_keys=True, indent=1))
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
