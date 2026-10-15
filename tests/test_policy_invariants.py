"""Mandate invariants under ARBITRARY operation sequences, not single examples.

The single-example tests next door each pin one behaviour of the policy engine. What a
skeptical reviewer asks is different: given any order of drafting, narrowing, confirming and
evaluating -- including the orders a confused caller or a hostile representative would
produce -- can authority ever end up HIGHER than the holder granted?

These tests answer that by driving the real PolicyService and the real SQLite store through
thousands of seeded random sequences and asserting five invariants after every single step.
Seeds are fixed, so a failure names an exact reproducible sequence.

No live Guava calls; this is the local policy core only.
"""
from __future__ import annotations

import random
from collections import Counter

import pytest

from scopelock.core import db
from scopelock.core.mandate import DEFAULT_MANDATE, FORBIDDEN_ACTIONS
from scopelock.core.policy import _PERMISSION_RANK, PolicyService

SEEDS = range(400)
VERBS = sorted(DEFAULT_MANDATE)
# Verbs the engine has never heard of. A representative can say anything; the engine must
# treat an unrecognised action as forbidden rather than as unconstrained.
UNKNOWN_VERBS = ["wire_funds", "", "ask_reason ", "AGREE_PAYMENT", "../../escalate"]
AFFIRMATIVES = ["si", "yes", "de acuerdo", "confirmo", "claro"]
NON_AFFIRMATIVES = ["no", "tal vez", "no se", "maybe", "not sure", ""]


@pytest.fixture(autouse=True)
def clean_db():
    db.reset_db()
    yield


def _make_case(name: str = "Maria Gomez") -> str:
    return db.create_case(
        holder_name=name,
        holder_lang="spanish",
        institution="Valley Health Plan",
        issue_type="denial",
        issue_summary="They denied her MRI claim.",
    )


def _dispositions(svc: PolicyService, case_id: str) -> dict[str, str]:
    return {row["verb"]: row["disposition"] for row in svc.get_mandate(case_id)}


