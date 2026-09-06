"""End-to-end over the fixture adapter and fake runner, plus validator, budgets and agent-safety checks."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from shopping_hunter.agents import ALL_BUILDERS
from shopping_hunter.budgets import BudgetExceeded, RunBudgets
from shopping_hunter.context import Phase, PhaseError
from shopping_hunter.duties import JORDAN_V1
from shopping_hunter.fx import FxRates
from shopping_hunter.models import HuntRequest, HuntResult, ScoredOffer
from shopping_hunter.orchestrator import run_hunt
from shopping_hunter.runners import FakeRunner
from shopping_hunter.validate import validate_hunt_result, validate_summary_text

from .conftest import make_offer


async def _run(ctx) -> HuntResult:
    return await run_hunt(ctx, FakeRunner(), FxRates(None), JORDAN_V1)


async def test_fake_end_to_end_ranks_only_confirmed_offers(make_ctx):
    ctx = make_ctx()
    r = await _run(ctx)
    assert r.status == "completed", r.status_reason
    assert r.validation_errors == []
    refs = [s.offer_ref for s in r.ranked]
    assert set(refs) == {"amazon.ae:B09HM94VDS:buybox", "amazon.com:B09HM94VDS:buybox", "amazon.com:B09HMKFDXC:buybox"}
    landed = [s.landed_jod for s in r.ranked]
    assert landed == sorted(landed)
    assert r.summary and r.ranked[0].offer_ref in r.summary
    for s in r.ranked:
        assert s.ships_to_country == "yes" and s.in_stock and s.passes_review_floor and s.landed_cost_complete
    assert [s.offer_ref for s in r.unconfirmed] == ["amazon.sa:B09HM94VDS:buybox"]
    codes = {(e.marketplace, e.asin): e.reason_code for e in r.excluded}
    assert codes[("amazon.com", "B07S395RWD")] == "wrong_model"
    assert codes[("amazon.com", "B0CASE0001")] == "accessory"
    assert codes[("amazon.com", "B0RENEW001")] == "renewed"
    assert codes[("amazon.com", "B0BUNDLE01")] == "bundle"
    assert codes[("amazon.ae", "B0KNOCK001")] == "knockoff"
    assert codes[("amazon.com", "B0MAC3SFOR")] == "does_not_ship"
    assert codes[("amazon.sa", "B0SAOUT001")] == "out_of_stock"
    assert r.coverage.marketplaces_completed == ["amazon.com", "amazon.ae", "amazon.sa"]
    assert ctx.phase == Phase.summarized


async def test_other_sellers_adds_third_party_offer(make_ctx):
    ctx = make_ctx(HuntRequest(query="Logitech MX Master 3S", include_other_sellers=True))
    r = await _run(ctx)
    assert "amazon.com:B09HM94VDS:AAAA1111" in ctx.offers
    assert any(s.offer_ref == "amazon.com:B09HM94VDS:AAAA1111" for s in r.ranked)


async def test_candidate_cap_is_enforced_and_reported(make_ctx):
    ctx = make_ctx(budgets=RunBudgets(max_candidates_per_marketplace=2))
    r = await _run(ctx)
    admitted, fetched = {}, {}
    for c in ctx.candidates.values():
        admitted[c.marketplace] = admitted.get(c.marketplace, 0) + 1
    for ref in ctx.fetch_results:
        m = ref.split(":")[0]
        fetched[m] = fetched.get(m, 0) + 1
    assert all(v <= 2 * 3 for v in admitted.values())   # admission cap = 3x inspection cap
    assert all(v <= 2 for v in fetched.values())         # inspection cap on accepted candidates
    assert r.coverage.truncated_by_cap > 0 or r.coverage.accepted_not_fetched > 0
    assert r.coverage.accepted_not_fetched >= 1          # amazon.com has 3 accepted matches, cap 2


async def test_run_deadline_yields_partial_not_exception(make_ctx, adapters):
    ctx = make_ctx(budgets=RunBudgets(max_run_seconds=0.0))
    r = await _run(ctx)
    assert r.status == "partial" and r.status_reason.startswith("budget:")


async def test_cancel_yields_cancelled_result(make_ctx, adapters):
    ctx = make_ctx()
    adapters["amazon.com"] = type("Slow", (), {})()  # replaced below
    from shopping_hunter.browser.fake import FakeAdapter
    for m in ("amazon.com", "amazon.ae", "amazon.sa"):
        adapters[m] = FakeAdapter(m, latency_s=0.2)
    task = asyncio.create_task(_run(ctx))
    await asyncio.sleep(0.05)
    ctx.cancel_event.set()
    r = await task
    assert r.status == "cancelled"


def test_no_agent_has_a_cart_tool(settings):
    for build in ALL_BUILDERS:
        agent = build(settings)
        names = [getattr(t, "name", "") for t in agent.tools]
        assert not any("cart" in n.lower() for n in names), names
    assert {t.name for t in ALL_BUILDERS[2](settings).tools} == {"search_amazon", "get_offers"}
    for b in (ALL_BUILDERS[0], ALL_BUILDERS[1], ALL_BUILDERS[3]):
        assert b(settings).tools == []


def test_validator_rejects_unfetched_and_tampered_rows(make_ctx):
    ctx = make_ctx()
    good = ScoredOffer(**make_offer().model_dump(), landed_jod=Decimal("100"), price_jod=Decimal("70"),
                       landed_cost_complete=True, passes_review_floor=True, rank_tier="ranked")
    ctx.scored[good.offer_ref] = good
    ghost = good.model_copy(update={"offer_ref": "amazon.com:B0GHOST001:buybox"})
    tampered = good.model_copy(update={"landed_jod": Decimal("1")})
    result = HuntResult(run_id=ctx.run_id, status="completed", query="x", ranked=[tampered, ghost, good])
    errs = validate_hunt_result(result, ctx)
    assert any("not in run store" in e for e in errs)
    assert any("money differs" in e for e in errs)
    assert any("duplicate" in e for e in errs)


def test_validator_rejects_unknown_asin_url_and_amount_in_summary(make_ctx):
    ctx = make_ctx()
    o = make_offer()
    ctx.offers[o.offer_ref] = o
    ctx.scored[o.offer_ref] = ScoredOffer(**o.model_dump(), landed_jod=Decimal("102.500"))
    errs = validate_summary_text("Best is B0FAKE0000 at https://evil.example/x for 999 JOD; also 102.5 JOD.", ctx)
    assert any("unknown ASIN" in e for e in errs)
    assert any("unknown URL" in e for e in errs)
    assert any("unknown amount 999" in e for e in errs)
    assert not any("102.5" in e for e in errs)


def test_tools_refuse_out_of_phase(make_ctx):
    ctx = make_ctx()
    with pytest.raises(PhaseError):
        ctx.require_phase(Phase.deep_pass)


def test_budget_tracker_raises():
    from shopping_hunter.budgets import BudgetTracker
    t = BudgetTracker(RunBudgets(max_search_calls=1))
    t.consume_search_call("amazon.com")
    with pytest.raises(BudgetExceeded):
        t.consume_search_call("amazon.com")


async def test_all_marketplaces_failing_is_partial_not_completed(make_ctx, adapters):
    """A run that searched nothing must not report success (regression: browser window closed)."""
    from shopping_hunter.browser.fake import FakeAdapter

    class DeadAdapter(FakeAdapter):
        async def search(self, query, page, *, run_id):
            raise RuntimeError("Target page, context or browser has been closed")

    for m in ("amazon.com", "amazon.ae", "amazon.sa"):
        adapters[m] = DeadAdapter(m)
    ctx = make_ctx()
    r = await _run(ctx)
    assert r.status == "partial" and r.status_reason == "search_failed_everywhere"
    assert r.ranked == [] and r.coverage.marketplaces_completed == []
    assert any("Search did not complete" in c for c in r.caveats)
    assert any("browser has been closed" in c for c in r.caveats)


async def test_one_marketplace_failing_is_partial_and_keeps_the_rest(make_ctx, adapters):
    from shopping_hunter.browser.fake import FakeAdapter

    class DeadAdapter(FakeAdapter):
        async def search(self, query, page, *, run_id):
            raise RuntimeError("boom")

    adapters["amazon.sa"] = DeadAdapter("amazon.sa")
    ctx = make_ctx()
    r = await _run(ctx)
    assert r.status == "partial" and r.status_reason == "search_incomplete"
    assert r.ranked, "offers from the healthy marketplaces must still be ranked"
    assert "amazon.sa" not in r.coverage.marketplaces_completed
