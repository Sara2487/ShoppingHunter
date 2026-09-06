"""Run budgets, enforced in Python. Prompts never carry limits; the tracker does.

Every counter has a hard cap. Tools and the orchestrator call ``consume`` before doing work; on
overrun a ``BudgetExceeded`` propagates and the orchestrator turns it into a ``partial`` result.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import Depth


class BudgetExceeded(Exception):
    def __init__(self, kind: str, limit: int | float):
        super().__init__(f"budget exceeded: {kind} (limit {limit})")
        self.kind = kind
        self.limit = limit


@dataclass(frozen=True)
class RunBudgets:
    max_model_turns: int = 8
    max_search_calls: int = 12
    max_offer_calls: int = 6
    max_queries_per_marketplace: int = 3
    max_pages_per_query: int = 1
    max_candidates_per_marketplace: int = 10
    max_other_seller_rows: int = 10
    max_offers_total: int = 40
    max_browser_requests: int = 60
    max_run_seconds: float = 300.0
    max_tool_seconds: float = 60.0

    @classmethod
    def for_depth(cls, depth: Depth) -> "RunBudgets":
        if depth == "thorough":
            return cls(
                max_pages_per_query=2,
                max_candidates_per_marketplace=30,
                max_offers_total=80,
                max_browser_requests=150,
                max_run_seconds=600.0,
            )
        return cls()


@dataclass
class BudgetTracker:
    budgets: RunBudgets
    started_at: float = field(default_factory=time.monotonic)
    search_calls: int = 0
    offer_calls: int = 0
    browser_requests: int = 0
    offers_total: int = 0
    queries_by_marketplace: dict[str, int] = field(default_factory=dict)

    # -- time --------------------------------------------------------------
    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def remaining_s(self) -> float:
        return max(0.0, self.budgets.max_run_seconds - self.elapsed_s)

    def check_deadline(self) -> None:
        if self.elapsed_s >= self.budgets.max_run_seconds:
            raise BudgetExceeded("max_run_seconds", self.budgets.max_run_seconds)

    # -- counters ----------------------------------------------------------
    def consume_search_call(self, marketplace: str) -> None:
        self.check_deadline()
        if self.search_calls >= self.budgets.max_search_calls:
            raise BudgetExceeded("max_search_calls", self.budgets.max_search_calls)
        n = self.queries_by_marketplace.get(marketplace, 0)
        if n >= self.budgets.max_queries_per_marketplace * self.budgets.max_pages_per_query:
            raise BudgetExceeded("max_queries_per_marketplace", self.budgets.max_queries_per_marketplace)
        self.search_calls += 1
        self.queries_by_marketplace[marketplace] = n + 1

    def consume_offer_call(self, n_items: int) -> None:
        self.check_deadline()
        if self.offer_calls >= self.budgets.max_offer_calls:
            raise BudgetExceeded("max_offer_calls", self.budgets.max_offer_calls)
        if self.offers_total + n_items > self.budgets.max_offers_total:
            raise BudgetExceeded("max_offers_total", self.budgets.max_offers_total)
        self.offer_calls += 1
        self.offers_total += n_items

    def consume_browser_request(self) -> None:
        self.check_deadline()
        if self.browser_requests >= self.budgets.max_browser_requests:
            raise BudgetExceeded("max_browser_requests", self.budgets.max_browser_requests)
        self.browser_requests += 1

    def snapshot(self) -> dict:
        return {
            "elapsed_s": round(self.elapsed_s, 1),
            "search_calls": self.search_calls,
            "offer_calls": self.offer_calls,
            "browser_requests": self.browser_requests,
            "offers_total": self.offers_total,
        }
