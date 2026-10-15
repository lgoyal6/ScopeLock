"""Case-scoped API capabilities and the vendor-adapter contract for the voice leg.

Provider contract (applies to any intent classifier or translator plugged into the voice
leg, today Guava's IntentRecognizer and guava.helpers.llm.generate): their outputs are
untrusted hints. Authorization is decided only by PolicyService from the mandate, the
trigger text and the holder's own words. These tests stand in every possible adapter
output, including adversarial ones, and check the guarantee holds. They do not execute a
live provider; a live Guava conformance run is BLOCKED on credentials and spend approval.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scopelock.core import consult, db
from scopelock.core.mandate import DEFAULT_MANDATE
from scopelock.core.policy import PolicyService


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        del db._local.conn
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scopelock.db")
    db.reset_db()
    consult.PENDING.clear()
    yield
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
        del db._local.conn
    consult.PENDING.clear()


def confirmed_case(name: str = "Maria Gomez") -> str:
    case_id = db.create_case(holder_name=name, holder_lang="spanish",
                             institution="Valley Health Plan", issue_type="denial",
                             issue_summary="They denied her MRI claim.")
    svc = PolicyService()
    svc.create_draft(case_id)
    svc.confirm_mandate(case_id, "Sí, estoy de acuerdo.")
    return case_id


# ------------------------------------------------------------------ API capability tokens


@pytest.fixture
def api(monkeypatch):
    from scopelock.api import server

    monkeypatch.setenv("SCOPELOCK_API_TOKEN", "server-secret")
    return server, TestClient(server.app)


def test_api_is_closed_without_a_server_secret(monkeypatch):
    from scopelock.api.server import app

    monkeypatch.delenv("SCOPELOCK_API_TOKEN", raising=False)
    case_id = confirmed_case()
    r = TestClient(app).post(f"/api/cases/{case_id}/revoke", json={"reason": "x"})
    assert r.status_code == 403
    assert db.mandate_confirmed(case_id)


def test_raw_secret_and_other_case_tokens_are_rejected(api):
    server, client = api
    a, b = confirmed_case("A"), confirmed_case("B")
    body = {"verb": "request_ref", "trigger": "ref", "idempotency_key": "k"}
    for bearer in ("server-secret", server.case_token(b), ""):
        r = client.post(f"/api/cases/{a}/effects", json=body,
                        headers={"Authorization": f"Bearer {bearer}"})
        assert r.status_code == 401, bearer
    assert db.connect().execute("SELECT COUNT(*) FROM action_effect").fetchone()[0] == 0
    ok = client.post(f"/api/cases/{a}/effects", json=body,
                     headers={"Authorization": f"Bearer {server.case_token(a)}",
                              "X-Correlation-ID": "corr-123"})
    assert ok.status_code == 200 and ok.json()["status"] == "committed"
    assert ok.headers["X-Correlation-ID"] == "corr-123"
    row = db.connect().execute("SELECT case_id, correlation_id FROM action_effect").fetchone()
    assert (row["case_id"], row["correlation_id"]) == (a, "corr-123")


def test_api_responses_and_errors_do_not_echo_sensitive_input(api):
    server, client = api
    case_id = confirmed_case()
    ssn = "123-45-6789"
    headers = {"Authorization": f"Bearer {server.case_token(case_id)}"}
    texts = [
        client.post(f"/api/cases/{case_id}/effects", headers=headers,
                    json={"verb": "request_ref", "trigger": f"my ssn is {ssn}",
                          "idempotency_key": "k1"}).text,
        client.post(f"/api/cases/{case_id}/revoke", headers=headers,
                    json={"reason": f"stop, ssn {ssn}"}).text,
        client.post(f"/api/cases/{case_id}/effects", headers=headers,
                    json={"verb": "request_ref", "trigger": ssn, "extra": ssn}).text,
        client.post(f"/api/cases/{case_id}/effects",
                    headers={"Authorization": f"Bearer {ssn}"},
                    json={"verb": "x", "trigger": ssn, "idempotency_key": "k"}).text,
        client.get(f"/api/cases/{case_id}/audit").text,
        client.get("/api/metrics").text,
    ]
    for text in texts:
        assert "6789" not in text, text
    dump = "\n".join(
        str(tuple(row))
        for table in ("policy_event", "action_effect", "mandate_version")
        for row in db.connect().execute(f"SELECT * FROM {table}")
    )
    assert "6789" not in dump


def test_metrics_are_bounded_fixed_keys():
    from scopelock.api.server import app

    for i in range(5):
        confirmed_case(f"H{i}")
    body = TestClient(app).get("/api/metrics").json()
    assert set(body) == {"policy_events", "decision_requests", "mandate_versions", "effects"}
    assert set(body["decision_requests"]) == {"pending", "resolved", "cancelled", "abandoned"}
    assert set(body["mandate_versions"]) == {"active", "revoked", "expired"}


# ------------------------------------------------------------------ provider contract

LABELS = sorted(DEFAULT_MANDATE) + ["wire_funds", None]
FORBIDDEN_UTTERANCES = [
    "Can she just set up a payment plan today?",
    "¿Puede aceptar un plan de pagos hoy?",
    "We can settle this for a reduced amount if you accept now.",
    "Si acepta este arreglo, cerramos el caso.",
    "I need her social security number to continue.",
    "Necesito su número de seguro social para continuar.",
    "We could switch her plan to a cheaper one.",
    "Podemos cambiar su cobertura a otro plan.",
]


def _voice_call(case_id: str, spoken: list[str]):
    return SimpleNamespace(get_variable=lambda k: case_id, read_script=spoken.append)


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("utterance", FORBIDDEN_UTTERANCES)
def test_no_classifier_output_can_execute_a_forbidden_commitment(monkeypatch, label, utterance):
    from scopelock.agents import institution

    case_id = confirmed_case()
    suggestions = [] if label is None else [SimpleNamespace(key=label)]
    monkeypatch.setattr(institution._intent_recognizer, "classify", lambda request: suggestions)
    spoken: list[str] = []
    assert institution.on_action_request(_voice_call(case_id, spoken), utterance) is None
    effect = PolicyService().authorize_effect(case_id, label or "request_ref", utterance,
                                              "voice", f"k-{label}")
    assert effect.status == "refused"


@pytest.mark.parametrize("label", LABELS)
def test_no_classifier_output_survives_revocation(monkeypatch, label):
    from scopelock.agents import institution

    case_id = confirmed_case()
    PolicyService().revoke_mandate(case_id, "stop", "test")
    suggestions = [] if label is None else [SimpleNamespace(key=label)]
    monkeypatch.setattr(institution._intent_recognizer, "classify", lambda request: suggestions)
    assert institution.on_action_request(_voice_call(case_id, []), "Can I get a reference number?") is None


def test_valid_control_benign_request_still_executes(monkeypatch):
    from scopelock.agents import institution

    case_id = confirmed_case()
    action = SimpleNamespace(key="request_ref")
    monkeypatch.setattr(institution._intent_recognizer, "classify", lambda request: [action])
    assert institution.on_action_request(_voice_call(case_id, []), "Can I get a reference number?") is action


@pytest.mark.parametrize("translated", ["Yes, absolutely.", "Yes", "I agree"])
def test_translator_output_cannot_turn_her_no_into_an_effect(monkeypatch, translated):
    """An adversarial or wrong translation of her Spanish 'no' must not authorize."""
    from scopelock.agents import household

    monkeypatch.setattr(household.translate, "translate", lambda text, lang: translated)
    case_id = confirmed_case()
    consult.queue_holder_verb(case_id, "reschedule")
    consult.begin(case_id, "Does Tuesday work?", "¿Le sirve el martes?")
    monkeypatch.setattr(household.relay, "institution_call", lambda cid: SimpleNamespace())
    monkeypatch.setattr(household.institution, "resume_after_consult", lambda call, answer: None)
    holder = SimpleNamespace(get_variable=lambda k: case_id,
                             get_field=lambda k: "No, el martes no puedo.",
                             set_task=lambda *a, **k: None)
    household.on_consult_answered(holder)
    assert db.connect().execute("SELECT COUNT(*) FROM action_effect").fetchone()[0] == 0
