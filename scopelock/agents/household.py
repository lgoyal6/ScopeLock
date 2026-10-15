"""C1 Intake + C2 mandate readback/confirmation + C4 household side of the consult pivot.
Spanish primary — the mother dials this leg.

Neither party waits on hold for the other. She calls in once, does intake, confirms the
mandate, and hangs up. Her real number is captured automatically from caller ID (not
asked for) and stored on the case. The moment the institution calls in, its on_call_start
places an outbound callback to her (see agents/institution.py) — on_reach_person below
picks that up and brings her back onto a fresh session, live, without her having to
dial anything or wait on the line herself.
"""
from __future__ import annotations

import guava
from guava import Agent, Field, Say
from guava.events import AgentSpeechEvent, CallerSpeechEvent

from scopelock.agents import institution, scripts
from scopelock.core import consult, db, mandate, relay, translate
from scopelock.core.util import safe

household = Agent(
    name=scripts.PRODUCT_NAME,
    organization=scripts.PRODUCT_NAME,
    purpose=scripts.HOUSEHOLD_PURPOSE,
    pronunciations=scripts.PRODUCT_PRONUNCIATIONS,
)

# See core/relay.py's HOUSEHOLD_AGENT docstring: lets institution.py place the outbound
# callback without importing this module (which would create an import cycle, since this
# module already imports institution.py for resume_after_consult).
relay.HOUSEHOLD_AGENT = household


@household.on_call_start
@safe
def on_call_start(call: guava.Call):
    call.set_language_mode(primary="spanish", secondary=["english"])

    case_id = call.get_variable("case_id")
    if case_id:
        # Outbound callback: bring her back onto an already-mandated case rather than
        # running intake again. reach_person() handles voicemail/wrong-number gracefully.
        row = db.get_case(case_id)
        call.reach_person(
            contact_full_name=row["holder_name"],
            greeting=f"Hola, ¿hablo con {row['holder_name']}?",
            voicemail_message=(
                "Le llama ScopeLock. Por favor, devuélvanos la llamada; el representante "
                "está esperando en la línea."
            ),
        )
        return

    call.set_variable("caller_phone", call.call_info.from_number)
    call.read_script(scripts.HOUSEHOLD_GREETING_ES)
    call.set_task(
        "intake",
        objective=(
            "The ScopeLock greeting was already spoken once. Do not greet or introduce "
            "yourself again. Understand who is calling, which institution is involved, and "
            "what happened. Ask one short question at a time. Speak only in Spanish unless "
            "she switches to English herself, and do not repeat a question she already answered."
        ),
        checklist=[
            Field(key="holder_name", field_type="text", sensitive=True,
                  description="Su nombre completo"),
            Field(key="institution", field_type="text",
                  description="El nombre de la institución o compañía"),
            Field(key="issue_type", field_type="multiple_choice",
                  choices=["denial", "billing", "reschedule", "dispute"],
                  description="El tipo de problema que tiene"),
            Field(key="issue_summary", field_type="text",
                  description="Lo que pasó, en sus propias palabras. No parafrasear."),
            Field(key="member_id", field_type="text", sensitive=True, required=False,
                  description="Su número de miembro o de cuenta, si lo tiene a mano"),
        ],
    )


@household.on_task_complete("intake")
@safe
def on_intake_done(call: guava.Call):
    case_id = db.create_case(
        holder_name=call.get_field("holder_name"),
        holder_lang="spanish",
        institution=call.get_field("institution"),
        issue_type=call.get_field("issue_type"),
        issue_summary=call.get_field("issue_summary"),
        holder_phone=call.get_variable("caller_phone"),
    )
    call.set_variable("case_id", case_id)
    call.set_variable("member_id", call.get_field("member_id") or "")
    mandate.create_case_mandate(case_id)

    call.set_task("mandate_readback", checklist=[Say(statement=scripts.MANDATE_READBACK_ES)])


