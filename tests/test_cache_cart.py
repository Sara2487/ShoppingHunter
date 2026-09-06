"""Cache TTL/negative behaviour, and CartService safety (tokens, revalidation, idempotency)."""
from __future__ import annotations

import asyncio
import time

import pytest

from shopping_hunter.browser.fake import FakeAdapter
from shopping_hunter.cache import Cache, cache_key
from shopping_hunter.cart import CartService
from shopping_hunter.duties import JORDAN_V1
from shopping_hunter.fx import FxRates
from shopping_hunter.models import HuntRequest
from shopping_hunter.runners import FakeRunner
from shopping_hunter.runs import RunRegistry


async def test_cache_roundtrip_and_expiry(tmp_path):
    c = Cache(tmp_path / "c.sqlite")
    await c.open()
    k = cache_key("search", "amazon.com", "logitech mx master 3s", "JO", "en_US", 1)
    assert await c.get("search", k) is None
    await c.set("search", k, {"candidates": [1, 2]}, ttl_s=60)
    hit = await c.get("search", k)
    assert hit and hit[0] == {"candidates": [1, 2]}
    await c.set("offer", k, {"x": 1}, ttl_s=0)  # ttl 0 = never stored
    assert await c.get("offer", k) is None
    await c.set("offer", k, {"x": 1}, ttl_s=1)
    c._db and await c._db.execute("UPDATE kv SET expires_at=? WHERE kind='offer'", (time.time() - 1,))
    assert await c.get("offer", k) is None  # expired rows are dropped on read
    assert c.hits == 1 and c.misses == 3
    await c.close()


async def _completed_run(settings):
    adapters = {m: FakeAdapter(m) for m in ("amazon.com", "amazon.ae", "amazon.sa")}
    reg = RunRegistry(settings)
    rec = await reg.start(HuntRequest(query="Logitech MX Master 3S", include_other_sellers=True),
                          lambda m: adapters[m], FakeRunner(), FxRates(None), JORDAN_V1)
    await rec.task
    await rec.pump
    return reg, rec, adapters


async def test_registry_runs_and_issues_tokens(settings):
    reg, rec, _ = await _completed_run(settings)
    assert rec.state == "completed" and rec.result
    assert all(s.offer_token for s in rec.result.ranked)
    assert rec.events[-1]["event"] == "run_closed"
    seqs = [e["seq"] for e in rec.events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    got = [e async for e in reg.stream(rec.run_id, after_seq=0)]
    assert got[-1]["event"] == "run_closed"


async def test_cart_requires_valid_token_and_is_single_use(settings):
    reg, rec, adapters = await _completed_run(settings)
    cart = CartService(reg, lambda m: adapters[m], settings)
    top = rec.result.ranked[0]
    bad = await cart.add("nope-nope-nope", "key-000000001")
    assert bad["status"] == "failed"
    ok = await cart.add(top.offer_token, "key-000000002")
    assert ok["status"] == "added" and ok["cart_url"]
    assert adapters[top.marketplace].cart == [(top.resolved_asin, top.offer_id)]
    replay = await cart.add(top.offer_token, "key-000000002")  # same idempotency key: no second add
    assert replay["status"] == "added" and replay.get("idempotent_replay")
    again = await cart.add(top.offer_token, "key-000000003")  # token consumed
    assert again["status"] == "failed" and "used" in again["detail"]
    assert len(adapters[top.marketplace].cart) == 1


async def test_cart_refuses_excluded_and_unconfirmed_shipping(settings):
    reg, rec, adapters = await _completed_run(settings)
    cart = CartService(reg, lambda m: adapters[m], settings)
    unconfirmed = rec.result.unconfirmed[0]  # amazon.sa: ships unknown
    r = await cart.add(unconfirmed.offer_token, "key-000000004")
    assert r["status"] == "failed" and "shipping" in r["detail"]
    assert adapters["amazon.sa"].cart == []


async def test_cancel_marks_run_cancelled(settings):
    adapters = {m: FakeAdapter(m, latency_s=0.3) for m in ("amazon.com", "amazon.ae", "amazon.sa")}
    reg = RunRegistry(settings)
    rec = await reg.start(HuntRequest(query="Logitech MX Master 3S"), lambda m: adapters[m], FakeRunner(), FxRates(None), JORDAN_V1)
    await asyncio.sleep(0.05)
    assert await reg.cancel(rec.run_id)
    await rec.pump
    assert rec.state == "cancelled"


async def test_session_relaunches_after_context_close(settings, monkeypatch):
    """The browser window can be closed by the user; the next use must relaunch it."""
    from shopping_hunter.browser.session import BrowserSession

    class StubContext:
        def __init__(self):
            self.closed = False
            self._handler = None

        def set_default_timeout(self, _ms):
            pass

        def on(self, event, handler):
            if event == "close":
                self._handler = handler

        @property
        def pages(self):
            return []

        async def new_page(self):
            if self.closed:
                raise RuntimeError("Target page, context or browser has been closed")
            return object()

        async def close(self):
            self.closed = True
            if self._handler:
                self._handler(self)

    sess = BrowserSession(settings)
    launched: list[StubContext] = []

    async def fake_launch():
        ctx = StubContext()
        launched.append(ctx)
        sess.context = ctx
        sess._closed = False
        ctx.on("close", sess._on_context_close)

    monkeypatch.setattr(sess, "_launch", fake_launch)
    monkeypatch.setattr(sess._lock, "acquire", lambda: None)
    monkeypatch.setattr(sess._lock, "release", lambda: None)

    await sess.start()
    assert await sess.ensure_alive() is launched[0]

    await launched[0].close()               # user closes the Chrome window
    assert sess._closed is True

    ctx = await sess.ensure_alive()          # next use must recover
    assert ctx is launched[1] and sess.relaunches == 1
    assert len(launched) == 2
