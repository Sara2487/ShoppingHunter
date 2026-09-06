"""The hunt pipeline. Harness-sequenced; agents make bounded judgments in between.

    plan -> search -> match -> fetch offers -> (deep pass) -> score -> summarize -> validate

Every failure mode (budget, turn limit, cancel, exception) still produces a HuntResult built
from whatever the canonical store holds, then validated. Nothing unvalidated leaves here.
"""
from __future__ import annotations

import asyncio
import json
import traceback

from agents.exceptions import MaxTurnsExceeded

from .agents import build_deep_pass, build_matcher, build_query_planner, build_summarizer
from .budgets import BudgetExceeded
from .context import HuntContext, Phase
from .duties import DutyRules
from .fx import FxRates
from .logging import log_event
from .models import (
    DeepPassReport, ExcludedCandidate, HuntResult, HuntSummary, MatchDecision, MatchDecisions, QueryPlan, now_utc,
)
from .normalize import clean_text, prefilter
from .runners import AgentRunner
from .scoring import score_offers, split_tiers
from .tools.offers import run_fetch_offers
from .tools.search import run_search
from .validate import validate_hunt_result, validate_summary


# --------------------------------------------------------------------------- steps

async def _plan(ctx: HuntContext, runner: AgentRunner) -> None:
    b = ctx.tracker.budgets
    payload = {"query": ctx.request.query, "marketplaces": ctx.request.marketplaces,
               "max_queries_per_marketplace": b.max_queries_per_marketplace}
    plan: QueryPlan = await runner.run(build_query_planner(ctx.settings), json.dumps(payload), ctx, max_turns=2)
    # Enforce: enabled marketplaces only, cap per marketplace, dedupe, fallback to the raw query.
    per: dict[str, list[str]] = {m: [] for m in ctx.request.marketplaces}
    for mq in plan.queries:
        q = clean_text(mq.query, 200)
        if mq.marketplace in per and q and q.lower() not in [x.lower() for x in per[mq.marketplace]]:
            if len(per[mq.marketplace]) < b.max_queries_per_marketplace:
                per[mq.marketplace].append(q)
    for m, qs in per.items():
        if not qs:
            qs.append(ctx.request.query)
    ctx.query_plan = QueryPlan(
        canonical_brand=plan.canonical_brand, canonical_model=plan.canonical_model,
        queries=[{"marketplace": m, "query": q} for m, qs in per.items() for q in qs],  # type: ignore[list-item]
    )
    ctx.emit("plan_ready", brand=plan.canonical_brand, model=plan.canonical_model,
             queries={m: qs for m, qs in per.items()})
    ctx.advance(Phase.planned)


async def _search_marketplace(ctx: HuntContext, marketplace: str, queries: list[str]) -> None:
    ctx.coverage.marketplaces_attempted.append(marketplace)
    try:
        for q in queries:
            for page in range(1, ctx.tracker.budgets.max_pages_per_query + 1):
                new = await run_search(ctx, marketplace, q, page)
                if not new:
                    break  # no new results on this page; next query
        ctx.coverage.marketplaces_completed.append(marketplace)
    except BudgetExceeded as e:
        if e.kind != "max_queries_per_marketplace":
            raise  # run-wide budget (deadline, total calls): abort the whole hunt as partial
        ctx.emit("marketplace_budget_stop", marketplace=marketplace, budget=e.kind)
        ctx.coverage.marketplaces_completed.append(marketplace)  # stopped by design, not failure
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        ctx.failed_marketplaces[marketplace] = str(e)[:200]
        ctx.emit("marketplace_failed", marketplace=marketplace, error=str(e)[:200])


async def _search(ctx: HuntContext) -> None:
    assert ctx.query_plan
    per: dict[str, list[str]] = {}
    for mq in ctx.query_plan.queries:
        per.setdefault(mq.marketplace, []).append(mq.query)
    await asyncio.gather(*(_search_marketplace(ctx, m, qs) for m, qs in per.items()))
    ctx.advance(Phase.searched)


