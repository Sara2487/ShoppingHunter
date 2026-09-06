"""get_offers: fetch canonical offers for candidate refs. Reference-based; no URLs from the model.

``run_fetch_offers`` is used by the orchestrator; ``get_offers`` is the agent-tool wrapper for
the deep-pass agent. Both store results in the run context; the model only gets compact rows.
"""
from __future__ import annotations

import asyncio
import json

from agents import RunContextWrapper, function_tool

from ..budgets import BudgetExceeded
from ..context import HuntContext, Phase
from ..models import Diagnostic, OfferFetchResult, parse_candidate_ref


async def _fetch_one(ctx: HuntContext, ref: str, include_other_sellers: bool, sem: asyncio.Semaphore) -> OfferFetchResult:
    marketplace, asin = parse_candidate_ref(ref)
    async with sem:
        ctx.check_cancelled()
        try:
            ctx.tracker.consume_browser_request()
        except BudgetExceeded as e:
            return OfferFetchResult(candidate_ref=ref, status="rate_limited",
                                    diagnostic=Diagnostic(code="BUDGET", detail=e.kind))
        ctx.emit("offer_fetch_started", ref=ref, other_sellers=include_other_sellers)
        try:
            res = await asyncio.wait_for(
                ctx.adapter_for(marketplace).fetch_offers(
                    asin, include_other_sellers=include_other_sellers,
                    max_rows=ctx.tracker.budgets.max_other_seller_rows, run_id=ctx.run_id,
                ),
                timeout=ctx.tracker.budgets.max_tool_seconds,
            )
        except asyncio.TimeoutError:
            res = OfferFetchResult(candidate_ref=ref, status="timeout", diagnostic=Diagnostic(code="TOOL_TIMEOUT"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - adapter failures become typed outcomes
            res = OfferFetchResult(candidate_ref=ref, status="parse_error",
                                   diagnostic=Diagnostic(code="ADAPTER_EXCEPTION", detail=str(e)[:200]))
        ctx.emit("offer_fetch_finished", ref=ref, status=res.status, offers=len(res.offers),
                 diagnostic=res.diagnostic.model_dump() if res.diagnostic else None)
        return res


async def run_fetch_offers(ctx: HuntContext, refs: list[str], include_other_sellers: bool = False) -> list[OfferFetchResult]:
    refs = [r for r in dict.fromkeys(refs) if r in ctx.candidates]
    if not refs:
        return []
    ctx.tracker.consume_offer_call(len(refs))
    sem = asyncio.Semaphore(ctx.settings.global_concurrency)
    results = await asyncio.gather(*(_fetch_one(ctx, r, include_other_sellers, sem) for r in refs))
    for r in results:
        ctx.admit_fetch_result(r)
    return list(results)


@function_tool(name_override="get_offers")
async def get_offers(wrapper: RunContextWrapper[HuntContext], candidate_refs: list[str], include_other_sellers: bool) -> str:
    """Fetch price, shipping, import fees, stock and delivery for candidates found by search_amazon.

    Args:
        candidate_refs: Refs exactly as returned by search_amazon (e.g. "amazon.ae:B09HM94VDS"). Max 10.
        include_other_sellers: Also read other sellers' offers for the same listing.
    """
    ctx = wrapper.context
    ctx.require_phase(Phase.deep_pass)
    refs = list(dict.fromkeys(candidate_refs))[:10]
    unknown = [r for r in refs if r not in ctx.candidates]
    if unknown:
        return json.dumps({"status": "invalid_args", "detail": f"unknown refs: {unknown[:5]}"})
    try:
        results = await run_fetch_offers(ctx, refs, include_other_sellers)
    except BudgetExceeded as e:
        return json.dumps({"status": "budget_exceeded", "budget": e.kind, "hint": "stop and finish"})
    out = []
    for r in results:
        out.append({
            "candidate_ref": r.candidate_ref,
            "status": r.status,
            "offers": [o.compact() for o in r.offers],
        })
    return json.dumps({"status": "ok", "results": out}, ensure_ascii=False, default=str)
