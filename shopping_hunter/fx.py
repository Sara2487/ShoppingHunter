"""Currency conversion with a cached rate table and peg fallbacks.

Rates are USD-based from open.er-api.com, cached on disk for ``fx_cache_ttl_s``. JOD, AED and
SAR are all USD-pegged, so the hardcoded fallback is accurate to well under 1 % and lets the app
work offline. Every quote records its source and timestamp so scores are reproducible.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

from .logging import log_event

PEGS_PER_USD: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "JOD": Decimal("0.709"),
    "AED": Decimal("3.6725"),
    "SAR": Decimal("3.75"),
}
FX_URL = "https://open.er-api.com/v6/latest/USD"
_DECIMALS = {"JOD": 3}


@dataclass(frozen=True)
class FxQuote:
    rate: Decimal  # multiply source amount by this to get target amount
    source: str  # "open.er-api.com" | "cache" | "peg"
    at: datetime


def quantize(amount: Decimal, currency: str) -> Decimal:
    places = _DECIMALS.get(currency.upper(), 2)
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


class FxRates:
    def __init__(self, data_dir: Path | None, ttl_s: int = 24 * 3600):
        self._cache_path = (data_dir / "fx_rates.json") if data_dir else None
        self._ttl = timedelta(seconds=ttl_s)
        self._per_usd: dict[str, Decimal] = dict(PEGS_PER_USD)
        self._source = "peg"
        self._at = datetime.now(timezone.utc)

    # -- loading ------------------------------------------------------------
    async def load(self, client: httpx.AsyncClient | None = None) -> None:
        if self._load_cache():
            return
        try:
            own = client is None
            client = client or httpx.AsyncClient(timeout=10)
            try:
                r = await client.get(FX_URL)
                r.raise_for_status()
                data = r.json()
            finally:
                if own:
                    await client.aclose()
            rates = data.get("rates") or {}
            per_usd = {k.upper(): Decimal(str(v)) for k, v in rates.items() if k.upper() in PEGS_PER_USD}
            if len(per_usd) < len(PEGS_PER_USD):
                raise ValueError("incomplete rate table")
            self._per_usd = per_usd
            self._source = "open.er-api.com"
            self._at = datetime.now(timezone.utc)
            self._save_cache()
            log_event("fx_loaded", source=self._source)
        except Exception as e:  # noqa: BLE001 - fall back to pegs on any failure
            log_event("fx_fallback_peg", error=str(e)[:200])
            self._per_usd = dict(PEGS_PER_USD)
            self._source = "peg"
            self._at = datetime.now(timezone.utc)

    def _load_cache(self) -> bool:
        if not self._cache_path or not self._cache_path.exists():
            return False
        try:
            raw = json.loads(self._cache_path.read_text("utf-8"))
            at = datetime.fromisoformat(raw["at"])
            if datetime.now(timezone.utc) - at > self._ttl:
                return False
            self._per_usd = {k: Decimal(v) for k, v in raw["per_usd"].items()}
            self._source = "cache:" + raw.get("source", "?")
            self._at = at
            return True
        except Exception:  # noqa: BLE001
            return False

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(
            json.dumps({"at": self._at.isoformat(), "source": self._source,
                        "per_usd": {k: str(v) for k, v in self._per_usd.items()}}),
            "utf-8",
        )

    # -- quoting ------------------------------------------------------------
    def quote(self, from_ccy: str, to_ccy: str) -> FxQuote:
        f, t = from_ccy.upper(), to_ccy.upper()
        if f not in self._per_usd or t not in self._per_usd:
            raise KeyError(f"no rate for {f}->{t}")
        return FxQuote(rate=self._per_usd[t] / self._per_usd[f], source=self._source, at=self._at)

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str) -> tuple[Decimal, FxQuote]:
        q = self.quote(from_ccy, to_ccy)
        return quantize(amount * q.rate, to_ccy), q