def _record_decision(ctx: HuntContext, d: MatchDecision) -> None:
    if d.candidate_ref not in ctx.candidates:
        return
    ctx.decisions[d.candidate_ref] = d
    ctx.emit("candidate_decision", ref=d.candidate_ref, decision="accepted" if d.matches else "excluded",
             confidence=d.confidence, reason=d.rejection_code, detail=(d.reasons[0] if d.reasons else None))


async def _match(ctx: HuntContext, runner: AgentRunner, refs: list[str] | None = None) -> None:
    initial = refs is None
    refs = refs if refs is not None else list(ctx.candidates)
    pending = []
    for ref in refs:
        c = ctx.candidates[ref]
        pre = prefilter(ctx.request.query, c.title)
        if pre:
            code, reason = pre
            _record_decision(ctx, MatchDecision(candidate_ref=ref, matches=False, confidence="high",
                                                canonical_brand=None, canonical_model=None, variant=[],
                                                reasons=[f"prefilter: {reason}"], rejection_code=code))  # type: ignore[arg-type]
        else:
            pending.append(c)
    if pending:
        payload = {
            "query": ctx.request.query,
            "canonical_brand": ctx.query_plan.canonical_brand if ctx.query_plan else None,
            "canonical_model": ctx.query_plan.canonical_model if ctx.query_plan else None,
            "candidates": [c.compact() for c in pending],
        }
        out: MatchDecisions = await runner.run(build_matcher(ctx.settings), json.dumps(payload, ensure_ascii=False), ctx, max_turns=2)
        for d in out.decisions:
            _record_decision(ctx, d)
        for c in pending:  # anything the model skipped is ambiguous, never silently accepted
            if c.ref not in ctx.decisions:
                _record_decision(ctx, MatchDecision(candidate_ref=c.ref, matches=False, confidence="low",
                                                    canonical_brand=None, canonical_model=None, variant=[],
                                                    reasons=["no decision returned"], rejection_code="ambiguous"))
    if initial:
        ctx.advance(Phase.matched)


def _select_for_fetch(ctx: HuntContext) -> list[str]:
    """Accepted, not-yet-fetched refs, capped per marketplace in search order; the rest is reported."""
    cap = ctx.tracker.budgets.max_candidates_per_marketplace
    fetched_per: dict[str, int] = {}
    for ref in ctx.fetch_results:
        m = ref.split(":", 1)[0]
        fetched_per[m] = fetched_per.get(m, 0) + 1
    chosen: list[str] = []
    skipped = 0
    for ref in ctx.unfetched_accepted_refs():
        m = ref.split(":", 1)[0]
        if fetched_per.get(m, 0) < cap:
            chosen.append(ref)
            fetched_per[m] = fetched_per.get(m, 0) + 1
        else:
            skipped += 1
    ctx.coverage.accepted_not_fetched += skipped
    return chosen


async def _fetch(ctx: HuntContext) -> None:
    refs = _select_for_fetch(ctx)
    if refs:
        await run_fetch_offers(ctx, refs, include_other_sellers=ctx.request.include_other_sellers)
    if ctx.phase == Phase.matched:
        ctx.advance(Phase.offers_fetched)


def _marketplaces_without_matches(ctx: HuntContext) -> list[str]:
    accepted = {ctx.candidates[r].marketplace for r in ctx.accepted_refs()}
    return [m for m in ctx.request.marketplaces if m not in accepted]


