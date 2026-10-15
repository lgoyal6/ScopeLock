"""Serve local console status and case-scoped authorization endpoints.
Read-only console routes are unauthenticated; mutations use case capabilities.
PolicyService and db.py enforce mandate state without claiming caller identity.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import os
import re
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from scopelock.core import audit, card, db
from scopelock.core.policy import PolicyService
from scopelock.core.mandate import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS
from scopelock.core.redact import redact

app = FastAPI(title="ScopeLock console API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_methods=["GET"],
    allow_headers=["Accept", "Content-Type"],
)


def _current_case() -> dict | None:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM kase WHERE state != 'closed' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM kase ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _table_exists(table: str) -> bool:
    return db.connect().execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    return value


def _decision_requests(case_id: str) -> list[dict]:
    if not _table_exists("decision_request"):
        return []
    return [
        dict(row)
        for row in db.connect()
        .execute(
            "SELECT * FROM decision_request WHERE case_id = ? ORDER BY created_at, id", (case_id,)
        )
        .fetchall()
    ]


def _mandate_rules(case_id: str) -> list[dict]:
    rules = [dict(row) for row in db.mandate_rules(case_id)]
    for rule in rules:
        rule["verb_label"] = ALLOWED_ACTIONS.get(rule["verb"]) or FORBIDDEN_ACTIONS.get(
            rule["verb"], rule["verb"]
        )
    return rules


def _mcp_readiness() -> dict[str, bool]:
    try:
        spec = importlib.util.find_spec("scopelock.mcp.server")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None

    if spec is None:
        return {"configured": False, "importable": False, "runnable": False, "ready": False}

    try:
        module = importlib.import_module("scopelock.mcp.server")
        candidates = (
            getattr(module, "main", None),
            getattr(module, "mcp", None),
            getattr(module, "server", None),
            getattr(module, "mcp_server", None),
        )
        runnable = any(
            callable(candidate) or callable(getattr(candidate, "run", None))
            for candidate in candidates
            if candidate is not None
        )
    except Exception:
        return {"configured": True, "importable": False, "runnable": False, "ready": False}

    return {
        "configured": True,
        "importable": True,
        "runnable": runnable,
        "ready": runnable,
    }


@app.get("/api/state")
def state():
    case = _current_case()
    if case is None:
        return {
            "case": None,
            "mandate": [],
            "transcript": [],
            "consults": [],
            "decision_requests": [],
            "violations": [],
            "card": None,
            "decision_count": 0,
        }

    case_id = case["id"]
    consults = [dict(row) for row in db.consults(case_id)]
    decisions = _decision_requests(case_id)
    holder_decision_count = sum(
        1
        for decision in decisions
        if decision.get("decided_by") == "holder"
        and decision.get("status") in {"resolved", "completed"}
    )
    payload = {
        "case": case,
        "mandate": _mandate_rules(case_id),
        "transcript": [dict(row) for row in db.utterances(case_id)],
        "consults": consults,
        "decision_requests": decisions,
        "violations": [dict(row) for row in db.violations(case_id)],
        "card": db.get_card(case_id),
        "decision_count": holder_decision_count if decisions else len(consults),
    }
    return _redact_payload(payload)


@app.get("/api/health")
def health():
    try:
        conn = db.connect()
        database_ready = conn.execute("SELECT 1").fetchone()[0] == 1 and _table_exists("kase")
        case = _current_case()
    except Exception:
        database_ready = False
        case = None

    active_case_ready = bool(case and case.get("state") != "closed")
    mandate_confirmed = False
    if database_ready and active_case_ready and case is not None:
        mandate_confirmed = db.mandate_confirmed(case["id"])

    phone_numbers = {
        "household_configured": bool(os.environ.get("HOUSEHOLD_NUMBER")),
        "institution_configured": bool(os.environ.get("INSTITUTION_NUMBER")),
    }
    phone_numbers["ready"] = all(phone_numbers.values())
    mcp = _mcp_readiness()
    readiness = {
        "phone_numbers": phone_numbers["ready"],
        "active_case": active_case_ready,
        "mandate_confirmed": mandate_confirmed,
        "mcp_server": mcp["ready"],
    }
    all_ready = database_ready and all(readiness.values())
    return {
        "status": "ready" if all_ready else "not_ready",
        "ready": all_ready,
        "process": {"ready": True},
        "database": {"ready": database_ready},
        "mcp": mcp,
        "phone_numbers": phone_numbers,
        "active_case": {"ready": active_case_ready},
        "mandate": {"confirmed": mandate_confirmed},
        "readiness": readiness,
    }


@app.get("/api/cases/{case_id}/audit")
def case_audit(case_id: str):
    if db.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return audit.get_audit_report(case_id)


@app.get("/api/cases/{case_id}/report")
def case_report(case_id: str):
    case = db.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")

    outcome = db.get_card(case_id) or {
        "case_id": case_id,
        "asked": [],
        "told": [],
        "reference_no": None,
        "agreed": None,
        "refused": [],
        "next_step": None,
    }
    return _redact_payload(
        {
            **outcome,
            "state": case["state"],
            "readback_es": card.build_readback_es(case_id),
        }
    )


# ---- Authorization API ---------------------------------------------------------------
# The console routes above are read-only. These routes change authorization state, so they
# are closed unless SCOPELOCK_API_TOKEN is set, and then require it as a bearer token.
# Revocation only removes authority; effects pass through the same PolicyService gate as
# MCP and the voice legs. Error bodies never echo request text.

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluateBody(_Body):
    verb: str = Field(min_length=1, max_length=128)
    trigger: str = Field(min_length=1, max_length=4096)


class EffectBody(_Body):
    verb: str = Field(min_length=1, max_length=128)
    trigger: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=128)
    decision_request_id: str | None = Field(default=None, max_length=128)


class RevokeBody(_Body):
    reason: str = Field(min_length=1, max_length=4096)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 body echoes the submitted values, which can carry an SSN or
    member ID. Report only which fields failed and why, never their contents."""
    return JSONResponse(
        status_code=422,
        content={"detail": [
            {"loc": [str(part) for part in error.get("loc", ())], "type": error.get("type")}
            for error in exc.errors()
        ]},
    )


