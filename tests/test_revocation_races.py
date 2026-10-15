"""Failure matrix for delegated authority: revocation, expiry, duplicates and restart.

Every test names its oracle in the docstring. Concurrency tests use real threads, each
with its own SQLite connection (db.connect is thread-local), against one file; the restart
test uses real separate Python processes. No live Guava calls: translate() is stubbed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from scopelock.core import consult, db, policy
from scopelock.core.policy import PolicyService

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        del db._local.conn
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scopelock.db")
    db.reset_db()
    consult.PENDING.clear()
    yield tmp_path / "scopelock.db"
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
        del db._local.conn
    consult.PENDING.clear()


def confirmed_case(ttl_seconds: float | None = policy.DEFAULT_MANDATE_TTL_SECONDS) -> str:
    case_id = db.create_case(
        holder_name="Maria Gomez",
        holder_lang="spanish",
        institution="Valley Health Plan",
        issue_type="denial",
        issue_summary="They denied her MRI claim.",
    )
    svc = PolicyService()
    svc.create_draft(case_id)
    svc.confirm_mandate(case_id, "Sí, estoy de acuerdo.", ttl_seconds=ttl_seconds)
    return case_id


def resolved_decision(case_id: str, verb: str = "reschedule", answer: str = "Sí, claro") -> str:
    decision_id = consult.request_holder_decision(case_id, verb, "New date?", "¿Nueva fecha?")
    consult.resolve_holder_decision(decision_id, answer, "Yes, of course", 1200)
    return decision_id


def effects(case_id: str) -> list[dict]:
    return [
        dict(r)
        for r in db.connect().execute(
            "SELECT * FROM action_effect WHERE case_id = ? ORDER BY seq", (case_id,)
        )
    ]


def in_thread(fn, *args, **kwargs):
    """Run fn on a fresh thread (fresh SQLite connection); return (thread, box)."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # surfaced by the caller
            box["error"] = exc
        finally:
            conn = getattr(db._local, "conn", None)
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


# ---------------------------------------------------------------- revocation semantics


def test_revocation_blocks_every_surface_that_reads_the_shared_policy():
    """Oracle: after revoke commits, API, MCP and voice all refuse a verb they allowed."""
    from scopelock.agents import institution
    from scopelock.api.server import app, case_token
    from scopelock.mcp import server as mcp_server

    case_id = confirmed_case()
    svc = PolicyService()
    assert svc.evaluate_action(case_id, "ask_reason", "Why?", source="test").may_execute

    out = anyio.run(mcp_server.server.call_tool, "revoke_mandate",
                    {"case_id": case_id, "reason": "She said stop"})
    assert out.structured_content["revoked"] is True

    assert not svc.evaluate_action(case_id, "ask_reason", "Why?", source="direct").may_execute
    mcp = anyio.run(mcp_server.server.call_tool, "evaluate_action",
                    {"case_id": case_id, "verb": "ask_reason", "trigger": "Why?", "source": "mcp"})
    assert mcp.structured_content["may_execute"] is False

    import os
    os.environ["SCOPELOCK_API_TOKEN"] = "t0ken"
    try:
        api = TestClient(app).post(
            f"/api/cases/{case_id}/evaluate",
            json={"verb": "ask_reason", "trigger": "Why?"},
            headers={"Authorization": f"Bearer {case_token(case_id)}"},
        )
    finally:
        del os.environ["SCOPELOCK_API_TOKEN"]
    assert api.status_code == 200 and api.json()["may_execute"] is False

    spoken: list[str] = []
    call = SimpleNamespace(get_variable=lambda k: case_id, read_script=spoken.append)
    institution._intent_recognizer.classify = lambda request: [SimpleNamespace(key="ask_reason")]
    assert institution.on_action_request(call, "Why was it denied?") is None
    assert spoken and "no longer active" in spoken[0]

    result = svc.authorize_effect(case_id, "ask_reason", "Why?", "test", "k1")
    assert (result.status, result.reason) == ("refused", "revoked")
    assert not db.mandate_confirmed(case_id)