def _policy_events(case_id: str) -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT verb, disposition, result FROM policy_event WHERE case_id = ?",
        (case_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _random_overrides(rnd: random.Random) -> dict[str, str]:
    """Any mix of narrowing and ATTEMPTED WIDENING, which is the interesting half."""
    out: dict[str, str] = {}
    for verb in VERBS:
        if rnd.random() < 0.5:
            out[verb] = rnd.choice(["forbidden", "requires_holder", "allowed"])
    return out


# I1. Authority is monotonically non-increasing. A draft may narrow what the holder allows;
# no sequence of overrides may grant a verb MORE authority than the built-in default. This is
# the whole point of a mandate: the agent cannot award itself powers.
def test_no_sequence_of_overrides_ever_exceeds_the_default_authority():
    svc = PolicyService()
    for seed in SEEDS:
        rnd = random.Random(seed)
        db.reset_db()
        case_id = _make_case()
        svc.create_draft(case_id, _random_overrides(rnd))

        for _ in range(rnd.randint(0, 6)):
            verb = rnd.choice(VERBS)
            try:
                svc.restrict_action(case_id, verb)
            except ValueError:
                pass  # confirmed mandates refuse modification; that is invariant I3
            for got_verb, got in _dispositions(svc, case_id).items():
                assert _PERMISSION_RANK[got] <= _PERMISSION_RANK[DEFAULT_MANDATE[got_verb]], (
                    f"seed {seed}: {got_verb} escalated to {got}, above default "
                    f"{DEFAULT_MANDATE[got_verb]}"
                )


# I2. The four forbidden verbs are absolutely forbidden, and anything unrecognised is too.
# may_execute must be False for them after any sequence whatsoever.
def test_forbidden_and_unknown_verbs_are_never_executable():
    svc = PolicyService()
    for seed in SEEDS:
        rnd = random.Random(seed)
        db.reset_db()
        case_id = _make_case()
        svc.create_draft(case_id, _random_overrides(rnd))
        if rnd.random() < 0.5:
            svc.confirm_mandate(case_id, rnd.choice(AFFIRMATIVES))

        for verb in list(FORBIDDEN_ACTIONS) + UNKNOWN_VERBS:
            decision = svc.evaluate_action(case_id, verb, "representative asked", source="test")
            assert not decision.may_execute, f"seed {seed}: {verb!r} became executable"
            assert decision.decision == "forbidden", f"seed {seed}: {verb!r} -> {decision.decision}"
            assert decision.refusal, f"seed {seed}: {verb!r} was blocked with no spoken refusal"


# I3. A confirmed mandate is frozen. The holder confirmed a specific contract aloud; nothing
# after that may change any disposition, and the attempt must raise rather than silently
# no-op, so a caller cannot believe it narrowed something it did not.
def test_a_confirmed_mandate_cannot_be_modified_by_any_later_operation():
    svc = PolicyService()
    for seed in SEEDS:
        rnd = random.Random(seed)
        db.reset_db()
        case_id = _make_case()
        svc.create_draft(case_id, _random_overrides(rnd))
        svc.confirm_mandate(case_id, rnd.choice(AFFIRMATIVES))
        assert db.mandate_confirmed(case_id), f"seed {seed}: clear affirmative did not confirm"

        frozen = _dispositions(svc, case_id)
        for _ in range(rnd.randint(1, 6)):
            verb = rnd.choice(VERBS)
            with pytest.raises(ValueError):
                svc.restrict_action(case_id, verb)
            svc.evaluate_action(case_id, rnd.choice(VERBS + UNKNOWN_VERBS), "x", source="test")
        assert _dispositions(svc, case_id) == frozen, f"seed {seed}: confirmed mandate drifted"


# I4. Audit completeness and agreement. Every evaluated action leaves exactly one event, and
# that event's recorded result matches what the caller was told. A ledger that disagrees with
# the decision it describes is worse than no ledger: the Callback Card is built from it.
#
# SCOPE, measured not assumed: this compares the MULTISET of (verb, result) pairs, not their
# order. policy_event.created_at is second-granularity and the row id's millisecond prefix
# ties for events in the same millisecond, so the ledger cannot recover the order of two
# actions inside one millisecond. On a real call actions are seconds apart, so this is a
# limitation of the ledger's resolution rather than a loss of the decisions themselves --
# but it is a limitation, and this test does not paper over it by asserting an order the
# schema cannot guarantee. Recovering order would need a monotonic sequence column.
def test_every_evaluation_leaves_exactly_one_truthful_audit_event():
    svc = PolicyService()
    for seed in SEEDS:
        rnd = random.Random(seed)
        db.reset_db()
        case_id = _make_case()
        svc.create_draft(case_id, _random_overrides(rnd))

        expected: list[tuple[str, str]] = []
        for _ in range(rnd.randint(1, 8)):
            verb = rnd.choice(VERBS + UNKNOWN_VERBS)
            d = svc.evaluate_action(case_id, verb, "representative said something", source="test")
            result = (
                "allowed" if d.may_execute
                else "holder_decision_required" if d.requires_holder
                else "blocked"
            )
            expected.append((verb, result))

        events = _policy_events(case_id)
        assert len(events) == len(expected), (
            f"seed {seed}: {len(expected)} evaluations produced {len(events)} audit events"
        )
        assert sorted(Counter((e["verb"], e["result"]) for e in events).items()) == sorted(
            Counter(expected).items()
        ), f"seed {seed}: ledger disagrees with the decisions the caller was given"


# I5. Cross-case isolation. Two cases are two different households. Nothing done on one may
# change the other's dispositions or land in the other's ledger -- the leak that would let one
# caller's narrowed mandate loosen another's, or expose one household's actions in another's card.
def test_operations_on_one_case_never_touch_another_case():
    svc = PolicyService()
    for seed in SEEDS:
        rnd = random.Random(seed)
        db.reset_db()
        a, b = _make_case("Maria Gomez"), _make_case("Ana Ruiz")
        svc.create_draft(a, _random_overrides(rnd))
        svc.create_draft(b, _random_overrides(rnd))
        before_b = _dispositions(svc, b)

        for _ in range(rnd.randint(1, 6)):
            op = rnd.choice(["restrict", "evaluate", "confirm"])
            if op == "restrict":
                try:
                    svc.restrict_action(a, rnd.choice(VERBS))
                except ValueError:
                    pass
            elif op == "evaluate":
                svc.evaluate_action(a, rnd.choice(VERBS + UNKNOWN_VERBS), "x", source="test")
            else:
                svc.confirm_mandate(a, rnd.choice(AFFIRMATIVES + NON_AFFIRMATIVES))

        assert _dispositions(svc, b) == before_b, f"seed {seed}: case B's mandate changed"
        assert _policy_events(b) == [], f"seed {seed}: case A's events landed in case B's ledger"
        assert not db.mandate_confirmed(b), f"seed {seed}: confirming case A confirmed case B"


# I2b. Defense in depth, verified by mutation rather than assumed.
#
# Deleting the forbidden/unknown guard in evaluate_action fails NONE of the tests above,
# because create_draft's rank clamp already means a stored forbidden verb can only ever hold
# "forbidden". That makes the guard look like dead code. It is not: it is the last line of
# defense if anything ever writes a disposition without going through create_draft --
# db.set_mandate_disposition is module-public and a future caller, a migration or a bug could
# use it. This test reaches past the policy service and writes the escalation straight into
# the store, which is the only way to prove the guard actually holds the line.
def test_a_forbidden_verb_written_straight_into_the_store_is_still_refused():
    svc = PolicyService()
    for verb in FORBIDDEN_ACTIONS:
        db.reset_db()
        case_id = _make_case()
        svc.create_draft(case_id)
        db.set_mandate_disposition(case_id, verb, "allowed")
        assert _dispositions(svc, case_id)[verb] == "allowed", "the store did not take the escalation"

        decision = svc.evaluate_action(case_id, verb, "representative pushed for it", source="test")
        assert not decision.may_execute, f"{verb} became executable via a direct store write"
        assert decision.decision == "forbidden", f"{verb} evaluated as {decision.decision}"
