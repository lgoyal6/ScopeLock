"""Apply one mandate policy across API, MCP and voice actions.
Uses db.py for atomic effects and consult.py for holder decisions.
Revocation and explicit refusals override otherwise permitted actions.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from scopelock.agents.scripts import REFUSAL, REFUSAL_WITHDRAWN
from scopelock.core.audit import insert_event, record_event
from scopelock.core import db, intent_guard
from scopelock.core.mandate import DEFAULT_MANDATE, FORBIDDEN_ACTIONS
from scopelock.core.redact import redact


_PERMISSION_RANK = {"forbidden": 0, "requires_holder": 1, "allowed": 2}
_DEFAULT_REFUSAL = (
    "I'm not authorized to do that on this call. "
    "I can get a reference number and have her call back about that."
)


# A confirmed mandate lapses after this long unless the holder confirms it again.
DEFAULT_MANDATE_TTL_SECONDS = 4 * 60 * 60

# Test seam: called inside authorize_effect's write transaction after every check passed
# and before the effect row is written. Race tests use it to attempt a concurrent
# revocation at the worst possible moment. Always None in production.
_BEFORE_EFFECT_WRITE = None


@dataclass
class PolicyDecision:
    decision: str
    may_execute: bool
    requires_holder: bool
    refusal: str | None
    audit_event_id: str


@dataclass
class EffectResult:
    """Outcome of one state-changing authorization.

    status is 'committed' (new effect row), 'duplicate' (the idempotency key or decision
    already produced an effect; effect_id names it) or 'refused' (reason says why).
    """

    status: str
    reason: str | None
    effect_id: str | None
    seq: int | None
    mandate_version: int | None
    audit_event_id: str


@dataclass
class RevocationResult:
    revoked: bool
    version: int | None
    cancelled_decisions: int
    audit_event_id: str


def _disposition(case_id: str, verb: str, trigger: str | None,
                 conn: sqlite3.Connection | None = None) -> tuple[str, str | None]:
    """The single disposition rule shared by evaluate_action and authorize_effect.

    Returns (disposition, refusal_verb). Deny wins: unknown or forbidden verbs, a revoked
    or expired mandate, and a trigger that commits to a forbidden action all refuse.
    """
    conn = conn or db.connect()
    # Re-confirmation always creates a new version, so a revoked or expired LATEST
    # version means no confirmation has replaced it yet.
    if db.mandate_version_status(case_id, conn) in ("revoked", "expired"):
        return "forbidden", "__withdrawn__"
    rule = conn.execute(
        "SELECT disposition FROM mandate_rule WHERE case_id = ? AND verb = ?",
        (case_id, verb),
    ).fetchone()
    if verb in FORBIDDEN_ACTIONS or verb not in DEFAULT_MANDATE or rule is None:
        return "forbidden", verb
    guarded = intent_guard.screen(trigger)
    if guarded is not None:
        return "forbidden", guarded
    disposition = rule["disposition"]
    return disposition, (verb if disposition == "forbidden" else None)


def _refusal_text(refusal_verb: str | None) -> str | None:
    if refusal_verb is None:
        return None
    if refusal_verb == "__withdrawn__":
        return REFUSAL_WITHDRAWN
    return REFUSAL.get(refusal_verb, _DEFAULT_REFUSAL)


def _normalized_words(text: str) -> tuple[str, set[str]]:
    ascii_text = "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
    # Preserve English negation before tokenization splits apostrophe contractions.
    ascii_text = re.sub(r"\b([a-z]+)n['’]t\b", r"\1 not", ascii_text)
    words = re.findall(r"[a-z]+", ascii_text)
    return " ".join(words), set(words)


def _is_clear_affirmative(utterance: str) -> bool:
    normalized, words = _normalized_words(utterance)
    if not normalized:
        return False

    if words & {"no", "not", "never", "nunca", "maybe", "perhaps", "quizas", "quiza"}:
        return False
    if any(phrase in normalized for phrase in ("tal vez", "no se", "not sure", "it depends")):
        return False

    affirmative_phrases = (
        "si",
        "yes",
        "estoy de acuerdo",
        "de acuerdo",
        "confirmo",
        "claro",
        "vale",
        "okay",
        "ok",
        "i agree",
    )
    return any(
        re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", normalized)
        for phrase in affirmative_phrases
    )


class PolicyService:
    def create_draft(self, case_id: str,
                     overrides: dict[str, str] | None = None) -> None:
        if db.get_case(case_id) is None:
            raise KeyError(f"unknown case: {case_id}")
        if db.mandate_confirmed(case_id):
            raise ValueError("confirmed mandates cannot be modified")

        rules = dict(DEFAULT_MANDATE)
        for verb, requested in (overrides or {}).items():
            if verb not in rules:
                raise KeyError(f"unknown policy verb: {verb}")
            if requested not in _PERMISSION_RANK:
                raise ValueError(f"invalid policy disposition: {requested}")

            # A draft may remove authority, never add more authority than the default.
            default = rules[verb]
            rules[verb] = (
                requested
                if _PERMISSION_RANK[requested] <= _PERMISSION_RANK[default]
                else default
            )

        db.create_mandate_rules(case_id, rules)
        db.set_case_state(case_id, "mandate_draft")

    def evaluate_action(self, case_id: str, verb: str, trigger: str,
                        source: str) -> PolicyDecision:
        disposition, refusal_verb = _disposition(case_id, verb, trigger)

        may_execute = disposition == "allowed"
        requires_holder = disposition == "requires_holder"
        refusal = _refusal_text(refusal_verb) if disposition == "forbidden" else None
        result = (
            "allowed"
            if may_execute
            else "holder_decision_required"
            if requires_holder
            else "blocked"
        )
        audit_event_id = record_event(
            case_id=case_id,
            verb=verb,
            disposition=disposition,
            source=source,
            trigger_redacted=trigger,
            result=result,
        )
        return PolicyDecision(
            decision=disposition,
            may_execute=may_execute,
            requires_holder=requires_holder,
            refusal=refusal,
            audit_event_id=audit_event_id,
        )

    def restrict_action(self, case_id: str, verb: str) -> None:
        if verb not in DEFAULT_MANDATE:
            raise KeyError(f"unknown policy verb: {verb}")
        if db.mandate_confirmed(case_id):
            raise ValueError("confirmed mandates cannot be modified")
        db.set_mandate_disposition(case_id, verb, "forbidden")

    def get_mandate(self, case_id: str) -> list[dict]:
        return [
            {
                "verb": row["verb"],
                "disposition": row["disposition"],
                "confirmed_by_holder": bool(row["confirmed_by_holder"]),
                "confirmed_utterance": row["confirmed_utterance"],
            }
            for row in db.mandate_rules(case_id)
        ]

    def confirm_mandate(self, case_id: str, utterance: str,
                        ttl_seconds: float | None = DEFAULT_MANDATE_TTL_SECONDS) -> None:
        """Confirm the draft aloud and persist it as a new immutable mandate version.

        Re-confirming a live version is a no-op; confirming after revocation or expiry
        starts version n+1 with its own expiry.
        """
        if not _is_clear_affirmative(utterance):
            return
        if not db.mandate_rules(case_id):
            raise KeyError(f"case {case_id} has no mandate draft")
        conn = db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = db.latest_mandate_version(case_id, conn)
            status = db.mandate_version_status(case_id, conn)
            rules = conn.execute(
                "SELECT verb, disposition, confirmed_by_holder FROM mandate_rule "
                "WHERE case_id = ? ORDER BY verb",
                (case_id,),
            ).fetchall()
            already = status == "active" and all(r["confirmed_by_holder"] for r in rules)
            if not already:
                conn.execute(
                    "UPDATE mandate_rule SET confirmed_by_holder = 1, confirmed_utterance = ? "
                    "WHERE case_id = ?",
                    (redact(utterance), case_id),
                )
                confirmed_at = db.epoch()
                seq = db.next_seq(conn, case_id, "mandate_confirmed")
                version = (latest["version"] + 1) if latest is not None else 1
                conn.execute(
                    "INSERT INTO mandate_version (id, case_id, version, rules_json, "
                    "confirmed_at, expires_at, created_seq, revoked_at, revoked_seq, "
                    "revoke_reason) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (
                        db.new_id("mv"),
                        case_id,
                        version,
                        json.dumps({r["verb"]: r["disposition"] for r in rules}, sort_keys=True),
                        confirmed_at,
                        None if ttl_seconds is None else confirmed_at + ttl_seconds,
                        seq,
                    ),
                )
                conn.execute("UPDATE kase SET state = 'mandated' WHERE id = ?", (case_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def revoke_mandate(self, case_id: str, reason: str, source: str) -> RevocationResult:
        """Withdraw the live mandate for every open session at once.

        One write transaction marks the version revoked, returns the rules to an
        unconfirmed draft, cancels every pending holder decision and interrupts the case.
        Any evaluation or effect that starts after this commits sees the revocation,
        because every surface reads the same SQLite state; an effect already inside its
        own write transaction finishes first and is ordered before the revocation.
        """
        if db.get_case(case_id) is None:
            raise KeyError("unknown case")
        conn = db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = db.latest_mandate_version(case_id, conn)
            status = db.mandate_version_status(case_id, conn)
            revoked = False
            version = latest["version"] if latest is not None else None
            seq = db.next_seq(conn, case_id, "mandate_revoked")
            if latest is not None and status in ("active", "expired"):
                conn.execute(
                    "UPDATE mandate_version SET revoked_at = ?, revoked_seq = ?, "
                    "revoke_reason = ? WHERE id = ?",
                    (db.epoch(), seq, redact(reason), latest["id"]),
                )
                revoked = True
            elif latest is None:
                # Never confirmed (or a pre-versioning file): record a revoked marker so
                # nothing proceeds until the holder confirms a fresh version aloud.
                rules = conn.execute(
                    "SELECT verb, disposition FROM mandate_rule WHERE case_id = ?",
                    (case_id,),
                ).fetchall()
                at = db.epoch()
                version = 1
                conn.execute(
                    "INSERT INTO mandate_version (id, case_id, version, rules_json, "
                    "confirmed_at, expires_at, created_seq, revoked_at, revoked_seq, "
                    "revoke_reason) VALUES (?, ?, 1, ?, ?, NULL, ?, ?, ?, ?)",
                    (
                        db.new_id("mv"),
                        case_id,
                        json.dumps({r["verb"]: r["disposition"] for r in rules}, sort_keys=True),
                        at,
                        seq,
                        at,
                        seq,
                        redact(reason),
                    ),
                )
                revoked = True
            conn.execute(
                "UPDATE mandate_rule SET confirmed_by_holder = 0 WHERE case_id = ?",
                (case_id,),
            )
            cancelled = conn.execute(
                "UPDATE decision_request SET status = 'cancelled', cancel_reason = 'revoked', "
                "resolved_at = ? WHERE case_id = ? AND status = 'pending'",
                (db.now(), case_id),
            ).rowcount
            conn.execute(
                "UPDATE kase SET state = 'interrupted' WHERE id = ? AND state != 'closed'",
                (case_id,),
            )
            event_id = insert_event(
                conn,
                case_id=case_id,
                verb="mandate_revoked",
                disposition="forbidden",
                source=source,
                trigger_redacted=reason,
                result=f"revoked version {version}" if revoked else "no live mandate",
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return RevocationResult(revoked, version, cancelled, event_id)

    def authorize_effect(self, case_id: str, verb: str, trigger: str, source: str,
                         idempotency_key: str, decision_request_id: str | None = None,
                         correlation_id: str | None = None) -> EffectResult:
        """Authorize a state-changing action and write its effect record atomically.

        The mandate check and the effect row share one BEGIN IMMEDIATE transaction, so a
        revocation can never land between "allowed" and "done". The idempotency key (and a
        holder decision id) can produce at most one effect: a repeated callback returns the
        original effect as 'duplicate' instead of acting twice.
        """
        conn = db.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = self._authorize_in_txn(
                conn, case_id, verb, trigger, source, idempotency_key,
                decision_request_id, correlation_id,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    def _authorize_in_txn(self, conn, case_id, verb, trigger, source, idempotency_key,
                          decision_request_id, correlation_id) -> EffectResult:
        def finish(status, reason, effect_id=None, seq=None, version=None):
            event_id = insert_event(
                conn,
                case_id=case_id,
                verb=verb,
                disposition="allowed" if status != "refused" else "forbidden",
                source=source,
                trigger_redacted=trigger,
                result=f"effect_{status}" + (f":{reason}" if reason else ""),
            )
            return EffectResult(status, reason, effect_id, seq, version, event_id)

        if not idempotency_key:
            return finish("refused", "missing_idempotency_key")
        if conn.execute("SELECT 1 FROM kase WHERE id = ?", (case_id,)).fetchone() is None:
            return finish("refused", "unknown_case")

        prior = conn.execute(
            "SELECT * FROM action_effect WHERE case_id = ? AND idempotency_key = ?",
            (case_id, idempotency_key),
        ).fetchone()
        if prior is not None:
            if prior["verb"] != verb or prior["decision_request_id"] != decision_request_id:
                return finish("refused", "idempotency_conflict")
            return finish("duplicate", None, prior["id"], prior["seq"], prior["mandate_version"])

        status = db.mandate_version_status(case_id, conn)
        rules = conn.execute(
            "SELECT confirmed_by_holder FROM mandate_rule WHERE case_id = ?", (case_id,)
        ).fetchall()
        confirmed = bool(rules) and all(r["confirmed_by_holder"] for r in rules)
        if status in ("revoked", "expired"):
            return finish("refused", status)
        if status != "active" or not confirmed:
            return finish("refused", "not_confirmed")
        version = db.latest_mandate_version(case_id, conn)

        disposition, _ = _disposition(case_id, verb, trigger, conn)
        if disposition == "forbidden":
            return finish("refused", "forbidden")
        if disposition == "requires_holder":
            if decision_request_id is None:
                return finish("refused", "holder_decision_required")
            decision = conn.execute(
                "SELECT * FROM decision_request WHERE id = ?", (decision_request_id,)
            ).fetchone()
            if decision is None or decision["case_id"] != case_id or decision["verb"] != verb:
                return finish("refused", "decision_mismatch")
            if decision["status"] != "resolved" or decision["decided_by"] != "holder":
                return finish("refused", "decision_not_resolved")
            if (decision["created_seq"] or 0) < version["created_seq"]:
                return finish("refused", "decision_predates_mandate")
            used = conn.execute(
                "SELECT id FROM action_effect WHERE decision_request_id = ?",
                (decision_request_id,),
            ).fetchone()
            if used is not None:
                return finish("refused", "decision_already_used")
            # Translation cannot create authority absent from the holder's answer.
            if not (_is_clear_affirmative(decision["answer_es"] or "")
                    and (not decision["answer_en"]
                         or _is_clear_affirmative(decision["answer_en"]))):
                return finish("refused", "holder_declined")
        elif decision_request_id is not None:
            return finish("refused", "decision_mismatch")

        if _BEFORE_EFFECT_WRITE is not None:
            _BEFORE_EFFECT_WRITE(case_id)

        seq = db.next_seq(conn, case_id, "effect")
        effect_id = db.new_id("eff")
        conn.execute(
            "INSERT INTO action_effect (id, case_id, verb, mandate_version, idempotency_key, "
            "decision_request_id, source, trigger_redacted, correlation_id, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                effect_id,
                case_id,
                verb,
                version["version"],
                idempotency_key,
                decision_request_id,
                source,
                redact(trigger),
                correlation_id,
                seq,
                db.now(),
            ),
        )
        return finish("committed", None, effect_id, seq, version["version"])
