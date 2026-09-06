"""Deterministic parsers for Amazon text fragments. No browser, no LLM; fully unit-testable.

All money is Decimal. Arabic-Indic digits and Arabic currency markers are handled so .ae/.sa
pages that fall back to Arabic still parse. Anything ambiguous returns None and the caller
records a parse warning rather than guessing.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from ..models import Money, ShipsTo

PARSER_VERSION = "amazon-parse-1"

_ARABIC_INDIC = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_EASTERN_ARABIC = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}

_CCY_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"US\s?\$|\$|USD"), "USD"),
    (re.compile(r"AED|د\.إ|درهم"), "AED"),
    (re.compile(r"SAR|SR\b|ر\.س|ريال|﷼"), "SAR"),
    (re.compile(r"JOD|JD\b|د\.أ|دينار"), "JOD"),
]
_NUM_RE = re.compile(r"(\d{1,3}(?:[,\s]\d{3})+|\d+)(?:[.,](\d{1,3}))?")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTH_AR = {"يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "مايو": 5, "يونيو": 6,
             "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10,
             "نوفمبر": 11, "ديسمبر": 12}

CANNOT_SHIP_PATTERNS = re.compile(
    r"cannot be shipped to your selected delivery location|does not ship to|not eligible for international shipping"
    r"|can't be shipped to|cannot be delivered to|isn't available for delivery to|unavailable for delivery to"
    r"|لا يمكن شحن|لا يمكن توصيل", re.I)
FREE_DELIVERY_RE = re.compile(r"\bfree\b.*\b(delivery|shipping)\b|\b(delivery|shipping)\b.*\bfree\b|توصيل مجاني|شحن مجاني", re.I)
IMPORT_FEES_RE = re.compile(r"import fees?( deposit)?|رسوم الاستيراد", re.I)
SHIPPING_AND_FEES_RE = re.compile(r"shipping\s*&\s*import fees? deposit|shipping and import fees", re.I)
IN_STOCK_RE = re.compile(r"\bin stock\b|only \d+ left|usually (ships|dispatched)|ships within|متوفر|متبقي", re.I)
OUT_OF_STOCK_RE = re.compile(r"currently unavailable|temporarily out of stock|out of stock|غير متوفر|نفدت الكمية", re.I)
RENEWED_RE = re.compile(r"\brenewed\b|\brefurbished\b|مجدد", re.I)
USED_RE = re.compile(r"\bused\b|pre-owned|مستعمل", re.I)
DATE_RANGE_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:-|–|to)\s*)?(\d{1,2})?\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})?(?:\s*(?:-|–|to)\s*(?:([A-Za-z]{3,9})\.?\s*)?(\d{1,2}))?")


def normalize_digits(s: str) -> str:
    return s.translate(_ARABIC_INDIC).translate(_EASTERN_ARABIC)


def _to_decimal(m: re.Match[str]) -> Decimal | None:
    whole = re.sub(r"[,\s]", "", m.group(1))
    frac = m.group(2)
    try:
        return Decimal(f"{whole}.{frac}" if frac else whole)
    except InvalidOperation:
        return None


def detect_currency(text: str) -> str | None:
    for pat, ccy in _CCY_MARKERS:
        if pat.search(text):
            return ccy
    return None


def parse_price(text: str | None, default_ccy: str) -> Money | None:
    """First monetary amount in ``text``. Currency from markers, else ``default_ccy``."""
    if not text:
        return None
    t = normalize_digits(text)
    m = _NUM_RE.search(t)
    if not m:
        return None
    amt = _to_decimal(m)
    if amt is None:
        return None
    return Money(amount=amt, currency=detect_currency(t) or default_ccy)


def _last_price(text: str, default_ccy: str) -> Money | None:
    """Last monetary amount with a decimal part in ``text`` (the fee figure sits right before its label)."""
    amounts = parse_all_amounts(text, default_ccy)
    return amounts[-1] if amounts else None


def parse_all_amounts(text: str, default_ccy: str) -> list[Money]:
    t = normalize_digits(text or "")
    out = []
    for m in _NUM_RE.finditer(t):
        d = _to_decimal(m)
        if d is not None and m.group(2) is not None:  # require a decimal part to skip counts/dates
            out.append(Money(amount=d, currency=detect_currency(t) or default_ccy))
    return out


def parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    t = normalize_digits(text)
    m = re.search(r"(\d)[.,](\d)\s*(?:out of|من|/)\s*5", t) or re.search(r"^\s*(\d)[.,](\d)\b", t)
    if not m:
        return None
    v = float(f"{m.group(1)}.{m.group(2)}")
    return v if 0 <= v <= 5 else None


def parse_count(text: str | None) -> int | None:
    if not text:
        return None
    t = normalize_digits(text).replace("‏", "")
    k = re.search(r"(\d+(?:[.,]\d+)?)\s*[kK]\b", t)
    if k:
        return int(round(float(k.group(1).replace(",", ".")) * 1000))
    m = re.search(r"(\d{1,3}(?:[,.]\d{3})+|\d+)", t)
    if not m:
        return None
    return int(re.sub(r"[,.]", "", m.group(1)))


def parse_availability(text: str | None) -> bool | None:
    if not text:
        return None
    if OUT_OF_STOCK_RE.search(text):
        return False
    if IN_STOCK_RE.search(text):
        return True
    return None


def parse_condition(text: str | None) -> Literal["new", "renewed", "used", "unknown"]:
    if not text:
        return "unknown"
    if RENEWED_RE.search(text):
        return "renewed"
    if USED_RE.search(text):
        return "used"
    if re.search(r"\bnew\b|جديد", text, re.I):
        return "new"
    return "unknown"


def _month(tok: str) -> int | None:
    tok = tok.lower().strip(".")
    if tok in _MONTH_AR:
        return _MONTH_AR[tok]
    return MONTHS.get(tok[:3])


def parse_delivery_days(text: str | None, today: date) -> int | None:
    """Earliest delivery date in the text, as days from ``today``. Handles 'Sep 18', '18 Sep',
    '18 - 25 September', 'September 18 - 25', 'Tuesday, September 16', and 'Get it by Tomorrow'."""
    if not text:
        return None
    t = normalize_digits(text)
    if re.search(r"\btoday\b|اليوم", t, re.I):
        return 0
    if re.search(r"\btomorrow\b|غدا", t, re.I):
        return 1
    best: date | None = None
    pairs: list[tuple[str, str]] = []
    # "18 - 25 September": the first day of a range belongs to the month that follows the range.
    for m in re.finditer(r"(\d{1,2})\s*[-–]\s*\d{1,2}\s+([A-Za-z؀-ۿ]{3,10})", t):
        pairs.append((m.group(1), m.group(2)))
    for m in re.finditer(r"(\d{1,2})\s+([A-Za-z؀-ۿ]{3,10})|([A-Za-z؀-ۿ]{3,10})\.?\s+(\d{1,2})\b", t):
        pairs.append((m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3)))
    for day, mon in pairs:
        mi = _month(mon)
        if not mi:
            continue
        try:
            d = date(today.year, mi, int(day))
        except ValueError:
            continue
        if d < today - timedelta(days=7):  # wrapped into next year
            d = date(today.year + 1, mi, int(day))
        if best is None or d < best:
            best = d
    if best is None:
        return None
    return max(0, (best - today).days)


_COMBINED_RE = re.compile(
    r"^(?P<pre>[^\n]*?)[ \t]*shipping\s*(?:&|and)\s*import\s+(?:fees?(?:\s+deposit)?|charges?)(?P<post>[^\n]*)$", re.I | re.M)
_FEES_RE = re.compile(
    r"^(?P<pre>[^\n]*?)[ \t]*import\s+(?:fees?(?:\s+deposit)?|charges?)(?P<post>[^\n]*)$", re.I | re.M)
_SHIP_LINE_RE = re.compile(r"^([^\n]*?\d[^\n]*?)[ \t]*(?:delivery|shipping|توصيل|شحن)\b", re.I | re.M)


def _labelled_amount(m: re.Match[str], default_ccy: str) -> Money | None:
    """Amount on a line like '$36.21 Shipping & Import Charges to Jordan' or 'Import Fees Deposit: AED 45.00'."""
    return _last_price(m.group("pre"), default_ccy) or parse_price(m.group("post"), default_ccy)


def primary_delivery_line(text: str | None) -> str:
    """The standard-delivery line only; drops 'Or fastest delivery ...' upsells."""
    if not text:
        return ""
    first = text.strip().split("\n", 1)[0]
    return re.split(r"\bor fastest\b", first, flags=re.I)[0].strip()


def parse_shipping_and_fees(delivery_text: str, fees_text: str, default_ccy: str) -> tuple[Money | None, Money | None, bool, list[str]]:
    """Return (shipping, import_fees, combined_flag, warnings).

    Handles the three shapes Amazon uses for an international address:
      * delivery line "$15.08 delivery Sunday, September 20" + a combined "$36.21 Shipping & Import
        Charges to Jordan"  -> shipping 15.08, fees 21.13 (derived), combined=False
      * only a combined "$45.13 Shipping & Import Fees Deposit to Jordan" -> shipping 45.13, fees 0, combined=True
      * separate "AED 30.00 delivery ..." + "Import Fees Deposit: AED 45.00" -> 30.00 / 45.00
    """
    warnings: list[str] = []
    dtext = normalize_digits(delivery_text or "")
    ftext = normalize_digits(fees_text or "")
    joined = f"{dtext}\n{ftext}"

    combined_amt: Money | None = None
    m = _COMBINED_RE.search(joined)
    if m:
        combined_amt = _labelled_amount(m, default_ccy)

    fees_amt: Money | None = None
    for m in _FEES_RE.finditer(joined):
        if _COMBINED_RE.match(m.group(0)):
            continue
        fees_amt = _labelled_amount(m, default_ccy)
        if fees_amt:
            break

    shipping: Money | None = None
    if FREE_DELIVERY_RE.search(primary_delivery_line(dtext)):
        shipping = Money(amount=Decimal(0), currency=default_ccy)
    else:
        m = _SHIP_LINE_RE.search(dtext)
        if m and not _COMBINED_RE.match(m.group(0)):
            amt = parse_price(m.group(1), default_ccy)
            if amt and amt.amount < Decimal("100000"):
                shipping = amt

    if combined_amt is not None:
        if shipping is not None and shipping.amount <= combined_amt.amount and shipping.currency == combined_amt.currency:
            fees = Money(amount=combined_amt.amount - shipping.amount, currency=combined_amt.currency)
            warnings.append("fees_derived_from_combined_figure")
            return shipping, fees, False, warnings
        return combined_amt, Money(amount=Decimal(0), currency=combined_amt.currency), True, warnings

    if shipping is None:
        warnings.append("shipping_cost_unrecognized")
    return shipping, fees_amt, False, warnings


def parse_ships_to(delivery_text: str, glow_text: str, country_names: tuple[str, ...], delivery_days: int | None) -> tuple[ShipsTo, str | None]:
    """Tri-state shipping eligibility with the evidence string that justified it."""
    dt = delivery_text or ""
    if CANNOT_SHIP_PATTERNS.search(dt):
        m = CANNOT_SHIP_PATTERNS.search(dt)
        return "no", dt[max(0, m.start() - 40): m.end() + 40].strip()
    glow_ok = any(c.lower() in (glow_text or "").lower() for c in country_names)
    mentions_country = any(c.lower() in dt.lower() for c in country_names)
    if delivery_days is not None and (glow_ok or mentions_country):
        return "yes", dt[:200].strip()
    return "unknown", (dt[:200].strip() or None)
