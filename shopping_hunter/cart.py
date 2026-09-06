"""CartService: the only path to Amazon's add-to-cart. Never reachable from an agent.

Flow per click: resolve the single-use token -> re-fetch the offer live -> confirm seller,
condition, stock and shipping -> compare price against what the user saw -> add -> confirm the
cart changed. A price rise beyond the threshold returns ``needs_confirmation`` instead of adding;
the UI then re-posts with ``confirm=true``. Idempotency keys make double-clicks safe.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from .config import Settings
from .logging import log_event
from .models import Offer, ScoredOffer, parse_candidate_ref
from .runs import RunRegistry


class CartService:
    def __init__(self, registry: RunRegistry, adapter_for: Callable[[str], Any], settings: Settings):
        self.registry = registry
        self.adapter_for = adapter_for
        self.settings = settings
        self._idempotent: dict[str, dict[str, Any]] = {}

    async def add(self, token: str, idempotency_key: str, confirm: bool = False) -> dict[str, Any]:
        if idempotency_key in self._idempotent:
            return {**self._idempotent[idempotency_key], "idempotent_replay": True}
        try:
            rec, offer_ref = self.registry.resolve_token(token)
        except KeyError as e:
            return {"status": "failed", "detail": str(e)}
        assert rec.ctx
        old: ScoredOffer | None = rec.ctx.scored.get(offer_ref)
        if old is None:
            return {"status": "failed", "detail": "offer not in run store"}
        if old.rank_tier == "excluded":
            return {"status": "failed", "detail": "offer was excluded; cannot add"}
        marketplace, asin = parse_candidate_ref(old.candidate_ref)
        adapter = self.adapter_for(marketplace)
        log_event("cart_revalidate", run_id=rec.run_id, offer_ref=offer_ref)

        res = await adapter.fetch_offers(asin, include_other_sellers=old.offer_id is not None,
                                         max_rows=self.settings.global_concurrency * 10, run_id=rec.run_id,
                                         bypass_cache=True)
        if res.status == "needs_human":
            return {"status": "failed", "detail": "robot check; solve it in the browser window and retry"}
        if res.status not in ("ok", "location_not_set"):
            return {"status": "failed", "detail": f"could not re-read the offer ({res.status})"}
        fresh: Offer | None = next((o for o in res.offers if o.offer_ref == offer_ref), None)
        if fresh is None:
            return {"status": "failed", "detail": "offer no longer listed"}

        problems: list[str] = []
        if fresh.ships_to_country != "yes":
            problems.append(f"shipping to {self.settings.country} not confirmed ({fresh.ships_to_country})")
        if fresh.in_stock is not True:
            problems.append("stock not confirmed")
        if fresh.condition not in rec.request.allowed_conditions:
            problems.append(f"condition is now {fresh.condition}")
        if old.seller and fresh.seller and old.seller != fresh.seller:
            problems.append(f"seller changed: {old.seller} -> {fresh.seller}")
        if problems:
            return {"status": "failed", "detail": "; ".join(problems), "fresh": fresh.compact()}

        price_note = None
        if old.price and fresh.price:
            if fresh.price.currency != old.price.currency:
                return {"status": "failed", "detail": "currency changed", "fresh": fresh.compact()}
            if old.price.amount > 0:
                change = (fresh.price.amount - old.price.amount) / old.price.amount
                if change > Decimal(str(self.settings.cart_price_change_threshold)) and not confirm:
                    return {"status": "needs_confirmation",
                            "detail": f"price rose from {old.price} to {fresh.price} ({change:.1%}); confirm to add anyway",
                            "fresh": fresh.compact()}
                if change != 0:
                    price_note = f"price changed from {old.price} to {fresh.price}"

        result = await adapter.add_to_cart(asin, old.offer_id)
        out = {"status": result.status, "detail": result.detail or price_note, "cart_url": result.cart_url,
               "cart_count": result.cart_count, "fresh": fresh.compact()}
        log_event("cart_result", run_id=rec.run_id, offer_ref=offer_ref, status=result.status)
        if result.status == "added":
            self.registry.consume_token(token)
            self._idempotent[idempotency_key] = out
        return out
