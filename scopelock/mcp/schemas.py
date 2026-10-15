"""Validated MCP input and structured-output models.

The transport accepts only the fields declared here.  In particular, holder
decision resolution deliberately has no caller-controlled ``decided_by`` field.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Decision = Literal["allowed", "requires_holder", "forbidden"]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]
Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
Verb = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class StrictModel(BaseModel):
    """Forbid undeclared MCP input fields instead of passing them through."""

    model_config = ConfigDict(extra="forbid")


class GetActiveCaseInput(StrictModel):
    pass


class ActiveCaseOutput(StrictModel):
    case_id: Identifier
    state: NonEmptyText
    holder_language: NonEmptyText
    institution: NonEmptyText
    issue_type: NonEmptyText


class GetMandateInput(StrictModel):
    case_id: Identifier


class MandateRuleOutput(StrictModel):
    verb: Verb
    disposition: Decision
    confirmed_by_holder: bool
    confirmed_at: str | None = None


class MandateOutput(StrictModel):
    case_id: Identifier
    rules: list[MandateRuleOutput]


class EvaluateActionInput(StrictModel):
    case_id: Identifier
    verb: Verb
    trigger: NonEmptyText
    source: NonEmptyText


class EvaluateActionOutput(StrictModel):
    decision: Decision
    may_execute: bool
    requires_holder: bool
    refusal: str | None
    audit_event_id: Identifier


class RestrictActionInput(StrictModel):
    case_id: Identifier
    verb: Verb


class RestrictActionOutput(StrictModel):
    case_id: Identifier
    verb: Verb
    disposition: Literal["forbidden"]
    restricted: Literal[True] = True


class RequestHolderDecisionInput(StrictModel):
    case_id: Identifier
    verb: Verb
    question_en: NonEmptyText
    question_es: NonEmptyText


class RequestHolderDecisionOutput(StrictModel):
    decision_request_id: Identifier
    status: Literal["pending"] = "pending"


class ResolveHolderDecisionInput(StrictModel):
    decision_id: Identifier
    answer_es: NonEmptyText
    answer_en: NonEmptyText
    latency_ms: Annotated[int, Field(ge=0)]


class ResolveHolderDecisionOutput(StrictModel):
    decision_request_id: Identifier
    status: Literal["resolved"] = "resolved"
    decided_by: Literal["holder"] = "holder"


class GetCallbackCardInput(StrictModel):
    case_id: Identifier


class CallbackCardOutput(StrictModel):
    case_id: Identifier
    asked: list[str]
    told: list[str]
    reference_no: str | None
    agreed: str | None
    refused: list[str]
    next_step: str | None
    readback_es: str


class GetAuditReportInput(StrictModel):
    case_id: Identifier


class AuditEventOutput(BaseModel):
    """Person C's frozen report is a redacted list of structured dictionaries."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    case_id: str | None = None
    event_type: str | None = None
    verb: str | None = None
    disposition: str | None = None
    source: str | None = None
    trigger_redacted: str | None = None
    result: str | None = None
    created_at: str | None = None


class AuditReportOutput(StrictModel):
    case_id: Identifier
    events: list[AuditEventOutput]


class RunSafetyScenarioInput(StrictModel):
    case_id: Identifier
    proposed_verbs: Annotated[list[Verb], Field(min_length=1, max_length=100)]


class SafetyOutcome(StrictModel):
    verb: Verb
    decision: Decision
    may_execute: bool
    requires_holder: bool
    refusal: str | None
    audit_event_id: Identifier


class RunSafetyScenarioOutput(StrictModel):
    case_id: Identifier
    outcomes: list[SafetyOutcome]


class RevokeMandateInput(StrictModel):
    case_id: Identifier
    reason: NonEmptyText


class RevokeMandateOutput(StrictModel):
    case_id: Identifier
    revoked: bool
    version: int | None
    cancelled_decisions: int
    audit_event_id: Identifier


class AuthorizeEffectInput(StrictModel):
    case_id: Identifier
    verb: Verb
    trigger: NonEmptyText
    idempotency_key: Identifier
    decision_request_id: Identifier | None = None


class AuthorizeEffectOutput(StrictModel):
    status: Literal["committed", "duplicate", "refused"]
    reason: str | None
    effect_id: str | None
    seq: int | None
    mandate_version: int | None
    audit_event_id: Identifier
