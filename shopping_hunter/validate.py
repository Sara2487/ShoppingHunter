"""Deterministic validation of the final result against the run's canonical store.

Runs after the agents are done. Structural errors (prefixed ``ranked:``, ``unconfirmed:``,
``excluded:``) cause the offending rows to be dropped; ``summary:`` errors cause the prose to be
dropped and replaced by a caveat. The UI never renders anything that failed here.
"""
from __future__ import annotations

import re
from decimal import Decimal

from .context import HuntContext
from .models import ASIN_IN_TEXT_RE, HuntResult, HuntSummary, ScoredOffer, canonical_url
from .scoring import sort_key

_URL_RE = re.compile(r"https?://\S+")
_MONEY_RE = re.compile(r"(?:(JOD|USD|AED|SAR|\$)\s?([\d,]+(?:\.\d+)?))|(?:([\d,]+(?:\.\d+)?)\s?(JOD|USD|AED|SAR))", re.I)


def _known_amounts(ctx: HuntContext) -> set[Decimal]:
    out: set[Decimal] = set()
    for s in ctx.scored.values():
        for v in (s.price_jod, s.shipping_jod, s.fees_jod, s.landed_jod, s.duty_est, s.tax_est):
            if v is not None:
                out.add(v)
        for m in (s.price, s.shipping, s.import_fees):
            if m is not None:
                out.add(m.amount)
    return out


def _amount_known(txt: str, known: set[Decimal]) -> bool:
    try:
        v = Decimal(txt.replace(",", ""))
    except Exception:  # noqa: BLE001
        return False
    for k in known:
        if abs(k - v) <= Decimal("0.5") or (v != 0 and abs(k - v) / v <= Decimal("0.01")):
            return True
    return False


def validate_scored_row(row: ScoredOffer, ctx: HuntContext, expected_tier: str) -> list[str]:
    errs: list[str] = []
    stored = ctx.scored.get(row.offer_ref)
    if stored is None:
        return [f"{expected_tier}:{row.offer_ref}: not in run store"]
    if stored.rank_tier != expected_tier:
        errs.append(f"{expected_tier}:{row.offer_ref}: stored tier is {stored.rank_tier}")
    if stored.landed_jod != row.landed_jod or stored.price_jod != row.price_jod:
        errs.append(f"{expected_tier}:{row.offer_ref}: money differs from stored scoring")
    try:
        if row.url != canonical_url(row.marketplace, row.requested_asin) and row.url != canonical_url(row.marketplace, row.resolved_asin):
            errs.append(f"{expected_tier}:{row.offer_ref}: non-canonical url")
    except ValueError:
        errs.append(f"{expected_tier}:{row.offer_ref}: invalid marketplace/asin")
    if expected_tier == "ranked":
        if row.ships_to_country != "yes":
            errs.append(f"ranked:{row.offer_ref}: shipping not confirmed")
        if row.in_stock is not True:
            errs.append(f"ranked:{row.offer_ref}: stock not confirmed")
        if row.condition not in ctx.request.allowed_conditions:
            errs.append(f"ranked:{row.offer_ref}: condition {row.condition} not allowed")
        if not row.passes_review_floor:
            errs.append(f"ranked:{row.offer_ref}: fails review floor")
        if not row.landed_cost_complete or row.landed_jod is None:
            errs.append(f"ranked:{row.offer_ref}: landed cost incomplete")
        if row.fees_source == "unavailable":
            errs.append(f"ranked:{row.offer_ref}: fees unavailable")
    return errs


def validate_hunt_result(result: HuntResult, ctx: HuntContext) -> list[str]:
    errs: list[str] = []
    seen: set[str] = set()
    for tier, rows in (("ranked", result.ranked), ("unconfirmed", result.unconfirmed)):
        for r in rows:
            if r.offer_ref in seen:
                errs.append(f"{tier}:{r.offer_ref}: duplicate")
            seen.add(r.offer_ref)
            errs += validate_scored_row(r, ctx, tier)
    if [r.offer_ref for r in result.ranked] != [r.offer_ref for r in sorted(result.ranked, key=sort_key)]:
        errs.append("ranked:order: does not match deterministic sort")
    for e in result.excluded:
        if f"{e.marketplace}:{e.asin}" not in ctx.candidates:
            errs.append(f"excluded:{e.marketplace}:{e.asin}: unknown candidate")
    if result.summary:
        errs += validate_summary_text(result.summary, ctx)
    return errs


def validate_summary_text(text: str, ctx: HuntContext) -> list[str]:
    errs: list[str] = []
    known_asins = {c.asin for c in ctx.candidates.values()} | {o.resolved_asin for o in ctx.offers.values()}
    for asin in ASIN_IN_TEXT_RE.findall(text):
        if asin not in known_asins:
            errs.append(f"summary: unknown ASIN {asin}")
    known_urls = {o.url for o in ctx.offers.values()}
    for url in _URL_RE.findall(text):
        if url.rstrip(".,;)") not in known_urls:
            errs.append(f"summary: unknown URL {url[:60]}")
    known = _known_amounts(ctx)
    for m in _MONEY_RE.finditer(text):
        amt = m.group(2) or m.group(3)
        if amt and not _amount_known(amt, known):
            errs.append(f"summary: unknown amount {amt}")
    return errs


def validate_summary(summary: HuntSummary, ctx: HuntContext) -> list[str]:
    errs = validate_summary_text(summary.summary, ctx)
    for ref in summary.mentioned_offer_refs + ([summary.top_offer_ref] if summary.top_offer_ref else []):
        if ref not in ctx.scored or ctx.scored[ref].rank_tier == "excluded":
            errs.append(f"summary: references non-ranked offer {ref}")
    return errs
