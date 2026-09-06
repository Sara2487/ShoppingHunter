"""Settings and startup checks.

Everything tunable lives here and is read from the environment (prefix ``HUNTER_``) or ``.env``.
``check_environment`` fails fast on the two things that silently break the app: pydantic v1 on the
path and a missing OpenAI key (unless running with the fixture adapter).
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HUNTER_", env_file=".env", extra="ignore")

    # LLM
    model: str = "gpt-4.1-mini"
    tracing: bool = False
    log_level: str = "INFO"
    fake_llm: bool = False      # deterministic runner instead of OpenAI (no key needed)
    fake_browser: bool = False  # fixture adapter instead of Playwright

    # Destination
    country: str = "JO"
    currency: str = "JOD"
    language: str = "en"

    # Browser
    headless: bool = False
    profile_dir: Path = Path("./data/profile")
    data_dir: Path = Path("./data")
    # None = try installed Chrome, then Edge, then Playwright's bundled Chromium.
    # Set explicitly ("chrome", "msedge", "chromium") to pin one.
    browser_channel: str | None = None
    global_concurrency: int = 3
    per_domain_concurrency: int = 1
    min_domain_delay_s: float = 2.0
    max_domain_delay_s: float = 4.0
    page_timeout_s: float = 30.0
    needs_human_timeout_s: float = 300.0

    # Cache TTLs (seconds)
    search_cache_ttl_s: int = 3600
    offer_cache_ttl_s: int = 6 * 3600
    negative_cache_ttl_s: int = 300
    fx_cache_ttl_s: int = 24 * 3600

    # Cart
    cart_price_change_threshold: Decimal = Decimal("0.05")  # 5 %

    # Web
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])

    @property
    def openai_api_key(self) -> str | None:
        return os.environ.get("OPENAI_API_KEY")


class StartupError(RuntimeError):
    pass


def check_environment(settings: Settings, *, require_api_key: bool = True) -> None:
    import pydantic

    if not pydantic.VERSION.startswith("2."):
        raise StartupError(
            f"pydantic {pydantic.VERSION} detected; Shopping Hunter needs pydantic 2.x. "
            "Run inside the project venv (.venv)."
        )
    if require_api_key and not settings.openai_api_key:
        raise StartupError("OPENAI_API_KEY is not set (put it in .env or the environment).")
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        raise StartupError("The web UI is local-only; HUNTER_HOST must be 127.0.0.1 or localhost.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        from dotenv import load_dotenv

        load_dotenv()
        _settings = Settings()
    return _settings
