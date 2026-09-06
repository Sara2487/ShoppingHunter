"""Open the dedicated browser profile on a marketplace sign-in page so YOU can log in.

    python scripts/login.py amazon.com
    python scripts/login.py amazon.ae amazon.sa

The script never touches credentials. It owns the profile lock while running, so stop the web
app first (or use the "Open login window" button in the web UI instead, which reuses the app's
browser). Close the browser window or press Ctrl+C when you are signed in.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopping_hunter.browser.amazon import AmazonAdapter  # noqa: E402
from shopping_hunter.browser.session import BrowserSession  # noqa: E402
from shopping_hunter.config import StartupError, check_environment, get_settings  # noqa: E402
from shopping_hunter.logging import setup_logging  # noqa: E402
from shopping_hunter.models import MARKETPLACES  # noqa: E402


async def main(marketplaces: list[str]) -> int:
    settings = get_settings()
    setup_logging("WARNING")
    try:
        check_environment(settings, require_api_key=False)
    except StartupError as e:
        print(f"startup error: {e}")
        return 2
    settings.headless = False
    session = BrowserSession(settings)
    await session.start()
    try:
        for m in marketplaces:
            adapter = AmazonAdapter(session, m, settings)
            await session.open_for_user(adapter.signin_url())
            print(f"[{m}] sign-in page opened. Log in, then set your delivery address to {settings.country}.")
        print("\nWhen done on every tab, press Enter here (or close the browser).")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sys.stdin.readline)
        for m in marketplaces:
            st = await AmazonAdapter(session, m, settings).login_status()
            print(f"[{m}] logged_in={st.logged_in} deliver_to_ok={st.deliver_to_ok} ({st.detail})")
    finally:
        await session.close()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a in MARKETPLACES] or list(MARKETPLACES)
    sys.exit(asyncio.run(main(args)))
