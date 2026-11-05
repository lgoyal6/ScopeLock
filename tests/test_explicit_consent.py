"""Consent regressions are bounded text cases, not a language benchmark."""
import pytest

from scopelock.core import db, consult
from scopelock.core.policy import PolicyService, _is_clear_affirmative


AMBIGUOUS = [
    "Did I say yes?", "What does yes mean?", "¿Sí?",
    "Si me llaman mañana, hablamos.", "Quiero saber si puedo cambiar la cita.",
    "Si estoy de acuerdo, ¿qué sucede?", "Yes, if you explain the fees first.",
    "Sí, pero primero necesito pensarlo.", "I said yes yesterday.",
    "Yes, only after I speak to my doctor.", "Sí, si puedo ir.",
]


@pytest.mark.parametrize("utterance", AMBIGUOUS)
def test_questions_conditionals_and_reported_speech_are_not_consent(utterance):
    assert not _is_clear_affirmative(utterance)


@pytest.mark.parametrize("utterance", [
    "Sí.", "Si", "Sí, claro", "Si, entiendo y estoy de acuerdo con eso.",
    "Yes, I agree.", "Yes, of course", "Sí, el martes", "Yes, Tuesday works",
    "Estoy de acuerdo.", "Confirmo.", "de acuerdo", "claro",
])
def test_supported_explicit_affirmatives_remain_accepted(utterance):
    assert _is_clear_affirmative(utterance)


@pytest.fixture
def case(tmp_path, monkeypatch):
    old = getattr(db._local, "conn", None)
    if old is not None:
        old.close()
        del db._local.conn
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "consent.sqlite")
    db.reset_db()
    key = db.create_case(holder_name="Fixture", holder_lang="spanish", institution="Fixture",
                         issue_type="reason", issue_summary="Synthetic consent regression")
    PolicyService().create_draft(key)
    yield key
    db.connect().close()
    del db._local.conn


def test_conditional_cannot_confirm_mandate(case):
    PolicyService().confirm_mandate(case, "Si me llaman mañana, hablamos.")
    assert not db.mandate_confirmed(case)


def test_favorable_translation_cannot_authorize_conditional_holder_answer(case):
    service = PolicyService()
    service.confirm_mandate(case, "Sí, claro")
    decision = consult.request_holder_decision(case, "reschedule", "Tuesday?", "¿Quiere el martes?")
    consult.resolve_holder_decision(decision, "Si me llaman mañana, hablamos.", "Yes", 1)
    result = service.authorize_effect(case, "reschedule", "Tuesday", "fixture", "decision", decision)
    assert result.status == "refused"
    assert result.reason == "holder_declined"
