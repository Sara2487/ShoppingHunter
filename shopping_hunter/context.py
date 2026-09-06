"""Per-run context: the canonical store every tool and the orchestrator read from and write to.

The LLM only ever sees refs. Tools resolve refs against ``candidates`` / ``offers`` here, and
scoring consumes only what is stored here. Phases are a state machine so a tool cannot be used
out of order (e.g. scoring before offers exist).
"""
from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Any, Callable

from .budgets import BudgetTracker
from .config import Settings
from .logging import log_event
from .models import (
    Candidate, Coverage, HuntRequest, MatchDecision, Offer, OfferFetchResult, QueryPlan, ScoredOffer,
)


class Phase(str, enum.Enum):
    initialized = "initialized"
    planned = "planned"
    searched = "searched"
    matched = "matched"
    offers_fetched = "offers_fetched"
    deep_pass = "deep_pass"
    scored = "scored"
    summarized = "summarized"
    validated = "validated"
    done = "done"


_ORDER = list(Phase)


class PhaseError(RuntimeError):
    pass


@dataclass
class HuntContext:
    run_id: str
    request: HuntRequest
    settings: Settings
    tracker: BudgetTracker
    adapter_for: Callable[[str], Any]  # marketplace -> AmazonAdapter
    events: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    phase: Phase = Phase.initialized
    query_plan: QueryPlan | None = None
    candidates: dict[str, Candidate] = field(default_factory=dict)
    decisions: dict[str, MatchDecision] = field(default_factory=dict)
    fetch_results: dict[str, OfferFetchResult] = field(default_factory=dict)
    offers: dict[str, Offer] = field(default_factory=dict)
    scored: dict[str, ScoredOffer] = field(default_factory=dict)
    coverage: Coverage = field(default_factory=Coverage)
    failed_marketplaces: dict[str, str] = field(default_factory=dict)
    dropped_events: int = 0

    # -- phases ----------------------------------------------------------------
    def advance(self, to: Phase) -> None:
        if _ORDER.index(to) < _ORDER.index(self.phase):
            raise PhaseError(f"cannot go back from {self.phase.value} to {to.value}")
        self.phase = to
        self.emit("phase", phase=to.value)

    def advance_to_at_least(self, to: Phase) -> None:
        """Advance only if currently earlier than ``to``; no-op otherwise (never goes back)."""
        if _ORDER.index(self.phase) < _ORDER.index(to):
            self.advance(to)

    def require_phase(self, *allowed: Phase) -> None:
        if self.phase not in allowed:
            raise PhaseError(f"operation not allowed in phase {self.phase.value}")

    # -- events ----------------------------------------------------------------
    def emit(self, event: str, **data: Any) -> None:
        payload = {"event": event, "run_id": self.run_id, **data}
        log_event(event, run_id=self.run_id, **data)
        try:
            self.events.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped_events += 1

    # -- cancellation ----------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError("hunt cancelled")

    # -- store helpers ---------------------------------------------------------
    def admit_candidates(self, cands: list[Candidate]) -> list[Candidate]:
        new: list[Candidate] = []
        for c in cands:
            self.coverage.candidates_before_dedup += 1
            if c.ref not in self.candidates:
                self.candidates[c.ref] = c
                new.append(c)
        self.coverage.candidates_after_dedup = len(self.candidates)
        return new

    def admit_fetch_result(self, r: OfferFetchResult) -> None:
        self.fetch_results[r.candidate_ref] = r
        if r.status == "ok" and r.offers:
            self.coverage.offers_inspected += len(r.offers)
            for o in r.offers:
                self.offers[o.offer_ref] = o
        else:
            self.coverage.offers_skipped[r.status] = self.coverage.offers_skipped.get(r.status, 0) + 1

    def accepted_refs(self) -> list[str]:
        return [ref for ref, d in self.decisions.items() if d.matches and d.confidence != "low"]

    def unfetched_accepted_refs(self) -> list[str]:
        return [ref for ref in self.accepted_refs() if ref not in self.fetch_results]
