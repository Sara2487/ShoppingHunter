"""Adapter protocol every marketplace backend implements (real Playwright or fixture-backed).

Adapters are the only code that touches Amazon. They return canonical models only; never HTML.
``add_to_cart`` exists on the adapter but is reachable solely through ``cart.CartService``,
never from any agent tool.
"""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel

from ..models import Candidate, OfferFetchResult


class LoginStatus(BaseModel):
    marketplace: str
    logged_in: bool | None = None
    deliver_to_ok: bool | None = None
    detail: str | None = None


class CartResult(BaseModel):
    status: Literal["added", "already_added", "needs_confirmation", "failed"]
    detail: str | None = None
    cart_url: str | None = None
    cart_count: int | None = None


class AmazonAdapter(Protocol):
    marketplace: str

    async def search(self, query: str, page: int, *, run_id: str) -> list[Candidate]: ...

    async def fetch_offers(
        self, asin: str, *, include_other_sellers: bool, max_rows: int, run_id: str, bypass_cache: bool = False
    ) -> OfferFetchResult: ...

    async def login_status(self) -> LoginStatus: ...

    async def add_to_cart(self, asin: str, offer_id: str | None) -> CartResult: ...


AdapterFactory = "Callable[[str], AmazonAdapter]"
