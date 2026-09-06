"""How agents get executed: the real Agents SDK ``Runner`` or a deterministic fake.

The fake runner lets the whole pipeline run offline (tests, ``--fake``) and doubles as a
reference for what "obviously right" matching looks like. It dispatches on ``agent.name``.
"""
from __future__ import annotations

import json
from typing import Any, Protocol

from agents import Agent, Runner, set_tracing_disabled

from .config import Settings
from .context import HuntContext
from .logging import log_event
from .models import (
    DeepPassReport, HuntSummary, MarketplaceQuery, MatchDecision, MatchDecisions, QueryPlan, VariantAttr,
)
from .normalize import model_tokens, normalize_title, prefilter


class AgentRunner(Protocol):
    async def run(self, agent: Agent[HuntContext], input: str, ctx: HuntContext, max_turns: int) -> Any: ...


class SdkRunner:
    def __init__(self, settings: Settings):
        if not settings.tracing:
            set_tracing_disabled(True)

    async def run(self, agent: Agent[HuntContext], input: str, ctx: HuntContext, max_turns: int) -> Any:
        ctx.emit("agent_started", agent=agent.name, input_chars=len(input))
        result = await Runner.run(agent, input, context=ctx, max_turns=max_turns)
        usage = None
        try:
            u = result.context_wrapper.usage
            usage = {"requests": u.requests, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
        except Exception:  # noqa: BLE001
            pass
        ctx.emit("agent_finished", agent=agent.name, usage=usage)
        return result.final_output


class FakeRunner:
    """Deterministic stand-in for the LLM. Good enough to exercise every code path."""

    async def run(self, agent: Agent[HuntContext], input: str, ctx: HuntContext, max_turns: int) -> Any:
        data = json.loads(input)
        ctx.emit("agent_started", agent=agent.name, fake=True)
        fn = getattr(self, f"_{agent.name}")
        out = fn(data, ctx)
        ctx.emit("agent_finished", agent=agent.name, fake=True)
        return out

    def _query_planner(self, data: dict, ctx: HuntContext) -> QueryPlan:
        q = data["query"].strip()
        words = q.split()
        brand = words[0] if len(words) > 1 else None
        model = " ".join(words[1:]) if len(words) > 1 else q
        return QueryPlan(canonical_brand=brand, canonical_model=model,
                         queries=[MarketplaceQuery(marketplace=m, query=q) for m in data["marketplaces"]])

    def _matcher(self, data: dict, ctx: HuntContext) -> MatchDecisions:
        query = data["query"]
        brand = (data.get("canonical_brand") or "").lower()
        want = model_tokens(query)
        # Every word of the canonical model must appear ("mx", "master", "3s"), not just the numeric token.
        model_words = set(normalize_title(data.get("canonical_model") or "").split()) - {brand}
        decisions: list[MatchDecision] = []
        for c in data["candidates"]:
            title = c["title"]
            t = normalize_title(title)
            pre = prefilter(query, title)
            variant: list[VariantAttr] = []
            if pre:
                code, reason = pre
                decisions.append(MatchDecision(candidate_ref=c["ref"], matches=False, confidence="high",
                                               canonical_brand=None, canonical_model=None, variant=[],
                                               reasons=[reason], rejection_code=code))  # type: ignore[arg-type]
                continue
            if brand and brand not in t:
                decisions.append(MatchDecision(candidate_ref=c["ref"], matches=False, confidence="high",
                                               canonical_brand=None, canonical_model=None, variant=[],
                                               reasons=["brand missing from title"], rejection_code="knockoff"))
                continue
            have = model_tokens(title)
            title_words = set(t.split())
            missing_words = {w for w in model_words if w not in title_words and not any(w in tw for tw in title_words if len(w) > 2)}
            if not want.issubset(have) or missing_words:
                decisions.append(MatchDecision(candidate_ref=c["ref"], matches=False, confidence="high",
                                               canonical_brand=data.get("canonical_brand"),
                                               canonical_model=None, variant=[],
                                               reasons=[f"model tokens {sorted((want - have) | missing_words)} missing"],
                                               rejection_code="wrong_model"))
                continue
            conf = "high"
            if "for mac" in t:
                variant.append(VariantAttr(name="edition", value="for Mac"))
                conf = "medium"
            decisions.append(MatchDecision(candidate_ref=c["ref"], matches=True, confidence=conf,  # type: ignore[arg-type]
                                           canonical_brand=data.get("canonical_brand"),
                                           canonical_model=data.get("canonical_model"), variant=variant,
                                           reasons=["brand and model tokens present"], rejection_code=None))
        return MatchDecisions(decisions=decisions)

    def _deep_pass(self, data: dict, ctx: HuntContext) -> DeepPassReport:
        return DeepPassReport(performed=False, action="none", new_decisions=[], notes="fake runner: no deep pass")

    def _summarizer(self, data: dict, ctx: HuntContext) -> HuntSummary:
        ranked = data.get("ranked", [])
        if not ranked:
            return HuntSummary(summary="No offer could be confirmed as shipping to Jordan with a complete landed cost.",
                               top_offer_ref=None, mentioned_offer_refs=[], caveats=[])
        top = ranked[0]
        est = " Import fees were estimated, not shown by Amazon." if top.get("fees_source") == "estimate" else ""
        s = (f"The best confirmed offer is {top['offer_ref']} at a landed cost of {top['landed_jod']} JOD "
             f"(delivery in {top.get('delivery_days')} days, rating {top.get('rating')} from {top.get('ratings_count')} ratings)."
             f"{est} {len(ranked)} offer(s) were ranked; {data.get('unconfirmed_count', 0)} could not be confirmed and "
             f"{data.get('excluded_count', 0)} were excluded.")
        return HuntSummary(summary=s, top_offer_ref=top["offer_ref"], mentioned_offer_refs=[top["offer_ref"]],
                           caveats=(["Import fees estimated for some offers."] if est else []))


def make_runner(settings: Settings, fake: bool) -> AgentRunner:
    if fake:
        log_event("runner_selected", kind="fake")
        return FakeRunner()
    log_event("runner_selected", kind="sdk", model=settings.model)
    return SdkRunner(settings)
