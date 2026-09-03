"""The template registry the content rules read.

§14.1's Content row and JC-17 between them say a message is a *registered
template plus named slots*, never prose. That only means anything if there is a
registry, and if an unregistered template id is a denial rather than a shrug --
so ``template_for`` returns ``None`` for an intent/language pair nobody has
registered, and ``facts.py`` turns a missing registration into
``TEMPLATE_IS_DLT_REGISTERED = False``.

This registry is small and real, not a stub: four templates covering the intents
the Phase 1 router emits, each with the slot names its copy contains and the
languages it is registered for. What it does *not* have is the copy itself --
rendering is the executor's job, and a template body in here would be the first
place free text could re-enter the system.

``BANNED_PHRASES`` is likewise minimal and genuinely applied: the check runs over
the slot *values*, which is the only place text from outside the registry can
reach a customer. While every slot value is a formatted amount or a date the
check finds nothing, and that is the correct state of affairs -- it becomes
load-bearing the moment the LLM personalisation path lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping

from reclaim.contracts.enums import Language, MessageIntent

__all__ = [
    "BANNED_PHRASES",
    "TEMPLATE_REGISTRY",
    "TemplateSpec",
    "contains_banned_phrase",
    "template_for",
]


@dataclass(frozen=True)
class TemplateSpec:
    """One registered template.

    ``dlt_registered`` is per template because TRAI's DLT registration is per
    template -- that is why ``SendMessage`` forbids a free-text slot on SMS.
    """

    template_id: str
    intent: MessageIntent
    languages: frozenset[Language]
    dlt_registered: bool
    slot_names: frozenset[str]


def _spec(
    template_id: str,
    intent: MessageIntent,
    *,
    slots: tuple[str, ...],
    languages: tuple[Language, ...] = (Language.EN_IN,),
    dlt_registered: bool = True,
) -> TemplateSpec:
    return TemplateSpec(
        template_id=template_id,
        intent=intent,
        languages=frozenset(languages),
        dlt_registered=dlt_registered,
        slot_names=frozenset(slots),
    )


TEMPLATE_REGISTRY: Mapping[str, TemplateSpec] = {
    spec.template_id: spec
    for spec in (
        _spec(
            "tpl_credential_update_v1",
            MessageIntent.CREDENTIAL_UPDATE_REQUEST,
            slots=("amount_due", "merchant_name", "update_link"),
        ),
        _spec(
            "tpl_afa_completion_v1",
            MessageIntent.AFA_COMPLETION_REQUEST,
            slots=("amount_due", "merchant_name", "afa_link"),
        ),
        _spec(
            "tpl_payment_reminder_v1",
            MessageIntent.PAYMENT_REMINDER,
            slots=("amount_due", "due_date", "merchant_name"),
        ),
        _spec(
            "tpl_mandate_reauth_v1",
            MessageIntent.MANDATE_REAUTH_REQUEST,
            slots=("amount_due", "merchant_name", "reauth_link"),
        ),
    )
}

#: Lower-cased substrings that must not appear in any slot value. §14.1's
#: Content row names the classes: threats, legal claims we cannot make, third
#: party disclosure, implied credit-bureau consequence. Substring matching is
#: crude and deliberately so -- it over-blocks rather than under-blocks, and a
#: real implementation replaces it with the per-language list §18.2 item 21
#: describes, not with something cleverer in English.
BANNED_PHRASES: Final[tuple[str, ...]] = (
    "cibil",
    "credit bureau",
    "credit score",
    "legal action",
    "we will sue",
    "court",
    "police",
    "arrest",
    "your employer",
    "your family",
    "blacklist",
    "defaulter",
)


def template_for(intent: MessageIntent, language: Language) -> TemplateSpec | None:
    """The registered template for this intent in this language, or ``None``.

    ``None`` is the fail-closed answer: the caller must treat "nobody registered
    a template for this" as a reason not to send, never as a reason to compose
    something.
    """
    for spec in TEMPLATE_REGISTRY.values():
        if spec.intent is intent and language in spec.languages:
            return spec
    return None


def contains_banned_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in BANNED_PHRASES)
