from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from shopping_hunter.browser.fake import FakeAdapter
from shopping_hunter.budgets import BudgetTracker, RunBudgets
from shopping_hunter.config import Settings
from shopping_hunter.context import HuntContext
from shopping_hunter.duties import JORDAN_V1
from shopping_hunter.fx import FxRates
from shopping_hunter.models import HuntRequest, Money, Offer, OfferProvenance, offer_ref


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, profile_dir=tmp_path / "profile", tracing=False)


@pytest.fixture
def fx() -> FxRates:
    return FxRates(None)  # pegs


@pytest.fixture
def rules():
    return JORDAN_V1


@pytest.fixture
def request_default() -> HuntRequest:
    return HuntRequest(query="Logitech MX Master 3S")


@pytest.fixture
def adapters() -> dict[str, FakeAdapter]:
    return {}


@pytest.fixture
def make_ctx(settings, adapters):
    def _make(request: HuntRequest | None = None, budgets: RunBudgets | None = None) -> HuntContext:
        req = request or HuntRequest(query="Logitech MX Master 3S")

        def adapter_for(m: str) -> FakeAdapter:
            if m not in adapters:
                adapters[m] = FakeAdapter(m)
            return adapters[m]

        return HuntContext(run_id=uuid.uuid4().hex[:12], request=req, settings=settings,
                           tracker=BudgetTracker(budgets or RunBudgets.for_depth(req.depth)), adapter_for=adapter_for)
    return _make


def make_offer(marketplace="amazon.com", asin="B09HM94VDS", *, price="99.99", ccy="USD", shipping="15.62",
               fees: str | None = "28.10", ships="yes", in_stock=True, condition="new", rating=4.6, count=31240,
               days=7, offer_id=None, run_id="test") -> Offer:
    now = datetime.now(timezone.utc)
    return Offer(
        offer_ref=offer_ref(marketplace, asin, offer_id), marketplace=marketplace, requested_asin=asin,
        resolved_asin=asin, offer_id=offer_id, seller="Amazon", condition=condition, in_stock=in_stock,
        ships_to_country=ships, price=Money(amount=Decimal(price), currency=ccy),
        shipping=Money(amount=Decimal(shipping), currency=ccy) if shipping is not None else None,
        import_fees=Money(amount=Decimal(fees), currency=ccy) if fees is not None else None,
        delivery_days=days, title="Logitech MX Master 3S", rating=rating, ratings_count=count,
        provenance=OfferProvenance(source="fixture", fetched_at=now, admitted_at=now, run_id=run_id, parser_version="t"),
        url=f"https://www.{marketplace}/dp/{asin}",
    )
