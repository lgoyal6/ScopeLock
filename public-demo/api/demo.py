"""Bounded synthetic endpoint for the ScopeLock policy demo."""
from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from scopelock.core import db, consult
from scopelock.core.policy import PolicyService


CASE_ID = "synthetic-scope-lock-demo"
_seed_lock = threading.Lock()
SCENARIOS = {
    "reference": {
        "label": "Request a call reference number",
        "verb": "request_ref",
        "trigger": "A representative asks whether to provide a call reference number.",
    },
    "appointment": {
        "label": "Reschedule an appointment",
        "verb": "reschedule",
        "trigger": "A representative proposes a new appointment time.",
    },
    "payment": {
        "label": "Agree to a payment plan",
        "verb": "agree_payment",
        "trigger": "A representative proposes a payment plan.",
    },
    "identifier": {
        "label": "Share a Social Security number",
        "verb": "disclose_ssn",
        "trigger": "A representative asks for a Social Security number.",
    },
}

# Serverless filesystems are ephemeral. Keep this demo's synthetic audit rows out of
# the source tree and away from the application's normal database.



def _evaluate_scenario(scenario_id: str, answer: str = "pending") -> dict[str, object]:
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise ValueError("Unknown demo scenario")

    conn = db.connect()
    conn.execute(
        "INSERT OR IGNORE INTO kase "
        "(id, holder_name, holder_lang, holder_phone, institution, issue_type, "
        "issue_summary, session_a, session_b, state, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, NULL, 'intake', ?)",
        (
            CASE_ID,
            "Fictional demo case",
            "en",
            "Synthetic service",
            "demo",
            "Synthetic authorization example",
            db.now(),
        ),
    )
    conn.commit()
    if not db.mandate_rules(CASE_ID):
        PolicyService().create_draft(CASE_ID)

    policy = PolicyService()
    policy.confirm_mandate(CASE_ID, "Sí, estoy de acuerdo.")
    decision = policy.evaluate_action(
        CASE_ID,
        scenario["verb"],
        scenario["trigger"],
        source="public_synthetic_demo",
    )
    decision_id = None
    question_en = "May I move the appointment to Tuesday at 10 AM?"
    question_es = "¿Puedo cambiar la cita al martes a las diez?"
    if decision.requires_holder:
        decision_id = consult.request_holder_decision(CASE_ID, scenario["verb"], question_en, question_es)
        if answer in {"approve", "decline"}:
            consult.resolve_holder_decision(decision_id, "Sí, de acuerdo." if answer == "approve" else "No, no cambie la cita.", "Yes, I agree." if answer == "approve" else "No, do not change it.", latency_ms=0)
    if answer == "revoke":
        policy.revoke_mandate(CASE_ID, "Fictional holder withdraws authorization", "public_synthetic_demo")
    effect = policy.authorize_effect(CASE_ID, scenario["verb"], scenario["trigger"], "public_synthetic_demo", "demo-action", decision_id)
    duplicate = policy.authorize_effect(CASE_ID, scenario["verb"], scenario["trigger"], "public_synthetic_demo", "demo-action", decision_id)
    count = conn.execute("SELECT COUNT(*) FROM action_effect WHERE case_id = ?", (CASE_ID,)).fetchone()[0]
    return {
        "scenario": scenario_id, "label": scenario["label"], "decision": decision.decision,
        "may_execute": decision.may_execute, "requires_holder": decision.requires_holder,
        "refusal": decision.refusal, "effect_written": count > 0,
        "effect_status": effect.status, "reason": effect.reason,
        "repeat_status": duplicate.status, "effect_count": count,
        "question_en": question_en if decision.requires_holder else None,
        "question_es": question_es if decision.requires_holder else None,
        "holder_answer": answer, "mandate_version": 1,
        "evidence_class": "synthetic_local_authorization_workflow",
    }



def evaluate_scenario(scenario_id: str, answer: str = "pending") -> dict[str, object]:
    if answer not in {"pending", "approve", "decline", "revoke"}:
        raise ValueError("Unknown holder response")
    # The owning DB module uses a global path and thread-local connection. Keep
    # each synthetic request isolated, close its connection and remove its rows.
    with _seed_lock, tempfile.TemporaryDirectory(prefix="scopelock-demo-") as directory:
        original_path = db.DB_PATH
        previous_connection = getattr(db._local, "conn", None)
        db._local.conn = None
        db.DB_PATH = Path(directory) / "policy.sqlite3"
        try:
            return _evaluate_scenario(scenario_id, answer)
        finally:
            connection = getattr(db._local, "conn", None)
            if connection is not None:
                connection.close()
            db._local.conn = previous_connection
            db.DB_PATH = original_path


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._reply(405, {"error": "Use POST."})

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length < 1 or content_length > 1024:
            self._reply(400, {"error": "Choose one of the listed synthetic scenarios."})
            return
        if self.headers.get_content_type() != "application/json":
            self._reply(415, {"error": "Send a JSON scenario selection."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, {"error": "Choose one of the listed synthetic scenarios."})
            return
        if not isinstance(payload, dict) or set(payload) not in ({"scenario"}, {"scenario", "answer"}):
            self._reply(400, {"error": "Choose one of the listed synthetic scenarios."})
            return
        try:
            result = evaluate_scenario(payload["scenario"], payload.get("answer", "pending"))
        except (TypeError, ValueError):
            self._reply(400, {"error": "Choose one of the listed synthetic scenarios."})
            return
        self._reply(200, result)

    def log_message(self, format: str, *args: object) -> None:
        return