def case_token(case_id: str, secret: str | None = None) -> str:
    """Case-scoped bearer capability: HMAC-SHA256(server secret, case ID).

    The operator mints one per case from SCOPELOCK_API_TOKEN; a token for one case is
    useless on another, and the raw server secret is never accepted as a bearer token.
    This is a service-to-service capability, not proof of the holder's identity.
    """
    secret = os.environ.get("SCOPELOCK_API_TOKEN", "") if secret is None else secret
    if not secret:
        raise ValueError("SCOPELOCK_API_TOKEN is not set")
    return hmac.new(secret.encode(), f"scopelock-case:{case_id}".encode(), hashlib.sha256).hexdigest()


def _require_token(authorization: str | None, case_id: str) -> None:
    if not os.environ.get("SCOPELOCK_API_TOKEN", ""):
        raise HTTPException(status_code=403, detail="Authorization API is disabled")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), case_token(case_id).encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")


def _correlation(response: Response, supplied: str | None) -> str:
    value = supplied if supplied and _CORRELATION_ID.match(supplied) else uuid.uuid4().hex
    response.headers["X-Correlation-ID"] = value
    return value


def _require_case(case_id: str) -> None:
    if db.get_case(case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")


@app.post("/api/cases/{case_id}/evaluate")
def api_evaluate(case_id: str, body: EvaluateBody, response: Response,
                 authorization: str | None = Header(default=None),
                 x_correlation_id: str | None = Header(default=None)):
    _require_token(authorization, case_id)
    _correlation(response, x_correlation_id)
    _require_case(case_id)
    return asdict(PolicyService().evaluate_action(case_id, body.verb, body.trigger, source="api"))


@app.post("/api/cases/{case_id}/effects")
def api_authorize_effect(case_id: str, body: EffectBody, response: Response,
                         authorization: str | None = Header(default=None),
                         x_correlation_id: str | None = Header(default=None)):
    _require_token(authorization, case_id)
    correlation_id = _correlation(response, x_correlation_id)
    _require_case(case_id)
    result = PolicyService().authorize_effect(
        case_id,
        body.verb,
        body.trigger,
        source="api",
        idempotency_key=body.idempotency_key,
        decision_request_id=body.decision_request_id,
        correlation_id=correlation_id,
    )
    return asdict(result)


@app.post("/api/cases/{case_id}/revoke")
def api_revoke(case_id: str, body: RevokeBody, response: Response,
               authorization: str | None = Header(default=None),
               x_correlation_id: str | None = Header(default=None)):
    _require_token(authorization, case_id)
    _correlation(response, x_correlation_id)
    _require_case(case_id)
    return asdict(PolicyService().revoke_mandate(case_id, body.reason, source="api"))


@app.get("/api/metrics")
def metrics():
    """Bounded counters only: fixed keys, no case IDs, no free text."""
    return audit.authz_metrics()


app.mount("/", StaticFiles(directory="scopelock/console", html=True), name="console")
