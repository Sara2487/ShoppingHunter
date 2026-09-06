"""Fixture-backed adapter for tests and ``--fake`` runs. No network, no browser.

The catalogue deliberately contains the traps a strict-model hunter must handle: prior
generation, accessory, renewed, bundle, knock-off, regional variant, out of stock, does-not-ship,
unknown shipping, missing import fees (estimate path), and a third-party seller offer.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from rapidfuzz import fuzz

from ..models import (
    Candidate, Money, Offer, OfferFetchResult, OfferProvenance, now_utc, offer_ref,
)
from ..normalize import normalize_title
from .base import CartResult, LoginStatus

PARSER_VERSION = "fixture-1"


def _m(amount: str, ccy: str) -> Money:
    return Money(amount=Decimal(amount), currency=ccy)


# (marketplace, asin, title, price, rating, count, sponsored, parent, variant)
_CATALOG: list[tuple] = [
    ("amazon.com", "B09HM94VDS", "Logitech MX Master 3S - Wireless Performance Mouse, Ergo, 8K DPI, Quiet Clicks - Graphite", "99.99", 4.6, 31240, False, "B0PARENT01", {"color": "Graphite"}),
    ("amazon.com", "B09HMKFDXC", "Logitech MX Master 3S Wireless Mouse - Pale Gray", "89.99", 4.6, 31240, False, "B0PARENT01", {"color": "Pale Gray"}),
    ("amazon.com", "B0MAC3SFOR", "Logitech MX Master 3S for Mac Wireless Bluetooth Mouse - Space Grey", "95.00", 4.5, 4100, False, None, {"edition": "for Mac", "color": "Space Grey"}),
    ("amazon.com", "B07S395RWD", "Logitech MX Master 3 Advanced Wireless Mouse - Graphite (previous generation)", "79.00", 4.6, 58000, False, None, {}),
    ("amazon.com", "B0CASE0001", "Hard Travel Case for Logitech MX Master 3S / 3 Wireless Mouse - Black", "14.99", 4.7, 2100, True, None, {}),
    ("amazon.com", "B0RENEW001", "Logitech MX Master 3S Wireless Mouse, Graphite (Renewed)", "69.99", 4.3, 800, False, None, {}),
    ("amazon.com", "B0BUNDLE01", "Logitech MX Master 3S Mouse Bundle with Desk Mat and Cleaning Cloth", "119.99", 4.4, 150, False, None, {}),
    ("amazon.ae", "B09HM94VDS", "Logitech MX Master 3S Wireless Mouse, Graphite", "349.00", 4.6, 12400, False, "B0PARENT01", {"color": "Graphite"}),
    ("amazon.ae", "B0KNOCK001", "Ergonomic Wireless Mouse MX Master 3S Style 8000 DPI Rechargeable - Black", "59.00", 3.9, 21, False, None, {}),
    ("amazon.sa", "B09HM94VDS", "Logitech MX Master 3S Performance Wireless Mouse - Graphite", "379.00", 4.6, 6800, False, "B0PARENT01", {"color": "Graphite"}),
    ("amazon.sa", "B0SAOUT001", "Logitech MX Master 3S Wireless Mouse - Black", "350.00", 4.5, 900, False, None, {"color": "Black"}),
    ("amazon.com", "B09XS7JWHH", "Sony WH-1000XM5 Wireless Noise Canceling Headphones - Black", "328.00", 4.5, 22000, False, None, {}),
    ("amazon.ae", "B09XS7JWHH", "Sony WH-1000XM5 Wireless Headphones - Black", "1199.00", 4.5, 3000, False, None, {}),
]

# marketplace:asin -> list of (offer_id|None, seller, condition, in_stock, ships, evidence, shipping, fees, days, prime)
_OFFERS: dict[str, list[tuple]] = {
    "amazon.com:B09HM94VDS": [
        (None, "Amazon.com", "new", True, "yes", "Delivery to Jordan; $15.62 Shipping & Import Fees Deposit", "15.62", "28.10", 7, False),
        ("AAAA1111", "ThirdPartyShop", "new", True, "yes", "Ships to Jordan", "20.00", None, 12, False),
    ],
    "amazon.com:B09HMKFDXC": [(None, "Amazon.com", "new", True, "yes", "Delivery to Jordan", "15.62", None, 8, False)],
    "amazon.com:B0MAC3SFOR": [(None, "Amazon.com", "new", True, "no", "This item cannot be shipped to your selected delivery location", None, None, None, False)],
    "amazon.com:B07S395RWD": [(None, "Amazon.com", "new", True, "yes", "Delivery to Jordan", "15.00", "22.00", 7, False)],
    "amazon.com:B0CASE0001": [(None, "CaseCo", "new", True, "yes", "Delivery to Jordan", "9.00", "3.00", 9, False)],
    "amazon.com:B0RENEW001": [(None, "RenewCo", "renewed", True, "yes", "Delivery to Jordan", "15.00", "20.00", 9, False)],
    "amazon.com:B0BUNDLE01": [(None, "BundleCo", "new", True, "yes", "Delivery to Jordan", "18.00", "30.00", 9, False)],
    "amazon.ae:B09HM94VDS": [(None, "Amazon.ae", "new", True, "yes", "Delivery to Jordan; AED 30.00 shipping, AED 45.00 import fees deposit", "30.00", "45.00", 4, True)],
    "amazon.ae:B0KNOCK001": [(None, "XYZ Store", "new", True, "yes", "Delivery to Jordan", "25.00", "8.00", 6, False)],
    "amazon.sa:B09HM94VDS": [(None, "Amazon.sa", "new", True, "unknown", None, "25.00", None, None, False)],
    "amazon.sa:B0SAOUT001": [(None, "Amazon.sa", "new", False, "yes", "Delivery to Jordan", "25.00", "40.00", 5, False)],
    "amazon.com:B09XS7JWHH": [(None, "Amazon.com", "new", True, "yes", "Delivery to Jordan", "25.00", "70.00", 7, False)],
    "amazon.ae:B09XS7JWHH": [(None, "Amazon.ae", "new", True, "yes", "Delivery to Jordan", "40.00", "150.00", 4, True)],
}

_CCY = {"amazon.com": "USD", "amazon.ae": "AED", "amazon.sa": "SAR"}


class FakeAdapter:
    """Implements ``browser.base.AmazonAdapter`` from the in-memory catalogue."""

    def __init__(self, marketplace: str, *, latency_s: float = 0.0, logged_in: bool = True):
        self.marketplace = marketplace
        self.latency_s = latency_s
        self._logged_in = logged_in
        self.cart: list[tuple[str, str | None]] = []
        self.calls: list[tuple] = []

    async def _lat(self) -> None:
        if self.latency_s:
            await asyncio.sleep(self.latency_s)

    async def search(self, query: str, page: int, *, run_id: str) -> list[Candidate]:
        self.calls.append(("search", query, page))
        await self._lat()
        q = normalize_title(query)
        scored: list[tuple[float, tuple]] = []
        for row in _CATALOG:
            if row[0] != self.marketplace:
                continue
            score = fuzz.token_set_ratio(q, normalize_title(row[2]))
            if score >= 60:
                scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], x[1][1]))
        start, end = (page - 1) * 10, page * 10
        out: list[Candidate] = []
        for _, (mk, asin, title, price, rating, count, sponsored, parent, variant) in scored[start:end]:
            out.append(Candidate.build(
                mk, asin, title=title, listed_price=_m(price, _CCY[mk]), rating=rating,
                ratings_count=count, sponsored=sponsored, parent_asin=parent, variant=dict(variant),
            ))
        return out

    async def fetch_offers(self, asin: str, *, include_other_sellers: bool, max_rows: int, run_id: str,
                           bypass_cache: bool = False) -> OfferFetchResult:
        self.calls.append(("fetch_offers", asin, include_other_sellers))
        await self._lat()
        key = f"{self.marketplace}:{asin}"
        rows = _OFFERS.get(key)
        cat = next((r for r in _CATALOG if r[0] == self.marketplace and r[1] == asin), None)
        if not rows or not cat:
            return OfferFetchResult(candidate_ref=key, status="not_found")
        if not include_other_sellers:
            rows = [r for r in rows if r[0] is None]
        rows = rows[:max_rows]
        now = now_utc()
        offers: list[Offer] = []
        for oid, seller, cond, in_stock, ships, evidence, shipping, fees, days, prime in rows:
            price = _m(cat[3] if oid is None else "92.50", _CCY[self.marketplace])
            offers.append(Offer(
                offer_ref=offer_ref(self.marketplace, asin, oid),
                marketplace=self.marketplace, requested_asin=asin, resolved_asin=asin,
                parent_asin=cat[7], offer_id=oid, seller=seller, condition=cond, in_stock=in_stock,
                ships_to_country=ships, shipping_evidence=evidence, price=price,
                shipping=_m(shipping, _CCY[self.marketplace]) if shipping is not None else None,
                import_fees=_m(fees, _CCY[self.marketplace]) if fees is not None else None,
                delivery_days=days, is_prime=prime, title=cat[2], rating=cat[4], ratings_count=cat[5],
                provenance=OfferProvenance(source="fixture", fetched_at=now, admitted_at=now,
                                           run_id=run_id, parser_version=PARSER_VERSION),
                url=f"https://www.{self.marketplace}/dp/{asin}",
            ))
        return OfferFetchResult(candidate_ref=key, status="ok", offers=offers)

    async def login_status(self) -> LoginStatus:
        return LoginStatus(marketplace=self.marketplace, logged_in=self._logged_in, deliver_to_ok=True, detail="fixture")

    async def add_to_cart(self, asin: str, offer_id: str | None) -> CartResult:
        self.calls.append(("add_to_cart", asin, offer_id))
        self.cart.append((asin, offer_id))
        return CartResult(status="added", cart_url=f"https://www.{self.marketplace}/cart", cart_count=len(self.cart))


_instances: dict[str, FakeAdapter] = {}


def fake_adapter_factory(marketplace: str) -> FakeAdapter:
    if marketplace not in _instances:
        _instances[marketplace] = FakeAdapter(marketplace)
    return _instances[marketplace]
