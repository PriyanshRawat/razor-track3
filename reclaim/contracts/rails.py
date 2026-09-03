"""Rail mechanics.

What lives here versus in policy configuration
----------------------------------------------
CONTRACT DECISION (JC-16). Two different kinds of number appear in
HACKATHON_PLAN.md and they must not share an owner:

* **Rail mechanics** -- facts about the rail, documented by the PSP or the
  regulator: an India card e-mandate debit is delayed 26 hours; a pre-debit
  notification must precede it by at least 24 hours; UPI Autopay cannot carry more
  than Rs 15,000 in a single recurring transaction. These are *floors we do not
  get to choose*. They live in this module, frozen, each with a citation.
* **Policy thresholds** -- numbers *we* choose: the Rs 2,000 T0 auto-reschedule
  ceiling, the AFA threshold (regulatory in origin but explicitly config in §11.2
  so that an RBI change is a one-line edit), contact caps, quiet hours. These live
  in ``reclaim.contracts.policy_format.PolicyThresholds`` and are loaded from YAML.

The two compose in exactly one direction: configuration may add a **safety
margin** on top of a rail floor and may never lower it
(``effective_pre_debit_notification_lead_hours``). A misconfigured YAML file can
therefore make RECLAIM more cautious than the regulation, never less. That is the
whole reason the numbers are split across two modules instead of one.

Every value here is a *default that a golden test pins*. Where the plan's sources
do not settle a question, the field carries a ``VERIFY`` note rather than a
confident number -- see Appendix B of HACKATHON_PLAN.md.
"""

from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reclaim.contracts.enums import Rail
from reclaim.contracts.money import Money

__all__ = [
    "RAIL_SPECS",
    "RailSpec",
    "effective_pre_debit_notification_lead_hours",
    "rail_permits_debit_request",
    "rail_requires_pre_debit_notification",
    "rail_spec",
]


