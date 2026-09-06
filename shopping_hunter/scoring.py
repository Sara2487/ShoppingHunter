"""Deterministic scoring and ranking. Pure functions; no I/O, no LLM.

Tiering:
  excluded     - not the requested product, disallowed condition, out of stock, does not ship,
                 fails the review floor, or over budget. Carries a reason code.
  unconfirmed  - matched, but shipping eligibility or landed cost could not be established.
                 Never silently ranked; never zero-filled.
  ranked       - everything known and passing; sorted by ``sort_key``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .duties import DutyRules, estimate_import_fees
from .fx import FxRates, quantize
from .models import HuntRequest, MatchDecision, Offer, ScoredOffer
from .normalize import group_key as make_group_key

_UNKNOWN_DELIVERY = 10_000


def sort_key(o: ScoredOffer) -> tuple:
    return (
        0 if o.landed_cost_complete else 1,
        o.landed_jod if o.landed_jod is not None else Decimal("1e12"),
        o.delivery_days if o.delivery_days is not None else _UNKNOWN_DELIVERY,
        -(o.ratings_count or 0),
        -(o.rating or 0.0),
        o.offer_ref,
    )


def _excluded(s: ScoredOffer, reason: str) -> ScoredOffer:
    s.rank_tier = "excluded"
    s.exclusion_reason = reason
    return s


def score_offer(
    offer: Offer,
    decision: MatchDecision | None,
    request: HuntRequest,
    fx: FxRates,
    rules: DutyRules,
    target_ccy: str,
    derived_fee_rate: Decimal | None = None,
) -> ScoredOffer:
    s = ScoredOffer(**offer.model_dump())

    # -- match ---------------------------------------------------------------
    if decision is None or not decision.matches:
        return _excluded(s, decision.rejection_code if decision and decision.rejection_code else "no_match")
    s.match_confidence = decision.confidence
    s.group_key = make_group_key(
        decision.canonical_brand, decision.canonical_model, {v.name: v.value for v in decision.variant}
    )
    if decision.confidence == "low":
        return _excluded(s, "low_match_confidence")

    # -- money (computed before tiering so unconfirmed rows still show what we know) ------
    if s.price is not None:
        s.price_jod, q = fx.convert(s.price.amount, s.price.currency, target_ccy)
        s.fx_rate, s.fx_source, s.fx_at = q.rate, q.source, q.at
    if s.shipping is not None:
        s.shipping_jod, _ = fx.convert(s.shipping.amount, s.shipping.currency, target_ccy)
    if s.import_fees is not None:
        s.fees_jod, _ = fx.convert(s.import_fees.amount, s.import_fees.currency, target_ccy)
        s.fees_source, s.fees_confidence = "amazon", "high"
    elif derived_fee_rate is not None and s.price is not None:
        fee = quantize(s.price.amount * derived_fee_rate, s.price.currency)
        s.fees_jod, _ = fx.convert(fee, s.price.currency, target_ccy)
        s.fees_source, s.fees_confidence = "amazon_derived", "medium"
    elif s.price_jod is not None:
        est = estimate_import_fees(rules, s.price_jod, s.shipping_jod)
        s.fees_jod, s.duty_est, s.tax_est = est.total, est.duty, est.tax
        s.rule_version, s.fees_source, s.fees_confidence = est.rule_version, "estimate", est.confidence
    else:
        s.fees_source, s.fees_confidence = "unavailable", "low"

    if s.price_jod is not None and s.shipping_jod is not None and s.fees_jod is not None:
        s.landed_jod = quantize(s.price_jod + s.shipping_jod + s.fees_jod, target_ccy)
        s.landed_cost_complete = True

    # -- review floor (product-level; may cover the variation family) -----------
    s.passes_review_floor = (
        s.rating is not None and s.ratings_count is not None
        and s.rating >= request.min_rating and s.ratings_count >= request.min_ratings_count
    )

    # -- hard exclusions -----------------------------------------------------
    if s.condition not in request.allowed_conditions:
        return _excluded(s, "condition_not_allowed")
    if s.in_stock is False:
        return _excluded(s, "out_of_stock")
    if s.ships_to_country == "no":
        return _excluded(s, "does_not_ship")
    if not s.passes_review_floor:
        return _excluded(s, "review_floor")
    if request.budget_jod is not None and s.landed_jod is not None and s.landed_jod > request.budget_jod:
        return _excluded(s, "over_budget")

    # -- unconfirmed -----------------------------------------------------------
    if s.ships_to_country == "unknown" or s.in_stock is None or not s.landed_cost_complete:
        s.rank_tier = "unconfirmed"
        return s

    s.rank_tier = "ranked"
    return s


def score_offers(
    offers: Iterable[Offer],
    decisions: dict[str, MatchDecision],
    request: HuntRequest,
    fx: FxRates,
    rules: DutyRules,
    target_ccy: str = "JOD",
) -> list[ScoredOffer]:
    offers = list(offers)
    rates = derived_fee_rates(offers)
    scored = [
        score_offer(o, decisions.get(o.candidate_ref), request, fx, rules, target_ccy,
                    derived_fee_rate=rates.get((o.marketplace, o.resolved_asin)))
        for o in offers
    ]
    return sorted(scored, key=sort_key)


def derived_fee_rates(offers: list[Offer]) -> dict[tuple[str, str], Decimal]:
    """Import-fee rate (fee / price) per listing, taken from an offer where Amazon displayed the
    fee separately. Other sellers of the same listing inherit it, scaled to their own price."""
    rates: dict[tuple[str, str], Decimal] = {}
    for o in offers:
        if (o.import_fees is not None and o.price is not None and o.price.amount > 0
                and not o.fees_included_in_shipping and o.import_fees.currency == o.price.currency):
            key = (o.marketplace, o.resolved_asin)
            if key not in rates or o.offer_id is None:  # prefer the buy box
                rates[key] = o.import_fees.amount / o.price.amount
    return rates


def split_tiers(scored: list[ScoredOffer]) -> tuple[list[ScoredOffer], list[ScoredOffer], list[ScoredOffer]]:
    ranked = sorted((s for s in scored if s.rank_tier == "ranked"), key=sort_key)
    unconfirmed = sorted((s for s in scored if s.rank_tier == "unconfirmed"), key=sort_key)
    excluded = [s for s in scored if s.rank_tier == "excluded"]
    return ranked, unconfirmed, excluded
