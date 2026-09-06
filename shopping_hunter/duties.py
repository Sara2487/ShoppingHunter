"""Import-fee estimation rules, versioned and explicitly provisional.

Amazon's displayed "Import Fees Deposit" always takes precedence over this table. This estimate
is used only when Amazon shows nothing, and results carry ``fees_source="estimate"`` with
``fees_confidence="low"`` so the UI labels them. The Jordan values below are placeholders to be
sanity-checked; customs classification, exemptions and regulations vary and change.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .fx import quantize
from .models import Confidence


@dataclass(frozen=True)
class DutyRules:
    version: str
    country: str
    currency: str
    de_minimis: Decimal  # customs value below which no duty/tax is charged (local currency)
    duty_rate: Decimal  # typical ad-valorem duty; real rate depends on HS code
    tax_rate: Decimal  # sales tax / VAT applied on (value + duty)
    taxable_base_includes_shipping: bool
    confidence: Confidence


JORDAN_V1 = DutyRules(
    version="jo-v1-provisional",
    country="JO",
    currency="JOD",
    de_minimis=Decimal("100"),
    duty_rate=Decimal("0.10"),
    tax_rate=Decimal("0.16"),
    taxable_base_includes_shipping=True,
    confidence="low",
)

RULES_BY_COUNTRY: dict[str, DutyRules] = {"JO": JORDAN_V1}


@dataclass(frozen=True)
class FeesEstimate:
    duty: Decimal
    tax: Decimal
    total: Decimal
    rule_version: str
    confidence: Confidence


def estimate_import_fees(rules: DutyRules, price_local: Decimal, shipping_local: Decimal | None) -> FeesEstimate:
    """Estimate duty + tax in the rules' currency. Never returns None; callers decide when to use it."""
    base = price_local + (shipping_local or Decimal(0)) if rules.taxable_base_includes_shipping else price_local
    if base <= rules.de_minimis:
        zero = quantize(Decimal(0), rules.currency)
        return FeesEstimate(zero, zero, zero, rules.version, rules.confidence)
    duty = quantize(base * rules.duty_rate, rules.currency)
    tax = quantize((base + duty) * rules.tax_rate, rules.currency)
    return FeesEstimate(duty, tax, quantize(duty + tax, rules.currency), rules.version, rules.confidence)
