"""Command-line entry point.

    python -m shopping_hunter.cli "Logitech MX Master 3S" --fake
    python -m shopping_hunter.cli "Logitech MX Master 3S" --marketplaces amazon.ae amazon.sa --depth thorough
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from decimal import Decimal

from .budgets import BudgetTracker, RunBudgets
from .config import StartupError, check_environment, get_settings
from .context import HuntContext
from .duties import RULES_BY_COUNTRY
from .fx import FxRates
from .logging import setup_logging
from .models import MARKETPLACES, HuntRequest, HuntResult
from .orchestrator import run_hunt
from .runners import make_runner


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, Decimal):
        return f"{v:.3f}"
    return str(v)


def print_result(r: HuntResult) -> None:
    print(f"\n== {r.query} ==  status={r.status}" + (f" ({r.status_reason})" if r.status_reason else ""))
    print(f"run_id={r.run_id}")
    if r.ranked:
        print("\nRANKED (landed cost, JOD):")
        hdr = f"{'#':>2} {'marketplace':<11} {'landed':>9} {'price':>9} {'ship':>7} {'fees':>7} {'src':<8} {'days':>4} {'rating':>6} {'seller':<16} title"
        print(hdr)
        for i, s in enumerate(r.ranked, 1):
            print(f"{i:>2} {s.marketplace:<11} {_fmt(s.landed_jod):>9} {_fmt(s.price_jod):>9} {_fmt(s.shipping_jod):>7} "
                  f"{_fmt(s.fees_jod):>7} {s.fees_source:<8} {_fmt(s.delivery_days):>4} "
                  f"{_fmt(s.rating):>6} {(s.seller or '-')[:16]:<16} {s.title[:50]}")
            print(f"   {s.url}")
    else:
        print("\nRANKED: none")
    if r.unconfirmed:
        print("\nUNCONFIRMED (matched, but shipping or cost not established):")
        for s in r.unconfirmed:
            print(f" - {s.offer_ref}: ships={s.ships_to_country} stock={s.in_stock} landed={_fmt(s.landed_jod)} fees={s.fees_source}")
    if r.excluded:
        print("\nEXCLUDED:")
        for e in r.excluded:
            print(f" - {e.marketplace}:{e.asin} [{e.reason_code}] {e.reason[:70]}  | {(e.title or '')[:45]}")
    c = r.coverage
    print("\nCOVERAGE:")
    print(f"  marketplaces attempted={c.marketplaces_attempted} completed={c.marketplaces_completed}")
    print(f"  queries={[(q.marketplace, q.query) for q in c.queries_used]}")
    print(f"  pages={c.pages_scanned} candidates before/after dedup={c.candidates_before_dedup}/{c.candidates_after_dedup} "
          f"truncated={c.truncated_by_cap} offers inspected={c.offers_inspected} skipped={c.offers_skipped}")
    if r.summary:
        print(f"\nSUMMARY:\n  {r.summary}")
    if r.caveats:
        print("\nCAVEATS:")
        for cv in r.caveats:
            print(f"  * {cv}")
    if r.validation_errors:
        print("\nVALIDATION ERRORS:")
        for v in r.validation_errors:
            print(f"  ! {v}")


async def amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings.log_level if not args.quiet else "WARNING")
    fake_llm = args.fake or args.fake_llm
    fake_browser = args.fake or args.fake_browser
    try:
        check_environment(settings, require_api_key=not fake_llm)
    except StartupError as e:
        print(f"startup error: {e}", file=sys.stderr)
        return 2

    request = HuntRequest(
        query=args.query, marketplaces=args.marketplaces or list(MARKETPLACES), min_rating=args.min_rating,
        min_ratings_count=args.min_ratings, budget_jod=Decimal(str(args.budget)) if args.budget else None,
        depth=args.depth, include_other_sellers=args.other_sellers,
        allowed_conditions=["new", "renewed", "used"] if args.allow_used else ["new"],
    )

    session = None
    cache = None
    if fake_browser:
        from .browser.fake import fake_adapter_factory as adapter_for
        fx = FxRates(None)  # pegs, no network
    else:
        from .browser.session import BrowserSession
        from .browser.amazon import make_adapter_factory
        from .cache import Cache
        session = BrowserSession(settings)
        await session.start()
        cache = Cache(settings.data_dir / "cache.sqlite")
        await cache.open()
        adapter_for = make_adapter_factory(session, settings, cache)
        fx = FxRates(settings.data_dir, settings.fx_cache_ttl_s)
        await fx.load()

    ctx = HuntContext(run_id=uuid.uuid4().hex[:12], request=request, settings=settings,
                      tracker=BudgetTracker(RunBudgets.for_depth(request.depth)), adapter_for=adapter_for)
    runner = make_runner(settings, fake=fake_llm)
    rules = RULES_BY_COUNTRY[settings.country]
    try:
        result = await run_hunt(ctx, runner, fx, rules)
    finally:
        if cache is not None:
            await cache.close()
        if session is not None:
            await session.close()

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print_result(result)
    return 0 if result.status in ("completed", "partial") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="shopping_hunter", description="Hunt a product on Amazon and rank by landed cost.")
    p.add_argument("query")
    p.add_argument("--fake", action="store_true", help="fixture adapter + fake LLM; no network, no key")
    p.add_argument("--fake-llm", action="store_true", help="real browser, deterministic fake LLM (no key needed)")
    p.add_argument("--fake-browser", action="store_true", help="fixture adapter, real LLM")
    p.add_argument("--marketplaces", nargs="+", choices=MARKETPLACES)
    p.add_argument("--depth", choices=["ordinary", "thorough"], default="ordinary")
    p.add_argument("--min-rating", type=float, default=4.0)
    p.add_argument("--min-ratings", type=int, default=50)
    p.add_argument("--budget", type=float, help="max landed cost in JOD")
    p.add_argument("--other-sellers", action="store_true")
    p.add_argument("--allow-used", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="suppress JSON event log")
    args = p.parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
