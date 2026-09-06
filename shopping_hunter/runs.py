"""In-memory run registry: hunts as jobs with states, an event log for SSE, and cart tokens.

States: running -> needs_human -> running ... -> completed | partial | failed | cancelled.
One hunt at a time (the browser profile is single-owner and Amazon rate limits are per account).
Offer tokens are server-generated, single-use, and the only way the cart route can name an offer.
"""
from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable

from .budgets import BudgetTracker, RunBudgets
from .config import Settings
from .context import HuntContext
from .duties import DutyRules
from .fx import FxRates
from .models import HuntRequest, HuntResult, now_utc
from .orchestrator import run_hunt
from .runners import AgentRunner


class RunBusy(RuntimeError):
    pass


@dataclass
class RunRecord:
    run_id: str
    request: HuntRequest
    state: str = "running"
    created_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None
    ctx: HuntContext | None = None
    task: asyncio.Task | None = None
    pump: asyncio.Task | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: HuntResult | None = None
    error: str | None = None
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "state": self.state, "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "query": self.request.query, "events": len(self.events), "error": self.error,
        }


class RunRegistry:
    def __init__(self, settings: Settings, max_history: int = 20):
        self.settings = settings
        self.max_history = max_history
        self.runs: dict[str, RunRecord] = {}
        self.order: list[str] = []
        self._tokens: dict[str, tuple[str, str]] = {}  # token -> (run_id, offer_ref)
        self._used_tokens: set[str] = set()

    # -- lifecycle -------------------------------------------------------------
    @property
    def active(self) -> RunRecord | None:
        for rid in reversed(self.order):
            r = self.runs[rid]
            if r.state in ("running", "needs_human"):
                return r
        return None

    async def start(self, request: HuntRequest, adapter_for: Callable[[str], Any], runner: AgentRunner,
                    fx: FxRates, rules: DutyRules) -> RunRecord:
        if self.active is not None:
            raise RunBusy("a hunt is already running")
        run_id = uuid.uuid4().hex[:12]
        ctx = HuntContext(run_id=run_id, request=request, settings=self.settings,
                          tracker=BudgetTracker(RunBudgets.for_depth(request.depth)), adapter_for=adapter_for)
        rec = RunRecord(run_id=run_id, request=request, ctx=ctx)
        self.runs[run_id] = rec
        self.order.append(run_id)
        self._trim()
        rec.task = asyncio.create_task(self._run(rec, runner, fx, rules), name=f"hunt-{run_id}")
        rec.pump = asyncio.create_task(self._pump(rec), name=f"pump-{run_id}")
        return rec

    async def _run(self, rec: RunRecord, runner: AgentRunner, fx: FxRates, rules: DutyRules) -> None:
        assert rec.ctx
        try:
            result = await run_hunt(rec.ctx, runner, fx, rules)
            rec.result = result
            rec.state = result.status
            for s in result.ranked + result.unconfirmed:
                s.offer_token = self.issue_token(rec.run_id, s.offer_ref)
        except asyncio.CancelledError:
            rec.state = "cancelled"
        except Exception as e:  # noqa: BLE001
            rec.state, rec.error = "failed", f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            rec.finished_at = now_utc()

    async def _pump(self, rec: RunRecord) -> None:
        assert rec.ctx
        q = rec.ctx.events
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if rec.task and rec.task.done() and q.empty():
                    break
                continue
            await self._append(rec, ev)
        await self._append(rec, {"event": "run_closed", "state": rec.state})

    async def _append(self, rec: RunRecord, ev: dict[str, Any]) -> None:
        ev = {"seq": len(rec.events) + 1, "ts": now_utc().isoformat(timespec="seconds"), **ev}
        name = ev.get("event")
        if name == "needs_human":
            rec.state = "needs_human"
        elif rec.state == "needs_human" and name in ("needs_human_cleared", "offer_fetch_finished", "search_finished"):
            rec.state = "running"
        rec.events.append(ev)
        if len(rec.events) > 2000:  # bounded: keep the head (request) and the tail
            rec.events = rec.events[:50] + rec.events[-1500:]
        async with rec.cond:
            rec.cond.notify_all()

    async def note_needs_human(self, marketplace: str, reason: str, run_id: str | None) -> None:
        rec = self.runs.get(run_id or "") or self.active
        if rec:
            await self._append(rec, {"event": "needs_human", "marketplace": marketplace, "reason": reason,
                                     "hint": "Solve the check in the Shopping Hunter browser window; the run resumes automatically."})

    async def cancel(self, run_id: str) -> bool:
        rec = self.runs.get(run_id)
        if not rec or not rec.ctx or rec.state not in ("running", "needs_human"):
            return False
        rec.ctx.cancel_event.set()
        await self._append(rec, {"event": "cancel_requested"})
        if rec.task:
            try:
                await asyncio.wait_for(asyncio.shield(rec.task), timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                rec.task.cancel()
        return True

    def get(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def _trim(self) -> None:
        while len(self.order) > self.max_history:
            rid = self.order.pop(0)
            rec = self.runs.pop(rid, None)
            if rec:
                for t, (r, _) in list(self._tokens.items()):
                    if r == rid:
                        self._tokens.pop(t, None)

    # -- events for SSE --------------------------------------------------------
    async def stream(self, run_id: str, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        rec = self.runs.get(run_id)
        if not rec:
            return
        seq = after_seq
        while True:
            pending = [e for e in rec.events if e["seq"] > seq]
            for e in pending:
                seq = e["seq"]
                yield e
            if pending and pending[-1].get("event") == "run_closed":
                return
            if rec.pump and rec.pump.done() and not pending:
                return
            async with rec.cond:
                try:
                    await asyncio.wait_for(rec.cond.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "keepalive", "seq": seq}

    # -- cart tokens -----------------------------------------------------------
    def issue_token(self, run_id: str, offer_ref: str) -> str:
        tok = secrets.token_urlsafe(18)
        self._tokens[tok] = (run_id, offer_ref)
        return tok

    def resolve_token(self, token: str) -> tuple[RunRecord, str]:
        if token in self._used_tokens:
            raise KeyError("token already used")
        pair = self._tokens.get(token)
        if not pair:
            raise KeyError("unknown token")
        rec = self.runs.get(pair[0])
        if not rec or not rec.ctx:
            raise KeyError("run expired")
        return rec, pair[1]

    def consume_token(self, token: str) -> None:
        self._used_tokens.add(token)
        self._tokens.pop(token, None)