async def _deep_pass(ctx: HuntContext, runner: AgentRunner) -> None:
    empty = _marketplaces_without_matches(ctx)
    if not empty:
        ctx.emit("deep_pass_skipped", reason="all marketplaces have accepted matches")
        ctx.advance(Phase.deep_pass)
        return
    if ctx.tracker.remaining_s < 30:
        ctx.emit("deep_pass_skipped", reason="insufficient time budget")
        ctx.advance(Phase.deep_pass)
        return
    ctx.advance(Phase.deep_pass)
    payload = {
        "query": ctx.request.query,
        "canonical_brand": ctx.query_plan.canonical_brand if ctx.query_plan else None,
        "canonical_model": ctx.query_plan.canonical_model if ctx.query_plan else None,
        "enabled_marketplaces": ctx.request.marketplaces,
        "marketplaces_without_accepted_matches": empty,
        "queries_already_used": [mq.model_dump() for mq in ctx.coverage.queries_used],
        "other_sellers_requested": False,
        "accepted_refs": ctx.accepted_refs(),
    }
    before = set(ctx.candidates)
    report: DeepPassReport = await runner.run(build_deep_pass(ctx.settings), json.dumps(payload, ensure_ascii=False),
                                              ctx, max_turns=ctx.tracker.budgets.max_model_turns)
    ctx.emit("deep_pass_finished", performed=report.performed, action=report.action, notes=clean_text(report.notes, 300))
    new_refs = [r for r in ctx.candidates if r not in before]
    for d in report.new_decisions:
        if d.candidate_ref in new_refs and d.candidate_ref not in ctx.decisions:
            _record_decision(ctx, d)
    undecided = [r for r in new_refs if r not in ctx.decisions]
    if undecided:
        await _match(ctx, runner, undecided)
    await _fetch(ctx)


def _score(ctx: HuntContext, fx: FxRates, rules: DutyRules) -> None:
    scored = score_offers(ctx.offers.values(), ctx.decisions, ctx.request, fx, rules, ctx.settings.currency)
    ctx.scored = {s.offer_ref: s for s in scored}
    ranked, unconfirmed, excluded = split_tiers(scored)
    ctx.emit("scored", ranked=len(ranked), unconfirmed=len(unconfirmed), excluded=len(excluded))
    ctx.advance_to_at_least(Phase.scored)


async def _summarize(ctx: HuntContext, runner: AgentRunner, result: HuntResult) -> None:
    if not result.ranked and not result.unconfirmed:
        ctx.advance(Phase.summarized)
        return
    payload = {
        "query": ctx.request.query,
        "ranked": [s.compact() for s in result.ranked[:5]],
        "unconfirmed_count": len(result.unconfirmed),
        "excluded_count": len(result.excluded),
        "coverage": result.coverage.model_dump(),
        "target_currency": ctx.settings.currency,
    }
    summary: HuntSummary = await runner.run(build_summarizer(ctx.settings), json.dumps(payload, ensure_ascii=False, default=str), ctx, max_turns=2)
    errs = validate_summary(summary, ctx)
    if errs:
        result.validation_errors += errs
        result.caveats.append("Summary omitted: it referenced data not present in this run.")
        ctx.emit("summary_rejected", errors=errs[:5])
    else:
        result.summary = clean_text(summary.summary, 2000)
        result.caveats += [clean_text(c, 300) for c in summary.caveats[:3]]
    ctx.advance(Phase.summarized)


# --------------------------------------------------------------------------- assembly

