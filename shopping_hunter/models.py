"""Shared data model.

Two families live here:

* LLM-facing models (QueryPlan, MatchDecisions, DeepPassReport, HuntSummary) are used as
  ``output_type`` for agents. They must be strict-JSON-schema compatible: no dicts with
  arbitrary keys, no Decimal, every field explicit. They never carry prices.
* Internal models (Candidate, Offer, ScoredOffer, HuntResult ...) are the canonical store.
  Money is always ``Decimal``. The LLM never produces these; it only references them by ref.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- constants

Marketplace = Literal["amazon.com", "amazon.ae", "amazon.sa"]
MARKETPLACES: tuple[str, ...] = ("amazon.com", "amazon.ae", "amazon.sa")
MARKETPLACE_CURRENCY: dict[str, str] = {"amazon.com": "USD", "amazon.ae": "AED", "amazon.sa": "SAR"}

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
# Loose pattern used to spot ASIN-like tokens inside free text (summary provenance check).
ASIN_IN_TEXT_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")

Confidence = Literal["high", "medium", "low"]
RejectionCode = Literal[
    "accessory", "bundle", "wrong_model", "wrong_generation", "knockoff", "renewed", "ambiguous", "other"
]
Condition = Literal["new", "renewed", "used", "unknown"]
ShipsTo = Literal["yes", "no", "unknown"]
OfferStatus = Literal[
    "ok", "not_found", "not_available", "does_not_ship", "parse_error", "needs_human",
    "timeout", "rate_limited", "login_required", "location_not_set",
]
FeesSource = Literal["amazon", "amazon_derived", "estimate", "unavailable"]
# amazon         - Amazon displayed the import fee for this exact offer
# amazon_derived - Amazon displayed it for the buy box of the same listing; scaled to this offer's price
# estimate       - provisional per-country rules table
# unavailable    - no price, nothing to estimate from
RankTier = Literal["ranked", "unconfirmed", "excluded"]
RunStatus = Literal["completed", "partial", "failed", "cancelled"]
Depth = Literal["ordinary", "thorough"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def candidate_ref(marketplace: str, asin: str) -> str:
    return f"{marketplace}:{asin}"


def parse_candidate_ref(ref: str) -> tuple[str, str]:
    marketplace, _, asin = ref.partition(":")
    if marketplace not in MARKETPLACES or not ASIN_RE.match(asin):
        raise ValueError(f"invalid candidate ref: {ref!r}")
    return marketplace, asin


def canonical_url(marketplace: str, asin: str) -> str:
    if marketplace not in MARKETPLACES or not ASIN_RE.match(asin):
        raise ValueError(f"cannot build url for {marketplace}/{asin}")
    return f"https://www.{marketplace}/dp/{asin}"


# --------------------------------------------------------------------------- money

class Money(BaseModel):
    model_config = ConfigDict(frozen=True)
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


# --------------------------------------------------------------------------- request

class HuntRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    marketplaces: list[Marketplace] = Field(default_factory=lambda: list(MARKETPLACES))
    min_rating: float = Field(default=4.0, ge=0, le=5)
    min_ratings_count: int = Field(default=50, ge=0, le=1_000_000)
    budget_jod: Decimal | None = Field(default=None, ge=0)
    depth: Depth = "ordinary"
    allowed_conditions: list[Condition] = Field(default_factory=lambda: ["new"])
    include_other_sellers: bool = False

    @field_validator("marketplaces")
    @classmethod
    def _nonempty_unique(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for m in v:
            if m not in out:
                out.append(m)
        if not out:
            raise ValueError("at least one marketplace required")
        return out  # type: ignore[return-value]


# --------------------------------------------------------------------------- candidates

class Candidate(BaseModel):
    """One search-result card. Keyed by (marketplace, asin) via ``ref``."""
    ref: str
    marketplace: Marketplace
    asin: str
    title: str = Field(max_length=300)
    listed_price: Money | None = None
    rating: float | None = None
    ratings_count: int | None = None
    sponsored: bool = False
    parent_asin: str | None = None
    variant: dict[str, str] = Field(default_factory=dict)
    url: str

    @classmethod
    def build(cls, marketplace: str, asin: str, **kw) -> "Candidate":
        return cls(
            ref=candidate_ref(marketplace, asin),
            marketplace=marketplace,  # type: ignore[arg-type]
            asin=asin,
            url=canonical_url(marketplace, asin),
            **kw,
        )

    def compact(self) -> dict:
        """The only representation the LLM ever sees."""
        return {
            "ref": self.ref,
            "title": self.title,
            "price": str(self.listed_price) if self.listed_price else None,
            "rating": self.rating,
            "ratings_count": self.ratings_count,
            "sponsored": self.sponsored,
        }


# --------------------------------------------------------------------------- LLM-facing (strict)

class MarketplaceQuery(BaseModel):
    marketplace: Marketplace
    query: str


class QueryPlan(BaseModel):
    canonical_brand: str | None
    canonical_model: str | None
    queries: list[MarketplaceQuery]


class VariantAttr(BaseModel):
    name: str
    value: str


class MatchDecision(BaseModel):
    candidate_ref: str
    matches: bool
    confidence: Confidence
    canonical_brand: str | None
    canonical_model: str | None
    variant: list[VariantAttr]
    reasons: list[str]
    rejection_code: RejectionCode | None


class MatchDecisions(BaseModel):
    decisions: list[MatchDecision]


class DeepPassReport(BaseModel):
    performed: bool
    action: Literal["none", "alternate_query", "other_sellers"]
    new_decisions: list[MatchDecision]
    notes: str


class HuntSummary(BaseModel):
    summary: str
    top_offer_ref: str | None
    mentioned_offer_refs: list[str]
    caveats: list[str]


# --------------------------------------------------------------------------- offers

class OfferProvenance(BaseModel):
    source: Literal["network", "cache", "fixture"]
    fetched_at: datetime
    admitted_at: datetime
    run_id: str
    parser_version: str


class Diagnostic(BaseModel):
    field: str | None = None
    code: str
    detail: str | None = None


class Offer(BaseModel):
    offer_ref: str  # "<marketplace>:<asin>:<offer_id|buybox>"
    marketplace: Marketplace
    requested_asin: str
    resolved_asin: str
    parent_asin: str | None = None
    offer_id: str | None = None
    seller: str | None = None
    condition: Condition = "unknown"
    in_stock: bool | None = None
    ships_to_country: ShipsTo = "unknown"
    shipping_evidence: str | None = None
    price: Money | None = None
    shipping: Money | None = None
    import_fees: Money | None = None
    fees_included_in_shipping: bool = False  # Amazon showed one combined "Shipping & Import Fees" figure
    delivery_days: int | None = None
    is_prime: bool = False
    title: str = Field(default="", max_length=300)
    rating: float | None = None
    ratings_count: int | None = None
    promotions_unverified: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)
    provenance: OfferProvenance
    url: str

    @property
    def candidate_ref(self) -> str:
        return candidate_ref(self.marketplace, self.requested_asin)

    def compact(self) -> dict:
        return {
            "offer_ref": self.offer_ref,
            "title": self.title,
            "seller": self.seller,
            "condition": self.condition,
            "in_stock": self.in_stock,
            "ships_to_country": self.ships_to_country,
            "price": str(self.price) if self.price else None,
            "shipping": str(self.shipping) if self.shipping else None,
            "import_fees": str(self.import_fees) if self.import_fees else None,
            "delivery_days": self.delivery_days,
            "rating": self.rating,
            "ratings_count": self.ratings_count,
        }


def offer_ref(marketplace: str, asin: str, offer_id: str | None) -> str:
    """Stable, short ref. Long Amazon offer-listing ids are hashed; the full id stays on the Offer."""
    if not offer_id:
        return f"{marketplace}:{asin}:buybox"
    if len(offer_id) > 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", offer_id):
        import hashlib
        offer_id = "h" + hashlib.sha1(offer_id.encode()).hexdigest()[:10]
    return f"{marketplace}:{asin}:{offer_id}"


class OfferFetchResult(BaseModel):
    candidate_ref: str
    status: OfferStatus
    offers: list[Offer] = Field(default_factory=list)
    diagnostic: Diagnostic | None = None


# --------------------------------------------------------------------------- scoring

class ScoredOffer(Offer):
    price_jod: Decimal | None = None
    shipping_jod: Decimal | None = None
    fees_jod: Decimal | None = None
    fees_source: FeesSource = "unavailable"
    fees_confidence: Confidence = "low"
    duty_est: Decimal | None = None
    tax_est: Decimal | None = None
    rule_version: str | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    fx_at: datetime | None = None
    landed_jod: Decimal | None = None
    landed_cost_complete: bool = False
    passes_review_floor: bool = False
    match_confidence: Confidence = "low"
    group_key: str = ""
    rank_tier: RankTier = "excluded"
    exclusion_reason: str | None = None
    offer_token: str | None = None

    def compact(self) -> dict:  # type: ignore[override]
        d = super().compact()
        d.update({
            "landed_jod": str(self.landed_jod) if self.landed_jod is not None else None,
            "landed_cost_complete": self.landed_cost_complete,
            "fees_source": self.fees_source,
            "rank_tier": self.rank_tier,
        })
        return d


class ExcludedCandidate(BaseModel):
    marketplace: Marketplace
    asin: str
    title: str | None = None
    reason_code: str
    reason: str


class Coverage(BaseModel):
    marketplaces_attempted: list[str] = Field(default_factory=list)
    marketplaces_completed: list[str] = Field(default_factory=list)
    queries_used: list[MarketplaceQuery] = Field(default_factory=list)
    pages_scanned: int = 0
    candidates_before_dedup: int = 0
    candidates_after_dedup: int = 0
    truncated_by_cap: int = 0
    accepted_not_fetched: int = 0
    offers_inspected: int = 0
    offers_skipped: dict[str, int] = Field(default_factory=dict)


class HuntResult(BaseModel):
    run_id: str
    status: RunStatus
    status_reason: str | None = None
    query: str
    ranked: list[ScoredOffer] = Field(default_factory=list)
    unconfirmed: list[ScoredOffer] = Field(default_factory=list)
    excluded: list[ExcludedCandidate] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    summary: str | None = None
    caveats: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=now_utc)
    finished_at: datetime | None = None
