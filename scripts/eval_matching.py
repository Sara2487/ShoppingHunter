"""Matching evaluation: run the matcher (real LLM or fake) over labelled titles, report precision/recall.

    python scripts/eval_matching.py --fake       # deterministic baseline, no key
    python scripts/eval_matching.py              # real model from HUNTER_MODEL

For a strict-model hunter false positives are worse than false negatives: watch precision first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopping_hunter.agents import build_matcher  # noqa: E402
from shopping_hunter.budgets import BudgetTracker, RunBudgets  # noqa: E402
from shopping_hunter.config import check_environment, get_settings  # noqa: E402
from shopping_hunter.context import HuntContext  # noqa: E402
from shopping_hunter.models import HuntRequest, MatchDecisions  # noqa: E402
from shopping_hunter.normalize import prefilter  # noqa: E402
from shopping_hunter.runners import make_runner  # noqa: E402

EVAL = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "matching_eval.json"


async def main(fake: bool) -> int:
    settings = get_settings()
    check_environment(settings, require_api_key=not fake)
    data = json.loads(EVAL.read_text("utf-8"))
    req = HuntRequest(query=data["query"])
    ctx = HuntContext(run_id=uuid.uuid4().hex[:12], request=req, settings=settings,
                      tracker=BudgetTracker(RunBudgets()), adapter_for=lambda m: None)
    runner = make_runner(settings, fake=fake)

    decisions: dict[str, bool] = {}
    pending = []
    for c in data["cases"]:
        pre = prefilter(data["query"], c["title"])
        if pre:
            decisions[c["ref"]] = False
        else:
            pending.append({"ref": c["ref"], "title": c["title"], "price": None, "rating": None, "ratings_count": None, "sponsored": False})
    payload = {"query": data["query"], "canonical_brand": data["canonical_brand"],
               "canonical_model": data["canonical_model"], "candidates": pending}
    out: MatchDecisions = await runner.run(build_matcher(settings), json.dumps(payload), ctx, max_turns=2)
    for d in out.decisions:
        decisions[d.candidate_ref] = bool(d.matches and d.confidence != "low")

    tp = fp = fn = tn = 0
    for c in data["cases"]:
        got = decisions.get(c["ref"], False)
        exp = c["expect"]
        tag = "ok " if got == exp else "BAD"
        if got and exp: tp += 1
        elif got and not exp: fp += 1
        elif not got and exp: fn += 1
        else: tn += 1
        print(f"{tag} expect={str(exp):5} got={str(got):5}  {c['note']:<38} {c['title'][:70]}")
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    print(f"\nprecision={prec:.2f} recall={rec:.2f}  tp={tp} fp={fp} fn={fn} tn={tn}  ({'fake' if fake else settings.model})")
    return 0 if fp == 0 else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--fake", action="store_true")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.fake)))