def test_revocation_cancels_pending_consultation_and_its_late_answer_authorizes_nothing():
    """Oracle: a pending decision becomes cancelled; the late answer raises and no effect exists."""
    case_id = confirmed_case()
    decision_id = consult.request_holder_decision(case_id, "reschedule", "Tuesday?", "¿Martes?")
    revoked = PolicyService().revoke_mandate(case_id, "changed her mind", source="test")
    assert revoked.cancelled_decisions == 1

    with pytest.raises(consult.DecisionNotPending):
        consult.resolve_holder_decision(decision_id, "Sí", "Yes", 900)
    row = db.connect().execute(
        "SELECT status, cancel_reason, decided_by FROM decision_request WHERE id = ?", (decision_id,)
    ).fetchone()
    assert dict(row) == {"status": "cancelled", "cancel_reason": "revoked", "decided_by": None}
    result = PolicyService().authorize_effect(
        case_id, "reschedule", "Yes", "test", f"decision:{decision_id}", decision_id
    )
    assert result.status == "refused"
    assert effects(case_id) == []


def test_voice_leg_reports_withdrawal_when_revoked_mid_consultation(monkeypatch):
    """Oracle: the rep hears the withdrawal script once and the household answer writes no effect."""
    from scopelock.agents import household, institution

    monkeypatch.setattr(household.translate, "translate", lambda text, lang: "Yes, Tuesday works")
    case_id = confirmed_case()
    consult.queue_holder_verb(case_id, "reschedule")
    consult.begin(case_id, "Does Tuesday work?", "¿Le sirve el martes?")

    rep_spoken: list[str] = []
    rep_hangups: list[str] = []
    rep = SimpleNamespace(read_script=rep_spoken.append, hangup=rep_hangups.append,
                          set_task=lambda *a, **k: pytest.fail("must not resume after revocation"))
    monkeypatch.setattr(household.relay, "institution_call", lambda cid: rep)

    PolicyService().revoke_mandate(case_id, "ya no", source="api")

    holder = SimpleNamespace(get_variable=lambda k: case_id,
                             get_field=lambda k: "Sí, el martes",
                             set_task=lambda *a, **k: None)
    household.on_consult_answered(holder)
    assert len(rep_spoken) == 1 and "no longer active" in rep_spoken[0]
    assert len(rep_hangups) == 1
    assert effects(case_id) == []


def test_voice_leg_commits_one_effect_for_an_affirmative_answer(monkeypatch):
    """Valid control: the same flow without revocation commits exactly one effect and resumes."""
    from scopelock.agents import household

    monkeypatch.setattr(household.translate, "translate", lambda text, lang: "Yes, Tuesday works")
    case_id = confirmed_case()
    consult.queue_holder_verb(case_id, "reschedule")
    consult.begin(case_id, "Does Tuesday work?", "¿Le sirve el martes?")
    resumed: list = []
    monkeypatch.setattr(household.relay, "institution_call", lambda cid: SimpleNamespace())
    monkeypatch.setattr(household.institution, "resume_after_consult",
                        lambda call, answer: resumed.append(answer))
    holder = SimpleNamespace(get_variable=lambda k: case_id,
                             get_field=lambda k: "Sí, el martes",
                             set_task=lambda *a, **k: None)

    household.on_consult_answered(holder)
    household.on_consult_answered(holder)  # duplicate callback

    assert resumed == ["Yes, Tuesday works"]
    assert [(e["verb"], e["source"]) for e in effects(case_id)] == [("reschedule", "institution_agent")]


def test_effect_and_revocation_race_is_serialized_at_the_worst_moment(monkeypatch):
    """Oracle: revocation attempted after the checks pass but before the effect is written
    blocks until the effect commits, so the effect's seq precedes revoked_seq, and the next
    attempt is refused. Negative control: tests/mutants show this fails if check and write
    are split across transactions."""
    case_id = confirmed_case()
    started = threading.Event()
    holder: dict = {}

    def hook(cid):
        thread, box = in_thread(PolicyService().revoke_mandate, cid, "race", "test")
        holder["revoker"] = (thread, box)
        started.set()
        time.sleep(0.3)
        assert thread.is_alive(), "revocation was not blocked by the open authorization"

    monkeypatch.setattr(policy, "_BEFORE_EFFECT_WRITE", hook)
    result = PolicyService().authorize_effect(case_id, "request_ref", "Ref please", "test", "race-1")
    monkeypatch.setattr(policy, "_BEFORE_EFFECT_WRITE", None)
    thread, box = holder["revoker"]
    thread.join(10)
    assert "error" not in box, box.get("error")
    assert result.status == "committed"

    version = db.latest_mandate_version(case_id)
    assert version["revoked_seq"] > result.seq
    after = PolicyService().authorize_effect(case_id, "request_ref", "Ref please", "test", "race-2")
    assert (after.status, after.reason) == ("refused", "revoked")


