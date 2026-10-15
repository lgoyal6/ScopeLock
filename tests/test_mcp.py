"""In-process MCP v2 transport and ScopeLock seam tests."""
from __future__ import annotations

import inspect
import json
from dataclasses import asdict, is_dataclass
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from scopelock.core import card, db, mandate
from scopelock.mcp import server as mcp_server


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Give every MCP test its own SQLite file and connection."""
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
        del db._local.conn
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scopelock.db")
    db.reset_db()
    yield
    current = getattr(db._local, "conn", None)
    if current is not None:
        current.close()
        del db._local.conn


def make_case(*, state: str = "mandated") -> str:
    case_id = db.create_case(
        holder_name="Test Holder",
        holder_lang="spanish",
        institution="Example Health Plan",
        issue_type="denial",
        issue_summary="A test claim needs review.",
    )
    mandate.create_case_mandate(case_id)
    db.set_case_state(case_id, state)
    return case_id


def call_tool(name: str, arguments: dict[str, Any]):
    return anyio.run(mcp_server.server.call_tool, name, arguments)


def decision_dict(decision: object) -> dict[str, Any]:
    if is_dataclass(decision):
        return asdict(decision)
    return {
        "decision": decision.decision,
        "may_execute": decision.may_execute,
        "requires_holder": decision.requires_holder,
        "refusal": decision.refusal,
        "audit_event_id": decision.audit_event_id,
    }


def row_bytes(row) -> bytes:
    return json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode()


def test_registers_all_named_tools_and_only_redacted_resources():
    tools = anyio.run(mcp_server.server.list_tools)
    templates = anyio.run(mcp_server.server.list_resource_templates)

    assert {tool.name for tool in tools} == {
        "get_active_case",
        "get_mandate",
        "evaluate_action",
        "restrict_action",
        "request_holder_decision",
        "resolve_holder_decision",
        "get_callback_card",
        "get_audit_report",
        "run_safety_scenario",
        "revoke_mandate",
        "authorize_effect",
    }
    assert {template.uri_template for template in templates} == {
        "case://{case_id}/mandate",
        "case://{case_id}/audit",
        "case://{case_id}/callback-card",
    }


def test_get_active_case_returns_only_redacted_summary_fields():
    case_id = make_case()
    result = call_tool("get_active_case", {})

    assert result.structured_content == {
        "case_id": case_id,
        "state": "mandated",
        "holder_language": "spanish",
        "institution": "Example Health Plan",
        "issue_type": "denial",
    }


@pytest.mark.parametrize(
    ("verb", "trigger", "source"),
    [
        ("ask_reason", "Why was this denied?", "institution_agent"),
        ("reschedule", "Can we choose another day?", "institution_agent"),
        ("agree_payment", "Can you approve a payment?", "institution_agent"),
        ("unknown_action", "Please do something else.", "mcp_test"),
    ],
)
def test_mcp_evaluate_action_matches_direct_policy_service_byte_for_byte(
    verb: str, trigger: str, source: str, monkeypatch
):
    case_id = make_case()
    real_service = mcp_server.PolicyService()
    captured: dict[str, object] = {}

    class CapturingPolicyService:
        def evaluate_action(
            self, captured_case_id: str, captured_verb: str,
            captured_trigger: str, captured_source: str
        ):
            decision = real_service.evaluate_action(
                captured_case_id, captured_verb, captured_trigger, captured_source
            )
            captured["decision"] = decision
            return decision

    monkeypatch.setattr(mcp_server, "PolicyService", CapturingPolicyService)
    via_mcp = call_tool(
        "evaluate_action",
        {"case_id": case_id, "verb": verb, "trigger": trigger, "source": source},
    )

    direct_json = json.dumps(
        decision_dict(captured["decision"]), sort_keys=True, separators=(",", ":")
    )
    mcp_json = json.dumps(via_mcp.structured_content, sort_keys=True, separators=(",", ":"))
    assert mcp_json == direct_json


def test_restrict_action_has_no_enable_path_and_honors_confirmation_ceiling():
    case_id = make_case()
    tools = anyio.run(mcp_server.server.list_tools)
    schema = next(tool.input_schema for tool in tools if tool.name == "restrict_action")
    assert set(schema["properties"]) == {"case_id", "verb"}

    prohibited = call_tool(
        "restrict_action",
        {"case_id": case_id, "verb": "agree_payment", "allowed": True},
    )
    assert prohibited.structured_content["disposition"] == "forbidden"
    assert db.mandate_rule(case_id, "agree_payment")["disposition"] == "forbidden"

    result = call_tool(
        "restrict_action", {"case_id": case_id, "verb": "ask_reason"}
    )
    assert result.structured_content["disposition"] == "forbidden"
    assert db.mandate_rule(case_id, "ask_reason")["disposition"] == "forbidden"

    mandate.confirm(case_id, "Yes, I agree.")
    with pytest.raises(ToolError):
        call_tool("restrict_action", {"case_id": case_id, "verb": "request_ref"})
    assert db.mandate_rule(case_id, "request_ref")["disposition"] == "allowed"


def test_safety_scenario_isolates_the_live_case_and_cleans_up_copy():
    case_id = make_case(state="live")
    case_before = row_bytes(db.get_case(case_id))
    mandate_before = [row_bytes(row) for row in db.mandate_rules(case_id)]

    result = call_tool(
        "run_safety_scenario",
        {"case_id": case_id, "proposed_verbs": ["ask_reason", "agree_payment"]},
    )

    assert [item["verb"] for item in result.structured_content["outcomes"]] == [
        "ask_reason",
        "agree_payment",
    ]
    assert row_bytes(db.get_case(case_id)) == case_before
    assert [row_bytes(row) for row in db.mandate_rules(case_id)] == mandate_before
    assert (
        db.connect().execute("SELECT id FROM kase WHERE id LIKE 'scn_%'").fetchall()
        == []
    )


def test_holder_resolution_cannot_accept_agent_decision_or_confirm_mandate():
    case_id = make_case()
    created = call_tool(
        "request_holder_decision",
        {
            "case_id": case_id,
            "verb": "reschedule",
            "question_en": "Would another day work?",
            "question_es": "Le sirve otro dia?",
        },
    )
    decision_id = created.structured_content["decision_request_id"]

    resolved = call_tool(
        "resolve_holder_decision",
        {
            "decision_id": decision_id,
            "answer_es": "Si.",
            "answer_en": "Yes.",
            "latency_ms": 150,
            "decided_by": "agent",
        },
    )
    assert resolved.structured_content["decided_by"] == "holder"

    tool_names = {tool.name for tool in anyio.run(mcp_server.server.list_tools)}
    assert "confirm_mandate" not in tool_names
    assert "confirm_mandate(" not in inspect.getsource(mcp_server)


def test_resources_serialize_safe_structured_shapes_without_confirmation_text():
    case_id = make_case()
    mandate.confirm(case_id, "Yes, I agree.")
    card.add_asked(case_id, "Why was the test claim denied?")
    card.add_told(case_id, "The test claim needs another review.")
    card.set_reference(case_id, "TEST-REF")
    card.set_next_step(case_id, "Wait for a written response.")

    mandate_contents = list(
        anyio.run(mcp_server.server.read_resource, f"case://{case_id}/mandate")
    )
    audit_contents = list(
        anyio.run(mcp_server.server.read_resource, f"case://{case_id}/audit")
    )
    card_contents = list(
        anyio.run(
            mcp_server.server.read_resource,
            f"case://{case_id}/callback-card",
        )
    )

    mandate_payload = json.loads(mandate_contents[0].content)
    audit_payload = json.loads(audit_contents[0].content)
    card_payload = json.loads(card_contents[0].content)

    assert mandate_payload["case_id"] == case_id
    assert all("confirmed_utterance" not in rule for rule in mandate_payload["rules"])
    assert "Yes, I agree." not in mandate_contents[0].content
    assert audit_payload["case_id"] == case_id
    assert audit_payload["events"]
    assert all("raw_transcript" not in event for event in audit_payload["events"])
    assert card_payload == {
        "case_id": case_id,
        "asked": ["Why was the test claim denied?"],
        "told": ["The test claim needs another review."],
        "reference_no": "TEST-REF",
        "agreed": None,
        "refused": [],
        "next_step": "Wait for a written response.",
        "readback_es": card.build_readback_es(case_id),
    }