@household.on_task_complete("mandate_readback")
@safe
def on_mandate_readback_done(call: guava.Call):
    call.set_task(
        "mandate_confirm",
        objective=(
            "The scripted mandate ended with a yes-or-no question. Stay silent until she "
            "answers, then capture her answer exactly. Do not repeat or paraphrase the "
            "mandate, and do not repeat the confirmation question."
        ),
        checklist=[
            Field(
                key="mandate_confirmed",
                field_type="text",
                description="Su respuesta a la confirmación, tal cual la diga",
            ),
        ],
    )


@household.on_task_complete("mandate_confirm")
@safe
def on_mandate_confirmed(call: guava.Call):
    case_id = call.get_variable("case_id")
    utterance = call.get_field("mandate_confirmed")
    mandate.confirm(case_id, utterance)
    if not db.mandate_confirmed(case_id):
        call.retry_task(
            "Her answer was not a clear yes. Ask only: '¿Está de acuerdo, sí o no?' "
            "Do not repeat or summarize the mandate."
        )
        return
    relay.open_institution_leg(case_id)
    call.hangup(
        "In one short Spanish response, tell her you have everything you need and she may "
        "hang up. Explain that ScopeLock will call her back when the institution joins. "
        "Do not repeat the mandate or the explanation."
    )


@household.on_reach_person
@safe
def on_reach_person(call: guava.Call, outcome: str):
    case_id = call.get_variable("case_id")
    inst_call = relay.institution_call(case_id)

    if outcome != "available":
        if inst_call is not None:
            inst_call.hangup(
                "Apologize that the account holder could not be reached just now, take a "
                "reference number or message if offered, and let them know she will call back."
            )
        relay.close_case(case_id)
        return

    row = db.get_case(case_id)
    relay.register_household_call(case_id, call)
    call.read_script(scripts.CONNECTED_HOLDER_ES)
    call.set_task(
        "waiting",
        objective=(
            "Remain completely silent until a new task arrives. Do not repeat the connection "
            "update, add filler, or narrate that you are waiting."
        ),
    )
    if inst_call is not None:
        institution.start_representing(inst_call, row)


@household.on_task_complete("consult")
@safe
def on_consult_answered(call: guava.Call):
    case_id = call.get_variable("case_id")
    answer_es = call.get_field("consult_answer")
    answer_en = translate.translate(answer_es, "english")
    inst_call = relay.institution_call(case_id)
    try:
        decision_id = consult.complete(case_id, answer_es, answer_en)
        withdrawn = institution.commit_holder_decision(case_id, decision_id, answer_en)
    except consult.DecisionNotPending:
        # Revoked, expired or cancelled while she was answering: her answer authorizes
        # nothing, and the representative hears that once instead of waiting in silence.
        withdrawn = True

    if inst_call is not None:
        if withdrawn:
            institution.report_authority_withdrawn(inst_call)
        else:
            institution.resume_after_consult(inst_call, answer_en)

    call.set_task(
        "waiting",
        objective=(
            "Remain completely silent until a new task arrives. Do not repeat her answer, "
            "add filler, or narrate that you are waiting."
        ),
    )


@household.on_task_complete("closing")
@safe
def on_closing_done(call: guava.Call):
    call.hangup(
        "Say one brief, warm goodbye in Spanish. Do not repeat the call summary and do not "
        "say goodbye more than once."
    )


@household.on_caller_speech
@safe
def on_caller_speech(call: guava.Call, event: CallerSpeechEvent):
    case_id = call.get_variable("case_id")
    if case_id:
        db.add_utterance(case_id, "household", "party", "spanish", event.utterance, call.id,
                          external_id=event.utterance_id)


@household.on_agent_speech
@safe
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent):
    case_id = call.get_variable("case_id")
    if case_id:
        db.add_utterance(case_id, "household", "agent", "spanish", event.utterance, call.id)