def test_concurrent_effects_and_revocation_never_commit_after_revoked_seq():
    """Oracle: across 30 rounds of 6 writer threads + 1 revoker, every committed effect has
    seq < revoked_seq and every effect attempted after revocation commits is refused."""
    for round_no in range(30):
        db.reset_db()
        case_id = confirmed_case()
        barrier = threading.Barrier(7)

        def writer(i):
            barrier.wait()
            return PolicyService().authorize_effect(
                case_id, "request_written", "Send it in writing", "test", f"r{round_no}-w{i}"
            )

        def revoker():
            barrier.wait()
            return PolicyService().revoke_mandate(case_id, "stop", "test")

        runs = [in_thread(writer, i) for i in range(6)] + [in_thread(revoker)]
        for thread, _ in runs:
            thread.join(20)
        for _, box in runs:
            assert "error" not in box, box.get("error")
        revoked_seq = db.latest_mandate_version(case_id)["revoked_seq"]
        for _, box in runs[:6]:
            res = box["value"]
            if res.status == "committed":
                assert res.seq < revoked_seq, f"round {round_no}: effect after revocation"
            else:
                assert (res.status, res.reason) == ("refused", "revoked")
        assert all(e["seq"] < revoked_seq for e in effects(case_id))


# ---------------------------------------------------------------- expiry


def test_expired_mandate_refuses_and_closes_pending_decision_then_reconfirm_starts_v2(monkeypatch):
    """Oracle: at t >= expires_at evaluate and authorize refuse, a pending answer is
    cancelled with reason 'expired', and only a fresh spoken confirmation restores authority."""
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(db, "_clock", lambda: clock["t"])
    case_id = confirmed_case(ttl_seconds=60)
    decision_id = consult.request_holder_decision(case_id, "escalate", "Supervisor?", "¿Supervisor?")
    svc = PolicyService()
    assert svc.authorize_effect(case_id, "request_ref", "ref", "test", "e1").status == "committed"

    clock["t"] += 60
    assert not svc.evaluate_action(case_id, "request_ref", "ref", "test").may_execute
    assert svc.authorize_effect(case_id, "request_ref", "ref", "test", "e2").reason == "expired"
    assert not db.mandate_confirmed(case_id)
    with pytest.raises(consult.DecisionNotPending):
        consult.resolve_holder_decision(decision_id, "Sí", "Yes", 5)
    row = db.connect().execute(
        "SELECT status, cancel_reason FROM decision_request WHERE id = ?", (decision_id,)
    ).fetchone()
    assert (row["status"], row["cancel_reason"]) == ("cancelled", "expired")

    svc.confirm_mandate(case_id, "maybe")  # ambiguous: still expired
    assert db.mandate_version_status(case_id) == "expired"
    svc.confirm_mandate(case_id, "Sí", ttl_seconds=60)
    assert db.latest_mandate_version(case_id)["version"] == 2
    assert svc.authorize_effect(case_id, "request_ref", "ref", "test", "e3").status == "committed"


def test_holder_decision_from_an_older_mandate_version_cannot_authorize_v2():
    """Oracle: a resolved 'yes' given under v1 is refused under v2 (decision_predates_mandate)."""
    case_id = confirmed_case()
    old = resolved_decision(case_id)
    svc = PolicyService()
    svc.revoke_mandate(case_id, "pause", "test")
    svc.confirm_mandate(case_id, "Sí, de acuerdo")
    result = svc.authorize_effect(case_id, "reschedule", "ok", "test", "k", old)
    assert (result.status, result.reason) == ("refused", "decision_predates_mandate")


# ---------------------------------------------------------------- duplicates


def test_duplicate_callbacks_produce_one_effect_even_when_concurrent():
    """Oracle: 8 threads delivering the same idempotency key yield exactly one committed
    effect; the other 7 return 'duplicate' naming the same effect id."""
    case_id = confirmed_case()
    barrier = threading.Barrier(8)

    def deliver():
        barrier.wait()
        return PolicyService().authorize_effect(case_id, "request_ref", "Ref?", "test", "cb-1")

    runs = [in_thread(deliver) for _ in range(8)]
    for thread, _ in runs:
        thread.join(20)
    results = [box["value"] for _, box in runs]
    assert sorted(r.status for r in results) == ["committed"] + ["duplicate"] * 7
    assert len({r.effect_id for r in results}) == 1
    assert len(effects(case_id)) == 1


