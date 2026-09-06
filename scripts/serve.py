"""Start the local web UI with one worker.

    python scripts/serve.py                      # real browser + OpenAI (needs .env with OPENAI_API_KEY)
    python scripts/serve.py --fake-llm           # real browser, deterministic matcher, no key needed
    python scripts/serve.py --fake-llm --fake-browser   # fully offline demo with fixture data
    python scripts/serve.py --headless
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--fake-llm", action="store_true")
    p.add_argument("--fake-browser", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--port", type=int)
    a = p.parse_args()
    if a.fake_llm:
        os.environ["HUNTER_FAKE_LLM"] = "true"
    if a.fake_browser:
        os.environ["HUNTER_FAKE_BROWSER"] = "true"
    if a.headless:
        os.environ["HUNTER_HEADLESS"] = "true"
    if a.port:
        os.environ["HUNTER_PORT"] = str(a.port)
    from shopping_hunter.web.app import main as serve
    serve()


if __name__ == "__main__":
    main()
