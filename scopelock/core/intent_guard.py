"""Deterministic bilingual deny-override for forbidden commitments.

The voice leg names an action with an LLM intent classifier. A paraphrase or a Spanish
phrasing can land on a harmless-looking verb ("request_written") while the words actually
ask the agent to commit the holder to a payment, a settlement, a coverage change or an SSN
disclosure. This screen reads the triggering text itself and, when it finds one of those
commitments, names the forbidden verb so the policy can refuse regardless of the label.

It only ever REMOVES authority: a match turns a decision into a refusal, a miss leaves the
classifier's verb to the normal mandate check. Patterns target commitments, not mentions
("why was the payment denied" stays an ask_reason). Over- and under-matching are measured
on a frozen bilingual fixture, never assumed.
"""
from __future__ import annotations

import re
import unicodedata

# Order matters only for which refusal is spoken when several match.
_PATTERNS: dict[str, tuple[str, ...]] = {
    "disclose_ssn": (
        r"social security",
        r"\bssn\b",
        r"last (?:four|4)(?: digits)?",
        r"\btax id\b",
        r"seguro social",
        r"numero de seguro",
        r"ultimos (?:cuatro|4)(?: digitos)?",
    ),
    "agree_payment": (
        r"payment plan",
        r"installment",
        r"\bagree to pay\b",
        r"\b(?:make|set up|schedule|take) (?:a |the |her |your )?payments?\b",
        r"\bpay (?:it|this|that|the balance|the bill|the amount|the rest|now|today|off)\b",
        r"\b(?:charge|put it on|bill) (?:her|the|your|a) (?:card|account)\b",
        r"\bdown payment\b",
        r"\bautopay\b",
        r"plan de pagos?",
        r"\ben cuotas\b",
        r"\ba plazos\b",
        r"\b(?:hacer|programar|acepta|aceptar|aceptaria) (?:un |el |los )?pagos?\b",
        r"\bpagar (?:la|el|esto|eso|ahora|hoy|el saldo|la cuenta|la factura|lo que)\b",
        r"\bacepta pagar\b",
        r"\bcargar (?:a )?(?:su|la) tarjeta\b",
        r"\bpago inicial\b",
    ),
    "accept_settlement": (
        r"\bsettle(?:ment)?\b",
        r"\baccept (?:the|this|our|a) (?:offer|arrangement|deal|agreement|compromise)\b",
        r"\breduced amount\b",
        r"\bwaive (?:her|the|your) (?:right|claim|appeal)\b",
        r"\bclose (?:out )?the (?:claim|account|dispute) for\b",
        r"\bliquidacion\b",
        r"\barreglo\b",
        r"\baceptar? (?:la|el|esta|este|nuestra|nuestro) (?:oferta|acuerdo|arreglo|propuesta)\b",
        r"\bmonto reducido\b",
        r"\brenunci(?:ar|a) (?:a )?(?:su|la) (?:apelacion|reclamacion|derecho)\b",
    ),
    "change_coverage": (
        r"\b(?:change|switch|downgrade|upgrade|cancel|drop|modify) (?:her|the|your|his) "
        r"(?:coverage|plan|policy|benefits)\b",
        r"\b(?:a )?(?:different|new|cheaper) plan\b",
        r"\b(?:cambiar|cancelar|modificar|bajar) (?:su|la|el|de) (?:cobertura|plan|poliza)\b",
        r"\botro plan\b",
        r"\bplan (?:diferente|nuevo|mas barato)\b",
    ),
}

# A bounded informational question does not itself disclose an identifier. The
# full-string grammar deliberately rejects appended instructions or extra values.
_SSN_REASON_QUESTION = re.compile(
    r"(?:why (?:is|was) (?:a |the )?(?:social security number|ssn) (?:required|needed)"
    r"(?: for (?:this|the) (?:claim|request))?"
    r"|por que se (?:requiere|necesita) (?:un |el )?numero de seguro social)"
)


_COMPILED = {
    verb: tuple(re.compile(pattern) for pattern in patterns)
    for verb, patterns in _PATTERNS.items()
}


def normalize(text: str) -> str:
    ascii_text = "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text))


def screen(text: str | None) -> str | None:
    """Return the forbidden verb a trigger commits to, or None."""
    if not text:
        return None
    normalized = normalize(text)
    for verb, patterns in _COMPILED.items():
        if verb == "disclose_ssn" and _SSN_REASON_QUESTION.fullmatch(normalized):
            continue
        if any(pattern.search(normalized) for pattern in patterns):
            return verb
    return None