def _assemble(ctx: HuntContext, result: HuntResult) -> HuntResult:
    ranked, unconfirmed, excluded_offers = split_tiers(list(ctx.scored.values()))
    result.ranked, result.unconfirmed = ranked, unconfirmed
    excluded: list[ExcludedCandidate] = []
    for ref, c in ctx.candidates.items():
        d = ctx.decisions.get(ref)
        if d is None:
            excluded.append(ExcludedCandidate(marketplace=c.marketplace, asin=c.asin, title=c.title,
                                              reason_code="not_evaluated", reason="run ended before matching"))
        elif not d.matches or d.confidence == "low":
            excluded.append(ExcludedCandidate(marketplace=c.marketplace, asin=c.asin, title=c.title,
                                              reason_code=d.rejection_code or "low_match_confidence",
                                              reason=d.reasons[0] if d.reasons else ""))
        else:
            fr = ctx.fetch_results.get(ref)
            if fr is None:
                excluded.append(ExcludedCandidate(marketplace=c.marketplace, asin=c.asin, title=c.title,
                                                  reason_code="not_fetched", reason="offer was not inspected"))
            elif fr.status != "ok":
                excluded.append(ExcludedCandidate(marketplace=c.marketplace, asin=c.asin, title=c.title,
                                                  reason_code=fr.status,
                                                  reason=(fr.diagnostic.detail or fr.diagnostic.code) if fr.diagnostic else fr.status))
    for s in excluded_offers:
        excluded.append(ExcludedCandidate(marketplace=s.marketplace, asin=s.resolved_asin, title=s.title,
                                          reason_code=s.exclusion_reason or "excluded",
                                          reason=f"offer {s.offer_ref}: {s.exclusion_reason}"))
    result.excluded = excluded
    result.coverage = ctx.coverage

    if any(s.fees_source == "estimate" for s in ranked):
        result.caveats.append("Import fees for some offers are estimates from a provisional rules table, not Amazon figures.")
    if unconfirmed:
        result.caveats.append(f"{len(unconfirmed)} matching offer(s) could not be confirmed as shipping to {ctx.settings.country} with a complete cost.")
    incomplete = [m for m in ctx.request.marketplaces if m not in ctx.coverage.marketplaces_completed]
    if incomplete:
        why = "; ".join(f"{m}: {ctx.failed_marketplaces[m]}" for m in incomplete if m in ctx.failed_marketplaces)
        result.caveats.append(f"Search did not complete on: {', '.join(incomplete)}." + (f" ({why})" if why else ""))
        # A run that searched nothing is not a completed run, whatever the agents returned.
        if result.status == "completed":
            result.status = "partial"
            result.status_reason = ("search_failed_everywhere" if not ctx.coverage.marketplaces_completed
                                    else "search_incomplete")
    if any(s.provenance.source == "cache" for s in ranked):
        result.caveats.append("Some offers came from cache; they are re-checked before add-to-cart.")
    result.caveats.append("Prices and eligibility were read from product pages and may change in cart or checkout.")

    errs = validate_hunt_result(result, ctx)
    if errs:
        result.validation_errors += errs
        bad = {e.split(":", 2)[1] for e in errs if e.count(":") >= 2 and e.split(":", 1)[0] in ("ranked", "unconfirmed")}
        result.ranked = [r for r in result.ranked if r.offer_ref not in bad]
        result.unconfirmed = [r for r in result.unconfirmed if r.offer_ref not in bad]
        if any(e.startswith("summary:") for e in errs):
            result.summary = None
        ctx.emit("validation_failed", errors=errs[:10])
    return result


# --------------------------------------------------------------------------- entry

async def run_hunt(ctx: HuntContext, runner: AgentRunner, fx: FxRates, rules: DutyRules) -> HuntResult:
    result = HuntResult(run_id=ctx.run_id, status="completed", query=ctx.request.query)
    assembled = False
    ctx.emit("hunt_started", query=ctx.request.query, marketplaces=ctx.request.marketplaces,
             depth=ctx.request.depth, budgets=ctx.tracker.budgets.__dict__)
    try:
        await _plan(ctx, runner)
        await _search(ctx)
        await _match(ctx, runner)
        await _fetch(ctx)
        await _deep_pass(ctx, runner)
        _score(ctx, fx, rules)
        _assemble(ctx, result)
        assembled = True
        await _summarize(ctx, runner, result)
    except BudgetExceeded as e:
        result.status, result.status_reason = "partial", f"budget:{e.kind}"
    except MaxTurnsExceeded:
        result.status, result.status_reason = "partial", "model_turn_limit"
    except asyncio.CancelledError:
        if not ctx.cancelled:
            raise
        result.status, result.status_reason = "cancelled", "user_cancelled"
    except Exception as e:  # noqa: BLE001
        result.status, result.status_reason = "failed", f"{type(e).__name__}: {str(e)[:200]}"
        log_event("hunt_exception", run_id=ctx.run_id, error=traceback.format_exc()[-1500:])
    finally:
        if not assembled:
            # Score whatever we have so a partial run still shows validated rows.
            try:
                _score(ctx, fx, rules)
            except Exception as e:  # noqa: BLE001
                log_event("partial_score_failed", run_id=ctx.run_id, error=str(e)[:200])
            _assemble(ctx, result)
        result.finished_at = now_utc()
        ctx.emit("hunt_finished", status=result.status, reason=result.status_reason,
                 ranked=len(result.ranked), unconfirmed=len(result.unconfirmed), excluded=len(result.excluded),
                 budgets=ctx.tracker.snapshot(), dropped_events=ctx.dropped_events)
    return result