class RailSpec(BaseModel):
    """Mechanical properties of one payment rail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rail: Rail
    is_recurring: bool
    is_mandate_backed: bool
    permits_debit_request: bool = Field(
        description="Whether a pull can be submitted on this rail at all. False "
        "only where money can exclusively be pushed to us (bank transfer), which "
        "makes ``schedule_debit`` structurally invalid rather than merely denied."
    )
    requires_pre_debit_notification: bool
    pre_debit_notification_min_lead_hours: int = Field(
        ge=0,
        description="Regulatory floor. Configuration may add to it, never subtract.",
    )
    charge_lead_time_hours: int = Field(
        ge=0,
        description="How far ahead of the intended settlement the debit must be "
        "submitted. 26h for India cards per Stripe's documented delay -- this is "
        "the horizon the hazard model must predict at (§2).",
    )
    max_per_transaction: Money | None = Field(
        default=None,
        description="Rail-level ceiling, independent of the mandate cap. Rs 15,000 "
        "for UPI Autopay recurring transactions.",
    )
    supports_afa_step_up: bool = Field(
        description="Whether a per-debit additional-factor authentication journey "
        "exists on this rail."
    )
    customer_present_only: bool
    counts_toward_network_retry_limit: bool = Field(
        description="Card-network excessive-retry limits apply only to card rails."
    )
    citation: str
    verify_before_demo: str = Field(
        default="",
        description="Non-empty when the plan's sources did not settle this rail's "
        "values. Appendix B of HACKATHON_PLAN.md must clear it.",
    )

    @model_validator(mode="after")
    def _notification_implies_lead(self) -> "RailSpec":
        if self.requires_pre_debit_notification and self.pre_debit_notification_min_lead_hours <= 0:
            raise ValueError(
                f"{self.rail}: a required pre-debit notification needs a positive "
                "minimum lead time, otherwise invariant #4 is unenforceable"
            )
        if self.is_recurring and not self.is_mandate_backed:
            raise ValueError(
                f"{self.rail}: a recurring rail without a mandate has nothing to "
                "authorise the debit"
            )
        return self


_STRIPE_INDIA = "Stripe Docs, India recurring payments (see HACKATHON_PLAN.md §11.2, Appendix A)."

RAIL_SPECS: Mapping[Rail, RailSpec] = {
    Rail.CARD_EMANDATE: RailSpec(
        rail=Rail.CARD_EMANDATE,
        is_recurring=True,
        is_mandate_backed=True,
        permits_debit_request=True,
        requires_pre_debit_notification=True,
        pre_debit_notification_min_lead_hours=24,
        charge_lead_time_hours=26,
        max_per_transaction=None,  # the mandate cap governs, not the rail
        supports_afa_step_up=True,
        customer_present_only=False,
        counts_toward_network_retry_limit=True,
        citation=_STRIPE_INDIA + " 'Stripe waits 26 hours before charging'; "
        "pre-debit notification >=24h with the exact amount and an opt-out.",
    ),
    Rail.UPI_AUTOPAY: RailSpec(
        rail=Rail.UPI_AUTOPAY,
        is_recurring=True,
        is_mandate_backed=True,
        permits_debit_request=True,
        requires_pre_debit_notification=True,
        pre_debit_notification_min_lead_hours=24,
        charge_lead_time_hours=24,
        max_per_transaction=Money.from_rupees(15000),
        supports_afa_step_up=True,
        customer_present_only=False,
        counts_toward_network_retry_limit=False,
        citation=_STRIPE_INDIA + " UPI Autopay recurring transactions cannot exceed "
        "Rs 15,000.",
        verify_before_demo="The 26-hour delay is documented for India *cards*. We "
        "assume the UPI Autopay submission horizon equals the 24h notification "
        "floor; confirm before asserting it to a judge.",
    ),
    Rail.ENACH: RailSpec(
        rail=Rail.ENACH,
        is_recurring=True,
        is_mandate_backed=True,
        permits_debit_request=True,
        requires_pre_debit_notification=True,
        pre_debit_notification_min_lead_hours=24,
        charge_lead_time_hours=24,
        max_per_transaction=None,
        supports_afa_step_up=False,
        customer_present_only=False,
        counts_toward_network_retry_limit=False,
        citation="RBI e-mandate framework, as summarised in HACKATHON_PLAN.md §14.1.",
        verify_before_demo="Whether the per-debit AFA step-up above the threshold "
        "applies to NACH mandates as it does to card and UPI e-mandates is NOT "
        "settled by the plan's sources. Modelled as False (no per-debit step-up "
        "journey exists), which makes a dead e-NACH mandate a re-registration case.",
    ),
    Rail.CARD_ONE_TIME: RailSpec(
        rail=Rail.CARD_ONE_TIME,
        is_recurring=False,
        is_mandate_backed=False,
        permits_debit_request=True,
        requires_pre_debit_notification=False,
        pre_debit_notification_min_lead_hours=0,
        charge_lead_time_hours=0,
        max_per_transaction=None,
        supports_afa_step_up=True,
        customer_present_only=True,
        counts_toward_network_retry_limit=True,
        citation="Customer-present card payment; 3DS at the point of payment.",
    ),
    Rail.UPI_COLLECT: RailSpec(
        rail=Rail.UPI_COLLECT,
        is_recurring=False,
        is_mandate_backed=False,
        permits_debit_request=True,
        requires_pre_debit_notification=False,
        pre_debit_notification_min_lead_hours=0,
        charge_lead_time_hours=0,
        max_per_transaction=None,
        supports_afa_step_up=True,
        customer_present_only=True,
        counts_toward_network_retry_limit=False,
        citation="UPI collect request; the payer authorises in their PSP app, so it "
        "is a customer journey rather than a merchant-initiated debit.",
    ),
    Rail.BANK_TRANSFER: RailSpec(
        rail=Rail.BANK_TRANSFER,
        is_recurring=False,
        is_mandate_backed=False,
        permits_debit_request=False,
        requires_pre_debit_notification=False,
        pre_debit_notification_min_lead_hours=0,
        charge_lead_time_hours=0,
        max_per_transaction=None,
        supports_afa_step_up=False,
        customer_present_only=False,
        counts_toward_network_retry_limit=False,
        citation="NEFT/RTGS/IMPS push. B2B settlement arrives here; we cannot pull "
        "it, which is why the B2B leg's only levers are the invoice and the ask.",
    ),
}

_missing_rails = set(Rail) - set(RAIL_SPECS)
if _missing_rails:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "RAIL_SPECS is missing entries for: "
        + ", ".join(sorted(r.value for r in _missing_rails))
    )


def rail_spec(rail: Rail) -> RailSpec:
    return RAIL_SPECS[rail]


def rail_requires_pre_debit_notification(rail: Rail) -> bool:
    return RAIL_SPECS[rail].requires_pre_debit_notification


def rail_permits_debit_request(rail: Rail) -> bool:
    """False only for push-only rails, where a debit cannot be expressed."""
    return RAIL_SPECS[rail].permits_debit_request


def effective_pre_debit_notification_lead_hours(
    rail: Rail, safety_margin_hours: int = 0
) -> int:
    """Regulatory floor plus our configured margin.

    The margin is *added*, never substituted, so no configuration change can make
    RECLAIM notify later than the regulation requires (JC-16).
    """
    if safety_margin_hours < 0:
        raise ValueError("safety_margin_hours must be >= 0; config may not weaken a floor")
    return RAIL_SPECS[rail].pre_debit_notification_min_lead_hours + safety_margin_hours