def test_concurrent_holder_answers_have_exactly_one_winner():
    """Oracle: two racing resolutions of one request; exactly one succeeds."""
    case_id = confirmed_case()
    decision_id = consult.request_holder_decision(case_id, "escalate", "Supervisor?", "¿Supervisor?")
    barrier = threading.Barrier(2)

    def answer(text):
        barrier.wait()
        consult.resolve_holder_decision(decision_id, text, text, 10)
        return text

    runs = [in_thread(answer, "Sí"), in_thread(answer, "No")]
    for thread, _ in runs:
        thread.join(20)
    outcomes = ["value" in box for _, box in runs]
    assert sorted(outcomes) == [False, True]
    loser = next(box for _, box in runs if "error" in box)
    assert isinstance(loser["error"], consult.DecisionNotPending)


def test_one_holder_decision_cannot_be_spent_twice_or_reused_with_a_new_key():
    """Oracle: the same resolved decision under a second idempotency key is refused."""
    case_id = confirmed_case()
    decision_id = resolved_decision(case_id)
    svc = PolicyService()
    first = svc.authorize_effect(case_id, "reschedule", "ok", "test", "a", decision_id)
    second = svc.authorize_effect(case_id, "reschedule", "ok", "test", "b", decision_id)
    replay = svc.authorize_effect(case_id, "reschedule", "ok", "test", "a", decision_id)
    assert first.status == "committed"
    assert (second.status, second.reason) == ("refused", "decision_already_used")
    assert (replay.status, replay.effect_id) == ("duplicate", first.effect_id)
    conflict = svc.authorize_effect(case_id, "request_ref", "ok", "test", "a")
    assert (conflict.status, conflict.reason) == ("refused", "idempotency_conflict")


def test_requires_holder_verbs_need_a_matching_affirmative_resolved_decision():
    """Valid and invalid controls for the holder gate."""
    case_id = confirmed_case()
    svc = PolicyService()
    assert svc.authorize_effect(case_id, "escalate", "x", "t", "k0").reason == "holder_decision_required"
    no = resolved_decision(case_id, "escalate", answer="No, gracias")
    assert svc.authorize_effect(case_id, "escalate", "x", "t", "k1", no).reason == "holder_declined"
    wrong_verb = resolved_decision(case_id, "reschedule")
    assert svc.authorize_effect(case_id, "escalate", "x", "t", "k2", wrong_verb).reason == "decision_mismatch"
    pending = consult.request_holder_decision(case_id, "escalate", "q", "q")
    assert svc.authorize_effect(case_id, "escalate", "x", "t", "k3", pending).reason == "decision_not_resolved"
    yes = resolved_decision(case_id, "escalate")
    assert svc.authorize_effect(case_id, "escalate", "x", "t", "k4", yes).status == "committed"
    assert svc.authorize_effect(case_id, "agree_payment", "x", "t", "k5").reason == "forbidden"
    assert svc.authorize_effect(case_id, "request_ref", "x", "t", "k6", yes).reason == "decision_mismatch"


def test_unconfirmed_draft_authorizes_no_effect():
    case_id = db.create_case(holder_name="A", holder_lang="spanish", institution="I",
                             issue_type="denial", issue_summary="s")
    PolicyService().create_draft(case_id)
    result = PolicyService().authorize_effect(case_id, "request_ref", "x", "t", "k")
    assert (result.status, result.reason) == ("refused", "not_confirmed")


def test_confirmed_mandate_cannot_be_redrafted_wider():
    """Oracle: create_draft on a live confirmed mandate raises, so confirmation cannot be reset."""
    case_id = confirmed_case()
    with pytest.raises(ValueError):
        PolicyService().create_draft(case_id)
    assert db.mandate_confirmed(case_id)


# ---------------------------------------------------------------- restart


