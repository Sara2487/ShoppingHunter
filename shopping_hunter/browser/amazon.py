"""Amazon marketplace adapter over the shared BrowserSession.

Reads happen through small JS extractors that return text for known selectors; all
interpretation is in ``parse.py``. Nothing here returns HTML. ``add_to_cart`` is called only by
``cart.CartService`` after an explicit user click.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import Page

from ..cache import Cache, cache_key
from ..config import Settings
from ..logging import log_event
from ..models import (
    ASIN_RE, MARKETPLACE_CURRENCY, Candidate, Diagnostic, Money, Offer, OfferFetchResult, OfferProvenance,
    now_utc, offer_ref,
)
from ..normalize import clean_text, normalize_query

# Statuses worth remembering. Robot checks, timeouts, login/location problems are never cached.
_CACHE_OK = {"ok"}
_CACHE_NEGATIVE = {"not_found", "not_available", "does_not_ship", "parse_error"}
from . import parse as P
from .base import CartResult, LoginStatus
from .selectors import AOD_PATH, LANGUAGE_PARAM, selectors_for
from .session import BrowserSession, NeedsHuman, RobotCheck

COUNTRY_NAMES = {"JO": ("Jordan", "الأردن", "JO")}

# One evaluation returns the text of the first matching selector for each key.
_EXTRACT_JS = """
(spec) => {
  const out = {};
  for (const [key, sels] of Object.entries(spec)) {
    out[key] = null;
    for (const sel of sels) {
      try {
        const el = document.querySelector(sel);
        if (el) { out[key] = (el.getAttribute('value') ?? el.innerText ?? el.textContent ?? '').trim(); if (out[key] !== '') break; }
      } catch (e) {}
    }
  }
  return out;
}
"""

_SEARCH_JS = """
(spec) => {
  const first = (root, sels) => { for (const s of sels) { try { const el = root.querySelector(s); if (el) { const t = (el.innerText || el.textContent || '').trim(); if (t) return t; } } catch(e){} } return null; };
  const has = (root, sels) => { for (const s of sels) { try { if (root.querySelector(s)) return true; } catch(e){} } return false; };
  const rows = [];
  let cards = [];
  for (const s of spec.search_result) { cards = Array.from(document.querySelectorAll(s)); if (cards.length) break; }
  for (const c of cards) {
    const asin = c.getAttribute('data-asin');
    if (!asin) continue;
    rows.push({
      asin,
      title: first(c, spec.search_title),
      brand: first(c, spec.search_brand),
      price: first(c, spec.search_price),
      rating: first(c, spec.search_rating),
      count: first(c, spec.search_count),
      sponsored: has(c, spec.search_sponsored),
      prime: has(c, spec.search_prime),
    });
  }
  return rows;
}
"""

_AOD_JS = """
(spec) => {
  const first = (root, sels) => { for (const s of sels) { try { const el = root.querySelector(s); if (el) { const t = (el.getAttribute && el.getAttribute('value')) || el.innerText || el.textContent || ''; if (t.trim()) return t.trim(); } } catch(e){} } return null; };
  const row = (r) => ({
      price: first(r, spec.aod_price),
      seller: first(r, spec.aod_seller),
      condition: first(r, spec.aod_condition),
      delivery: first(r, spec.aod_delivery),
      shipping: first(r, spec.aod_shipping),
      offer_id: first(r, spec.aod_offer_id),
  });
  let pinned = null;
  for (const s of spec.aod_pinned) { const el = document.querySelector(s); if (el) { pinned = row(el); break; } }
  let rows = [];
  for (const s of spec.aod_offer) { const els = Array.from(document.querySelectorAll(s)); if (els.length) { rows = els.map(row); break; } }
  return { pinned, rows };
}
"""


class AmazonAdapter:
    def __init__(self, session: BrowserSession, marketplace: str, settings: Settings, cache: Cache | None = None):
        self.session = session
        self.marketplace = marketplace
        self.settings = settings
        self.cache = cache
        # Account-context hash: session characteristics that change what a page shows (no PII).
        self.account_ctx = cache_key(settings.country, settings.language)
        self.domain = f"www.{marketplace}"
        self.ccy = MARKETPLACE_CURRENCY[marketplace]
        self.sel = selectors_for(marketplace)
        self.lang = LANGUAGE_PARAM[marketplace]
        self.country_names = COUNTRY_NAMES.get(settings.country, (settings.country,))

    # -- helpers ---------------------------------------------------------------
    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"https://{self.domain}{path}{sep}language={self.lang}"

    async def _extract(self, page: Page, keys: list[str]) -> dict[str, str | None]:
        spec = {k: self.sel[k] for k in keys}
        return await page.evaluate(_EXTRACT_JS, spec)

    async def _goto_with_human(self, page: Page, url: str, run_id: str | None) -> None:
        try:
            await self.session.goto(page, url, run_id=run_id)
        except RobotCheck as e:
            await self.session.wait_for_human(page, self.marketplace, run_id=run_id, reason=str(e))
            await self.session.goto(page, url, run_id=run_id)

    # -- deliver-to ------------------------------------------------------------
    async def glow_text(self, page: Page) -> str:
        d = await self._extract(page, ["glow_line"])
        return d.get("glow_line") or ""

    async def ensure_deliver_to(self, page: Page, run_id: str | None = None) -> bool:
        """Make sure the page's delivery location is the configured country. Returns True if it is."""
        glow = await self.glow_text(page)
        if any(c.lower() in glow.lower() for c in self.country_names):
            return True
        log_event("deliver_to_fix", run_id=run_id, marketplace=self.marketplace, glow=clean_text(glow, 80))
        try:
            link = page.locator(", ".join(self.sel["glow_link"])).first
            await link.click(timeout=5000)
            await page.wait_for_timeout(1200)
            # Address book (logged in): pick the address that mentions the country.
            addr = page.locator(", ".join(self.sel["glux_address_list"]))
            if await addr.count():
                for c in self.country_names:
                    opt = addr.locator(f"[data-addr-id]:has-text('{c}'), li:has-text('{c}'), .a-declarative:has-text('{c}')").first
                    if await opt.count():
                        await opt.click(timeout=5000)
                        await page.wait_for_load_state("domcontentloaded")
                        glow = await self.glow_text(page)
                        return any(c2.lower() in glow.lower() for c2 in self.country_names)
            # Country selector (logged out or international).
            dd = page.locator(", ".join(self.sel["glux_country_dropdown"])).first
            if await dd.count():
                await dd.click(timeout=5000)
                await page.wait_for_timeout(500)
                opt = page.locator(f"a.a-dropdown-link:has-text('{self.country_names[0]}'), li:has-text('{self.country_names[0]}') a").first
                if await opt.count():
                    await opt.click(timeout=5000)
            sel = page.locator(", ".join(self.sel["glux_country_select"])).first
            if await sel.count():
                try:
                    await sel.select_option(value=self.settings.country, timeout=3000)
                except Exception:  # noqa: BLE001
                    await sel.select_option(label=self.country_names[0], timeout=3000)
            done = page.locator(", ".join(self.sel["glux_done"])).first
            if await done.count():
                await done.click(timeout=5000)
            await page.wait_for_timeout(1500)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            glow = await self.glow_text(page)
            ok = any(c.lower() in glow.lower() for c in self.country_names)
            log_event("deliver_to_result", run_id=run_id, marketplace=self.marketplace, ok=ok, glow=clean_text(glow, 80))
            return ok
        except Exception as e:  # noqa: BLE001
            log_event("deliver_to_error", run_id=run_id, marketplace=self.marketplace, error=str(e)[:200])
            return False

    # -- search ----------------------------------------------------------------
    async def search(self, query: str, page_no: int, *, run_id: str) -> list[Candidate]:
        key = cache_key("search", self.marketplace, normalize_query(query), self.settings.country, self.lang, page_no)
        if self.cache:
            hit = await self.cache.get("search", key)
            if hit:
                log_event("cache_hit", run_id=run_id, kind="search", marketplace=self.marketplace, age_s=int((now_utc() - hit[1]).total_seconds()))
                return [Candidate.model_validate(d) for d in hit[0]["candidates"]]
        out = await self._search_live(query, page_no, run_id)
        if self.cache:
            await self.cache.set("search", key, {"candidates": [c.model_dump(mode="json") for c in out]},
                                 self.settings.search_cache_ttl_s)
        return out

    async def _search_live(self, query: str, page_no: int, run_id: str) -> list[Candidate]:
        url = self._url(f"/s?k={quote_plus(query)}&page={page_no}")
        async with self.session.page(self.domain) as page:
            await self._goto_with_human(page, url, run_id)
            await self.ensure_deliver_to(page, run_id)
            try:
                await page.wait_for_selector(self.sel["search_result"][0], timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            rows: list[dict[str, Any]] = await page.evaluate(_SEARCH_JS, self.sel)
        out: list[Candidate] = []
        for r in rows:
            asin = (r.get("asin") or "").strip()
            if not ASIN_RE.match(asin) or not r.get("title"):
                continue
            title = clean_text(r["title"], 300)
            brand = clean_text(r.get("brand"), 60)
            if brand and brand.lower() not in title.lower() and len(brand) < 40:
                title = clean_text(f"{brand} {title}", 300)
            out.append(Candidate.build(
                self.marketplace, asin,
                title=title,
                listed_price=P.parse_price(r.get("price"), self.ccy),
                rating=P.parse_rating(r.get("rating")),
                ratings_count=P.parse_count(r.get("count")),
                sponsored=bool(r.get("sponsored")),
            ))
        return out

    # -- product / offers ------------------------------------------------------
    async def fetch_offers(self, asin: str, *, include_other_sellers: bool, max_rows: int, run_id: str,
                           bypass_cache: bool = False) -> OfferFetchResult:
        key = cache_key("offer", self.marketplace, asin, self.settings.country, self.account_ctx, include_other_sellers)
        if self.cache and not bypass_cache:
            hit = await self.cache.get("offer", key)
            if hit:
                res = OfferFetchResult.model_validate(hit[0])
                now = now_utc()
                for o in res.offers:  # re-admit into this run; keep the original fetch time
                    o.provenance = o.provenance.model_copy(update={"source": "cache", "admitted_at": now, "run_id": run_id})
                log_event("cache_hit", run_id=run_id, kind="offer", ref=res.candidate_ref, status=res.status,
                          age_s=int((now - hit[1]).total_seconds()))
                return res
        res = await self._fetch_offers_live(asin, include_other_sellers=include_other_sellers, max_rows=max_rows, run_id=run_id)
        if self.cache:
            if res.status in _CACHE_OK:
                await self.cache.set("offer", key, res.model_dump(mode="json"), self.settings.offer_cache_ttl_s)
            elif res.status in _CACHE_NEGATIVE:
                await self.cache.set("offer", key, res.model_dump(mode="json"), self.settings.negative_cache_ttl_s)
        return res

    async def _fetch_offers_live(self, asin: str, *, include_other_sellers: bool, max_rows: int, run_id: str) -> OfferFetchResult:
        ref = f"{self.marketplace}:{asin}"
        try:
            async with self.session.page(self.domain) as page:
                await self._goto_with_human(page, self._url(f"/dp/{asin}?th=1&psc=1"), run_id)
                loc_ok = await self.ensure_deliver_to(page, run_id)
                d = await self._extract(page, [
                    "title", "price", "availability", "delivery_block", "import_fees", "seller", "rating", "count",
                    "asin_input", "parent_asin", "condition", "add_to_cart", "glow_line", "account_greeting",
                ])
                if not d.get("title"):
                    if "/dp/" not in page.url and "/gp/" not in page.url:
                        return OfferFetchResult(candidate_ref=ref, status="not_found",
                                                diagnostic=Diagnostic(code="NO_PRODUCT_PAGE", detail=page.url[:120]))
                    return OfferFetchResult(candidate_ref=ref, status="parse_error",
                                            diagnostic=Diagnostic(field="title", code="TITLE_MISSING"))
                offers = [self._buybox_offer(asin, d, loc_ok, run_id)]
                if include_other_sellers:
                    try:
                        await self._goto_with_human(page, self._url(AOD_PATH.format(asin=asin)), run_id)
                        try:
                            await page.wait_for_selector(", ".join(self.sel["aod_container"]), timeout=8000)
                        except Exception:  # noqa: BLE001
                            pass
                        aod = await page.evaluate(_AOD_JS, self.sel)
                        pinned = aod.get("pinned") or {}
                        if pinned.get("offer_id") and offers[0].offer_id is None:
                            offers[0].parse_warnings.append("buybox_offer_listing_id_seen")
                        offers += self._aod_offers(asin, (aod.get("rows") or [])[:max_rows], offers[0], loc_ok, run_id)
                    except NeedsHuman:
                        raise
                    except Exception as e:  # noqa: BLE001
                        offers[0].parse_warnings.append(f"aod_failed:{type(e).__name__}")
                if offers[0].price is None and offers[0].in_stock is False:
                    return OfferFetchResult(candidate_ref=ref, status="not_available", offers=offers)
                if not loc_ok and offers[0].ships_to_country == "unknown":
                    return OfferFetchResult(candidate_ref=ref, status="location_not_set", offers=offers,
                                            diagnostic=Diagnostic(code="DELIVER_TO_NOT_SET"))
                return OfferFetchResult(candidate_ref=ref, status="ok", offers=offers)
        except NeedsHuman as e:
            return OfferFetchResult(candidate_ref=ref, status="needs_human", diagnostic=Diagnostic(code="ROBOT_CHECK", detail=str(e)[:120]))
        except RobotCheck as e:
            return OfferFetchResult(candidate_ref=ref, status="rate_limited", diagnostic=Diagnostic(code="ROBOT_CHECK", detail=str(e)[:120]))

    def _buybox_offer(self, asin: str, d: dict[str, str | None], loc_ok: bool, run_id: str) -> Offer:
        today = date.today()
        warnings: list[str] = []
        price = P.parse_price(d.get("price"), self.ccy)
        if price is None:
            warnings.append("price_unrecognized")
        delivery_text = d.get("delivery_block") or ""
        fees_text = d.get("import_fees") or ""
        shipping, fees, combined, w = P.parse_shipping_and_fees(delivery_text, fees_text, self.ccy)
        warnings += w
        days = P.parse_delivery_days(P.primary_delivery_line(delivery_text), today)
        in_stock = P.parse_availability(d.get("availability"))
        if in_stock is None and d.get("add_to_cart"):
            in_stock = True
        ships, evidence = P.parse_ships_to(delivery_text, d.get("glow_line") or "", self.country_names, days)
        if not loc_ok and ships == "yes":
            ships, evidence = "unknown", (evidence or "") + " [deliver-to not confirmed]"
        from ..normalize import condition_from_title
        cond = condition_from_title(d.get("title") or "")
        if cond == "new" and d.get("condition"):
            parsed = P.parse_condition(d.get("condition"))
            if parsed in ("renewed", "used"):
                cond = parsed
        resolved = (d.get("asin_input") or asin).strip()
        if not ASIN_RE.match(resolved):
            resolved = asin
        now = now_utc()
        return Offer(
            offer_ref=offer_ref(self.marketplace, asin, None), marketplace=self.marketplace,  # type: ignore[arg-type]
            requested_asin=asin, resolved_asin=resolved,
            parent_asin=(d.get("parent_asin") or None), offer_id=None,
            seller=clean_text(d.get("seller"), 80) or None, condition=cond, in_stock=in_stock,
            ships_to_country=ships, shipping_evidence=clean_text(evidence, 200) or None,
            price=price, shipping=shipping, import_fees=fees, fees_included_in_shipping=combined,
            delivery_days=days, is_prime=False, title=clean_text(d.get("title"), 300),
            rating=P.parse_rating(d.get("rating")), ratings_count=P.parse_count(d.get("count")),
            parse_warnings=warnings,
            provenance=OfferProvenance(source="network", fetched_at=now, admitted_at=now, run_id=run_id,
                                       parser_version=P.PARSER_VERSION),
            url=f"https://{self.domain}/dp/{asin}",
        )

    def _aod_offers(self, asin: str, rows: list[dict[str, Any]], buybox: Offer, loc_ok: bool, run_id: str) -> list[Offer]:
        today = date.today()
        out: list[Offer] = []
        for r in rows:
            oid = (r.get("offer_id") or "").strip() or None
            if oid is None:
                continue
            price = P.parse_price(r.get("price"), self.ccy)
            dtext = r.get("delivery") or ""
            shipping, fees, combined, w = P.parse_shipping_and_fees(dtext, r.get("shipping") or "", self.ccy)
            days = P.parse_delivery_days(P.primary_delivery_line(dtext), today)
            ships, evidence = P.parse_ships_to(dtext, "", self.country_names, days)
            if ships == "unknown" and days is not None and loc_ok:
                ships, evidence = "yes", f"{dtext[:150]} [deliver-to confirmed on page]"
            now = now_utc()
            out.append(Offer(
                offer_ref=offer_ref(self.marketplace, asin, oid), marketplace=self.marketplace,  # type: ignore[arg-type]
                requested_asin=asin, resolved_asin=buybox.resolved_asin, parent_asin=buybox.parent_asin, offer_id=oid,
                seller=clean_text(r.get("seller"), 80) or None, condition=P.parse_condition(r.get("condition")),
                in_stock=True if price is not None else None, ships_to_country=ships,
                shipping_evidence=clean_text(evidence, 200) or None, price=price, shipping=shipping,
                import_fees=fees, fees_included_in_shipping=combined, delivery_days=days, title=buybox.title,
                rating=buybox.rating, ratings_count=buybox.ratings_count, parse_warnings=w,
                provenance=OfferProvenance(source="network", fetched_at=now, admitted_at=now, run_id=run_id,
                                           parser_version=P.PARSER_VERSION),
                url=buybox.url,
            ))
        # Drop the AOD row that duplicates the buy box (same seller & price).
        return [o for o in out if not (o.seller == buybox.seller and o.price == buybox.price)]

    # -- status / login --------------------------------------------------------
    async def login_status(self) -> LoginStatus:
        try:
            async with self.session.page(self.domain) as page:
                await self.session.goto(page, self._url("/"))
                d = await self._extract(page, ["account_greeting", "glow_line"])
                greeting = (d.get("account_greeting") or "").lower()
                logged_in = bool(greeting) and "sign in" not in greeting and "تسجيل الدخول" not in greeting
                glow = d.get("glow_line") or ""
                ok = any(c.lower() in glow.lower() for c in self.country_names)
                return LoginStatus(marketplace=self.marketplace, logged_in=logged_in, deliver_to_ok=ok,
                                   detail=clean_text(glow, 80) or None)
        except RobotCheck:
            return LoginStatus(marketplace=self.marketplace, detail="robot check")
        except Exception as e:  # noqa: BLE001
            return LoginStatus(marketplace=self.marketplace, detail=f"error: {type(e).__name__}")

    def signin_url(self) -> str:
        return f"https://{self.domain}/ap/signin?openid.return_to=https%3A%2F%2F{self.domain}%2F&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=usflex&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0"

    # -- cart (CartService only) ---------------------------------------------
    async def add_to_cart(self, asin: str, offer_id: str | None) -> CartResult:
        async with self.session.page(self.domain) as page:
            try:
                if offer_id:
                    await self._goto_with_human(page, self._url(AOD_PATH.format(asin=asin)), None)
                    try:
                        await page.wait_for_selector(", ".join(self.sel["aod_container"]), timeout=10000)
                    except Exception:  # noqa: BLE001
                        pass
                    safe = offer_id.replace('"', "")
                    row = page.locator(f'input[name$="[offerListingId]"][value="{safe}"], input[name="offeringID.1"][value="{safe}"]').first
                    if not await row.count():
                        return CartResult(status="failed", detail="offer no longer listed")
                    form = row.locator("xpath=ancestor::form[1]")
                    btn = form.locator(", ".join(self.sel["aod_atc"])).first
                else:
                    await self._goto_with_human(page, self._url(f"/dp/{asin}?th=1&psc=1"), None)
                    btn = page.locator(", ".join(self.sel["add_to_cart"])).first
                if not await btn.count():
                    return CartResult(status="failed", detail="add-to-cart button not found")
                before = P.parse_count((await self._extract(page, ["cart_count"])).get("cart_count")) or 0
                await btn.click()
                try:
                    await page.wait_for_selector(", ".join(self.sel["atc_confirm"]), timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                await page.wait_for_timeout(1000)
                after = P.parse_count((await self._extract(page, ["cart_count"])).get("cart_count"))
                confirmed = (await page.locator(", ".join(self.sel["atc_confirm"])).count() > 0
                             or await page.locator(", ".join(self.sel["aod_added"])).count() > 0)
                if confirmed or (after is not None and after > before):
                    return CartResult(status="added", cart_url=f"https://{self.domain}/cart", cart_count=after)
                return CartResult(status="failed", detail="no confirmation seen", cart_count=after)
            except NeedsHuman:
                return CartResult(status="failed", detail="robot check not cleared")
            except RobotCheck:
                return CartResult(status="failed", detail="robot check")


def make_adapter_factory(session: BrowserSession, settings: Settings, cache: Cache | None = None):
    adapters: dict[str, AmazonAdapter] = {}

    def factory(marketplace: str) -> AmazonAdapter:
        if marketplace not in adapters:
            adapters[marketplace] = AmazonAdapter(session, marketplace, settings, cache)
        return adapters[marketplace]

    return factory


def money_or_none(v: str | None, ccy: str) -> Money | None:
    return P.parse_price(v, ccy)
