"""Unified, redacted audit recording and reporting."""
from __future__ import annotations

import sqlite3
from typing import Any

from scopelock.core import db
from scopelock.core.redact import redact

_POLICY_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_event (
  id               TEXT PRIMARY KEY,
  case_id          TEXT NOT NULL,
  verb             TEXT NOT NULL,
  disposition      TEXT NOT NULL,
  source           TEXT NOT NULL,
  trigger_redacted TEXT,
  result            TEXT NOT NULL,
  created_at        TEXT NOT NULL
)
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _event(
    *,
    event_id: str,
    case_id: str,
    verb: str,
    disposition: str,
    source: str,
    trigger_redacted: str | None,
    result: str,
    created_at: str,
) -> dict:
    return _redact_value(
        {
            "id": event_id,
            "case_id": case_id,
            "verb": verb,
            "disposition": disposition,
            "source": source,
            "trigger_redacted": trigger_redacted,
            "result": result,
            "created_at": created_at,
        }
    )


def insert_event(
    conn: sqlite3.Connection,
    *,
    case_id: str,
    verb: str,
    disposition: str,
    source: str,
    trigger_redacted: str | None,
    result: str,
) -> str:
    """Insert one redacted policy event inside the caller's open transaction (no commit)."""
    event_id = db.new_id("evt")
    conn.execute(
        "INSERT INTO policy_event "
        "(id, case_id, verb, disposition, source, trigger_redacted, result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            case_id,
            verb,
            disposition,
            source,
            redact(trigger_redacted) if trigger_redacted is not None else None,
            redact(result),
            db.now(),
        ),
    )
    return event_id


def record_event(
    case_id: str,
    verb: str,
    disposition: str,
    source: str,
    trigger_redacted: str | None,
    result: str,
) -> str:
    """Insert one policy event and return its generated ID.

    The ``CREATE TABLE IF NOT EXISTS`` is an exact compatibility bootstrap for this
    parallel branch. Once Person A's schema is present it is a no-op.
    """
    conn = db.connect()
    conn.execute(_POLICY_EVENT_SCHEMA)
    event_id = db.new_id("evt")
    conn.execute(
        "INSERT INTO policy_event "
        "(id, case_id, verb, disposition, source, trigger_redacted, result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            case_id,
            verb,
            disposition,
            source,
            redact(trigger_redacted) if trigger_redacted is not None else None,
            redact(result),
            db.now(),
        ),
    )
    conn.commit()
    return event_id