_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from scopelock.core import consult, db
    from scopelock.core.policy import PolicyService
    db.DB_PATH = Path(sys.argv[1])
    step = sys.argv[2]
    svc = PolicyService()
    out = {}
    if step == "before":
        case_id = db.create_case(holder_name="M", holder_lang="spanish", institution="I",
                                 issue_type="denial", issue_summary="s")
        svc.create_draft(case_id)
        svc.confirm_mandate(case_id, "Sí")
        out["case_id"] = case_id
        out["effect"] = svc.authorize_effect(case_id, "request_ref", "r", "voice", "k-restart").status
        out["pending"] = consult.request_holder_decision(case_id, "reschedule", "q", "q")
    else:
        case_id = sys.argv[3]
        out["recovered"] = consult.recover_after_restart()
        out["replay"] = svc.authorize_effect(case_id, "request_ref", "r", "voice", "k-restart").status
        out["version_status"] = db.mandate_version_status(case_id)
        out["revoked"] = svc.revoke_mandate(case_id, "stop", "api").revoked
    print(json.dumps(out))
    """
)


def _child(db_path: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(db_path), *args],
        cwd=REPO, capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "GUAVA_API_KEY": "gva-test-placeholder-not-a-real-key"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_state_survives_a_real_process_restart_and_pending_requests_are_cancelled(isolated_db):
    """Oracle: process 2 sees process 1's version and effect; recovery cancels the orphaned
    pending decision (never resolves it); the replayed callback is a duplicate; a revocation
    from process 2 is visible to this process without restarting it."""
    db_path = isolated_db.parent / "restart.db"
    first = _child(db_path, "before")
    assert first["effect"] == "committed"
    second = _child(db_path, "after", first["case_id"])
    assert second == {"recovered": 1, "replay": "duplicate", "version_status": "active", "revoked": True}

    import scopelock.core.db as live_db
    live_db.DB_PATH = db_path
    conn = getattr(live_db._local, "conn", None)
    if conn is not None:
        conn.close()
        del live_db._local.conn
    row = live_db.connect().execute(
        "SELECT status, cancel_reason, decided_by FROM decision_request WHERE id = ?",
        (first["pending"],),
    ).fetchone()
    assert dict(row) == {"status": "cancelled", "cancel_reason": "restart", "decided_by": None}
    assert not PolicyService().evaluate_action(first["case_id"], "request_ref", "r", "x").may_execute


@pytest.mark.parametrize(('spanish', 'english', 'expected'), [
    ('No, gracias', 'Yes, of course', 'refused'),
    ('Sí, claro', 'No, thank you', 'refused'),
    ('Tal vez', 'Yes', 'refused'),
    ('Sí, claro', '', 'committed'),
    ('Sí, claro', 'Yes, of course', 'committed'),
])
def test_translation_cannot_override_holder_answer(spanish, english, expected):
    case_id = confirmed_case()
    decision_id = consult.request_holder_decision(case_id, 'escalate', 'Escalate?', '¿Escalar?')
    consult.resolve_holder_decision(decision_id, spanish, english, 50)
    result = PolicyService().authorize_effect(case_id, 'escalate', 'Please escalate',
                                              'offline-test', 'translation-control', decision_id)
    assert result.status == expected
    assert len(effects(case_id)) == (1 if expected == 'committed' else 0)


@pytest.mark.parametrize(('text', 'allowed'), [
    ('Why is a Social Security number required?', True),
    ('Why was the SSN needed for this claim?', True),
    ('¿Por qué se requiere un número de seguro social?', True),
    ('¿Por qué se necesita el número de seguro social?', True),
    ('Why is a Social Security number required? Give it to me now.', False),
    ('¿Por qué se requiere un número de seguro social? Compártelo ahora.', False),
    ('Why is a Social Security number required 123-45-6789?', False),
    ('Please disclose the SSN', False),
])
def test_information_question_is_distinct_from_identifier_disclosure(text, allowed):
    case_id = confirmed_case()
    result = PolicyService().evaluate_action(case_id, 'ask_reason', text, 'offline-test')
    assert result.may_execute is allowed
    # Even a harmless trigger cannot enable an explicitly forbidden typed action.
    assert not PolicyService().evaluate_action(case_id, 'disclose_ssn', text, 'offline-test').may_execute


@pytest.mark.parametrize("answer", ["Yes, don't reschedule", "Yes, I can't agree", "Yes, I won’t agree"])
def test_negative_contraction_cannot_authorize_holder_effect(answer):
    """A positive token followed by a refusal is not holder consent."""
    case_id = confirmed_case()
    decision_id = consult.request_holder_decision(case_id, "reschedule", "New date?", "¿Nueva fecha?")
    consult.resolve_holder_decision(decision_id, "Sí", answer, 100)
    result = PolicyService().authorize_effect(case_id, "reschedule", "New date", "test", "contraction", decision_id)
    assert result.status == "refused"
    assert result.reason == "holder_declined"
    assert effects(case_id) == []


def test_health_confirmation_tracks_mandate_expiry(monkeypatch):
    """Health uses the same expiry status as authorization, not only stored flags."""
    from scopelock.api.server import health
    monkeypatch.setattr(db, "_clock", lambda: 1000.0)
    confirmed_case(ttl_seconds=1)
    assert health()["mandate"]["confirmed"]
    monkeypatch.setattr(db, "_clock", lambda: 1002.0)
    assert not health()["mandate"]["confirmed"]
