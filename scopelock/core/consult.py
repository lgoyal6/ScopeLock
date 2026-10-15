"""Holder decision lifecycle and live-call compatibility coordination."""
from __future__ import annotations

from scopelock.core import db, relay, translate

# case_id -> pending live-call metadata. Persistence lives only in decision_request.
PENDING: dict[str, dict] = {}
_QUEUED_VERBS: dict[str, str] = {}

HOLDING_LINE_EN = "One moment, I'm going to confirm that with her."
HOLDING_LINE_ES = "Un momento, voy a preguntarle eso a ella directamente."

# Explicit lifecycle. pending is the only non-terminal state; every transition out of it is
# a single conditional UPDATE, so two callbacks racing for the same request cannot both win.
DECISION_STATES = ("pending", "resolved", "cancelled", "abandoned")


class DecisionNotPending(ValueError):
    """The request was already resolved, cancelled (revocation, expiry, restart) or abandoned."""


def request_holder_decision(case_id: str, verb: str, question_en: str,
                            question_es: str) -> str:
    if db.get_case(case_id) is None:
        raise KeyError(f"unknown case: {case_id}")

    decision_id = db.new_id("dec")
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        seq = db.next_seq(conn, case_id, "decision_requested")
        conn.execute(
            "INSERT INTO decision_request (id, case_id, verb, question_en, question_es, "
            "answer_es, answer_en, status, decided_by, latency_ms, created_at, resolved_at, "
            "cancel_reason, created_seq) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, 'pending', NULL, NULL, ?, NULL, NULL, ?)",
            (
                decision_id,
                case_id,
                verb,
                db.redact(question_en),
                db.redact(question_es),
                db.now(),
                seq,
            ),
        )
        conn.execute(
            "UPDATE kase SET state = 'consulting_holder' WHERE id = ? "
            "AND state NOT IN ('closed', 'interrupted')",
            (case_id,),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return decision_id


def cancel_holder_decision(decision_id: str, reason: str) -> bool:
    """Close a pending request without an answer. Returns False if it was not pending."""
    conn = db.connect()
    cursor = conn.execute(
        "UPDATE decision_request SET status = 'cancelled', cancel_reason = ?, resolved_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (db.redact(reason), db.now(), decision_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def recover_after_restart() -> int:
    """Cancel requests left pending by a process that died.

    The live call that asked the question is gone with the process, so nobody can answer
    it; cancelling (never resolving) keeps the holder's decision unmanufactured. Run only
    from the process that owns live calls, before it starts listening.
    """
    PENDING.clear()
    _QUEUED_VERBS.clear()
    conn = db.connect()
    cursor = conn.execute(
        "UPDATE decision_request SET status = 'cancelled', cancel_reason = 'restart', "
        "resolved_at = ? WHERE status = 'pending'",
        (db.now(),),
    )
    conn.commit()
    return cursor.rowcount


def resolve_holder_decision(decision_id: str, answer_es: str, answer_en: str,
                            latency_ms: int) -> None:
    if latency_ms < 0:
        raise ValueError("latency_ms must be non-negative")

    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT case_id, status FROM decision_request WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown decision request")
        if row["status"] == "pending" and db.mandate_version_status(
            row["case_id"], conn
        ) in ("revoked", "expired"):
            # Expiry is evaluated lazily: the first callback after it closes the request.
            conn.execute(
                "UPDATE decision_request SET status = 'cancelled', cancel_reason = ?, "
                "resolved_at = ? WHERE id = ? AND status = 'pending'",
                (db.mandate_version_status(row["case_id"], conn), db.now(), decision_id),
            )
            conn.commit()
            raise DecisionNotPending(f"decision request {decision_id} is cancelled")

        # Security invariant: the holder identity is literal SQL, never caller-controlled.
        # The status guard in WHERE makes resolution single-winner under concurrency.
        cursor = conn.execute(
            "UPDATE decision_request SET answer_es = ?, answer_en = ?, status = 'resolved', "
            "decided_by = 'holder', latency_ms = ?, resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                db.redact(answer_es),
                db.redact(answer_en),
                latency_ms,
                db.now(),
                decision_id,
            ),
        )
        if cursor.rowcount != 1:
            raise DecisionNotPending(
                f"decision request {decision_id} is already {row['status']}"
            )
        conn.execute(
            "UPDATE kase SET state = 'representing' WHERE id = ? "
            "AND state NOT IN ('closed', 'interrupted')",
            (row["case_id"],),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise


def to_plain_spanish(question_en: str) -> str:
    """Render the representative's question plainly, without institution jargon."""
    return translate.translate(
        f"In plain, simple language, not insurance jargon: {question_en}", "spanish"
    )


def queue_holder_verb(case_id: str, verb: str) -> None:
    """Carry the evaluated verb into the existing Guava action handler."""
    _QUEUED_VERBS[case_id] = verb


def is_pending(case_id: str) -> bool:
    return case_id in PENDING


def begin(case_id: str, question_en: str, question_es: str | None = None) -> dict:
    """Compatibility wrapper for the current live-call coordinator."""
    rendered_es = question_es or to_plain_spanish(question_en)
    verb = _QUEUED_VERBS.pop(case_id, "holder_decision")
    decision_id = request_holder_decision(case_id, verb, question_en, rendered_es)
    entry = {
        "decision_id": decision_id,
        "verb": verb,
        "question_en": question_en,
        "question_es": rendered_es,
        "timer": relay.LatencyTimer(),
    }
    PENDING[case_id] = entry
    relay.set_turn(case_id, "household")
    return entry


def complete(case_id: str, answer_es: str, answer_en: str | None = None) -> str:
    """Resolve the pending request with the holder's own answer and restore the rep leg."""
    entry = PENDING.pop(case_id, None)
    if entry is None:
        raise KeyError(f"case {case_id} has no pending holder decision")
    rendered_en = answer_en or translate.translate(answer_es, "english")
    resolve_holder_decision(
        entry["decision_id"],
        answer_es,
        rendered_en,
        entry["timer"].elapsed_ms(),
    )
    relay.set_turn(case_id, "institution")
    return entry["decision_id"]


def abandon(case_id: str) -> None:
    """Close a pending request without manufacturing a decision for the holder."""
    entry = PENDING.pop(case_id, None)
    _QUEUED_VERBS.pop(case_id, None)
    if entry is not None:
        conn = db.connect()
        conn.execute(
            "UPDATE decision_request SET status = 'abandoned', resolved_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (db.now(), entry["decision_id"]),
        )
        conn.commit()
        db.set_case_state(case_id, "representing")
    relay.set_turn(case_id, "institution")


def decision_count(case_id: str) -> int:
    row = db.connect().execute(
        "SELECT COUNT(*) AS count FROM decision_request "
        "WHERE case_id = ? AND status = 'resolved' AND decided_by = 'holder'",
        (case_id,),
    ).fetchone()
    return row["count"]