def get_audit_report(case_id: str) -> list[dict]:
    """Return a chronological redacted timeline spanning the complete case lifecycle."""
    conn = db.connect()
    case_row = conn.execute("SELECT * FROM kase WHERE id = ?", (case_id,)).fetchone()
    if case_row is None:
        return []

    case = dict(case_row)
    events: list[dict] = []

    if _table_exists(conn, "policy_event"):
        rows = conn.execute(
            "SELECT * FROM policy_event WHERE case_id = ? ORDER BY created_at, id", (case_id,)
        ).fetchall()
        events.extend(_redact_value(dict(row)) for row in rows)

    recorded_verbs = {str(event.get("verb", "")) for event in events}
    if "scope_created" not in recorded_verbs:
        events.append(
            _event(
                event_id=f"scope_{case_id}",
                case_id=case_id,
                verb="scope_created",
                disposition="recorded",
                source="guava",
                trigger_redacted=case.get("issue_summary"),
                result=f"Scope created for {case.get('institution', 'institution')}",
                created_at=case["created_at"],
            )
        )

    mandate_rows = conn.execute(
        "SELECT * FROM mandate_rule WHERE case_id = ? ORDER BY verb", (case_id,)
    ).fetchall()
    confirmed_row = next(
        (dict(row) for row in mandate_rows if dict(row).get("confirmed_by_holder")), None
    )
    if confirmed_row is not None and "mandate_confirmation" not in recorded_verbs:
        events.append(
            _event(
                event_id=f"confirmation_{case_id}",
                case_id=case_id,
                verb="mandate_confirmation",
                disposition="confirmed",
                source="guava",
                trigger_redacted=confirmed_row.get("confirmed_utterance"),
                result="Mandate confirmed by holder",
                # The frozen mandate schema has no confirmation timestamp.
                created_at=case["created_at"],
            )
        )

    decision_rows: list[sqlite3.Row] = []
    if _table_exists(conn, "decision_request"):
        decision_rows = conn.execute(
            "SELECT * FROM decision_request WHERE case_id = ? ORDER BY created_at, id",
            (case_id,),
        ).fetchall()

    if decision_rows:
        for row in decision_rows:
            decision = dict(row)
            answer = decision.get("answer_es") or decision.get("answer_en")
            result = f"Holder decision {decision.get('status', 'pending')}"
            if answer:
                result += f": {answer}"
            events.append(
                _event(
                    event_id=decision["id"],
                    case_id=case_id,
                    verb=decision["verb"],
                    disposition="requires_holder",
                    source="guava",
                    trigger_redacted=(
                        f"{decision.get('question_en', '')} / {decision.get('question_es', '')}"
                    ),
                    result=result,
                    created_at=decision["created_at"],
                )
            )
    elif _table_exists(conn, "consult"):
        rows = conn.execute(
            "SELECT * FROM consult WHERE case_id = ? ORDER BY created_at, id", (case_id,)
        ).fetchall()
        for row in rows:
            consult = dict(row)
            events.append(
                _event(
                    event_id=consult["id"],
                    case_id=case_id,
                    verb="holder_consultation",
                    disposition="requires_holder",
                    source="guava",
                    trigger_redacted=(
                        f"{consult.get('question_en', '')} / {consult.get('question_es', '')}"
                    ),
                    result=(
                        f"Holder answered: {consult.get('answer_es', '')} "
                        f"/ {consult.get('answer_en', '')}"
                    ),
                    created_at=consult["created_at"],
                )
            )

    if _table_exists(conn, "violation"):
        rows = conn.execute(
            "SELECT * FROM violation WHERE case_id = ? ORDER BY created_at, id", (case_id,)
        ).fetchall()
        for row in rows:
            violation = dict(row)
            events.append(
                _event(
                    event_id=violation["id"],
                    case_id=case_id,
                    verb=violation["verb"],
                    disposition="forbidden",
                    source="guava",
                    trigger_redacted=violation.get("trigger"),
                    result="Action refused",
                    created_at=violation["created_at"],
                )
            )

    if case.get("state") == "closed" and "closeout" not in recorded_verbs:
        card_row = conn.execute("SELECT created_at FROM card WHERE case_id = ?", (case_id,)).fetchone()
        events.append(
            _event(
                event_id=f"closeout_{case_id}",
                case_id=case_id,
                verb="closeout",
                disposition="completed",
                source="guava",
                trigger_redacted=None,
                result="Case closed and Callback Card finalized",
                created_at=card_row["created_at"] if card_row else case["created_at"],
            )
        )

    return sorted(events, key=lambda event: (event["created_at"], event["id"]))


_EVENT_BUCKETS = (
    "allowed",
    "holder_decision_required",
    "blocked",
    "effect_committed",
    "effect_duplicate",
    "effect_refused",
    "mandate_revoked",
)


def authz_metrics() -> dict:
    """Process-independent counters read from SQLite. Every key is from a fixed set, so
    cardinality is bounded and no case ID, utterance or reason text can appear."""
    conn = db.connect()
    events = {bucket: 0 for bucket in _EVENT_BUCKETS}
    for row in conn.execute("SELECT verb, result, COUNT(*) AS n FROM policy_event GROUP BY verb, result"):
        if row["verb"] == "mandate_revoked":
            events["mandate_revoked"] += row["n"]
            continue
        bucket = str(row["result"]).split(":", 1)[0]
        if bucket in events:
            events[bucket] += row["n"]
    decisions = {state: 0 for state in ("pending", "resolved", "cancelled", "abandoned")}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM decision_request GROUP BY status"):
        if row["status"] in decisions:
            decisions[row["status"]] = row["n"]
    versions = {"active": 0, "revoked": 0, "expired": 0}
    now = db.epoch()
    for row in conn.execute("SELECT expires_at, revoked_at FROM mandate_version"):
        if row["revoked_at"] is not None:
            versions["revoked"] += 1
        elif row["expires_at"] is not None and now >= row["expires_at"]:
            versions["expired"] += 1
        else:
            versions["active"] += 1
    effects = conn.execute("SELECT COUNT(*) FROM action_effect").fetchone()[0]
    return {
        "policy_events": events,
        "decision_requests": decisions,
        "mandate_versions": versions,
        "effects": effects,
    }
