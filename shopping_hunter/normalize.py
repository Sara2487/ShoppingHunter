"""Deterministic text normalisation and the strict-mode pre-filter.

Listing text is untrusted. Everything that reaches the LLM goes through ``clean_text`` (control
chars stripped, length capped). ``prefilter`` rejects obvious non-matches (accessories, renewed
items, parts) before an LLM turn is spent; it is deliberately conservative and only rejects
when the *query* does not itself ask for that thing.
"""
from __future__ import annotations

import re
import unicodedata

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s\-\.]")

ACCESSORY_PATTERNS = [
    r"\bcase for\b", r"\bcover for\b", r"\bcompatible with\b", r"\bfits\b", r"\bfor use with\b",
    r"\breplacement (?:part|battery|cable|feet|skates)\b", r"\bscreen protector\b", r"\bskin for\b",
    r"\bcharger for\b", r"\bstand for\b", r"\bmount for\b", r"\bgrip tape\b", r"\bmouse feet\b",
    r"\bdock for\b", r"\bsleeve for\b", r"\bholder for\b",
]
RENEWED_PATTERNS = [r"\brenewed\b", r"\brefurbished\b", r"\bpre-?owned\b", r"\bused\b", r"\bopen box\b"]
BUNDLE_PATTERNS = [r"\bbundle\b", r"\bcombo\b", r"\bwith .* (?:pad|case|bag)\b", r"\b\d+[- ]pack\b", r"\bset of \d+\b"]

_ACCESSORY = re.compile("|".join(ACCESSORY_PATTERNS), re.I)
_RENEWED = re.compile("|".join(RENEWED_PATTERNS), re.I)
_BUNDLE = re.compile("|".join(BUNDLE_PATTERNS), re.I)


def clean_text(s: str | None, max_len: int = 300) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _CONTROL_RE.sub("", s).translate(_DASHES)
    s = _WS_RE.sub(" ", s).strip()
    return s[:max_len].strip()


def normalize_title(s: str) -> str:
    s = clean_text(s, 500).lower()
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def normalize_query(s: str) -> str:
    return normalize_title(s)


def model_tokens(s: str) -> set[str]:
    """Tokens that look like model identifiers (letters+digits), e.g. 'mx', '3s', 'g502', 'wh-1000xm5'."""
    out: set[str] = set()
    for tok in normalize_title(s).split():
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            out.add(tok)
        elif tok.isdigit() and len(tok) <= 4:
            out.add(tok)
    return out


def prefilter(query: str, title: str) -> tuple[str, str] | None:
    """Return (rejection_code, reason) for obvious non-matches, else None.

    Only rejects on signals the query itself doesn't contain.
    """
    q = normalize_title(query)
    t = normalize_title(title)
    if _ACCESSORY.search(t) and not _ACCESSORY.search(q):
        return "accessory", "title reads as an accessory or replacement part"
    if _RENEWED.search(t) and not _RENEWED.search(q):
        return "renewed", "title indicates renewed/used condition"
    if _BUNDLE.search(t) and not _BUNDLE.search(q):
        return "bundle", "title indicates a bundle or multipack"
    return None


def condition_from_title(title: str) -> str:
    t = normalize_title(title)
    if re.search(r"\b(renewed|refurbished)\b", t):
        return "renewed"
    if re.search(r"\b(used|pre-owned|open box)\b", t):
        return "used"
    return "new"


def group_key(brand: str | None, model: str | None, variant: dict[str, str]) -> str:
    """Brand + model + the variant dimensions that make products non-interchangeable.

    Colour is intentionally excluded so colour variants group together.
    """
    material = {k: v for k, v in variant.items() if k.lower() not in {"color", "colour"}}
    parts = [normalize_title(brand or ""), normalize_title(model or "")]
    parts += [f"{k.lower()}={normalize_title(v)}" for k, v in sorted(material.items())]
    return "|".join(p for p in parts if p)
