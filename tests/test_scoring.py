from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings as hsettings
from hypothesis import strategies as st

from shopping_hunter.duties import JORDAN_V1, estimate_import_fees
from shopping_hunter.fx import FxRates, quantize
from shopping_hunter.models import HuntRequest, MatchDecision
from shopping_hunter.scoring import score_offer, score_offers, sort_key, split_tiers

from .conftest import make_offer


def _match(ref: str, conf="high") -> MatchDecision:
    return MatchDecision(candidate_ref=ref, matches=True, confidence=conf, canonical_brand="Logitech",
                         canonical_model="MX Master 3S", variant=[], reasons=[], rejection_code=None)


_DEFAULT = object()


def _score(offer, req=None, decision=_DEFAULT):
    req = req or HuntRequest(query="Logitech MX Master 3S")
    d = _match(offer.candidate_ref) if decision is _DEFAULT else decision
    return score_offer(offer, d, req, FxRates(None), JORDAN_V1, "JOD")


def test_amazon_fees_replace_estimate_and_landed_is_sum():
    s = _score(make_offer())
    assert s.fees_source == "amazon" and s.fees_confidence == "high"
    assert s.landed_jod == quantize(s.price_jod + s.shipping_jod + s.fees_jod, "JOD")
    assert s.rank_tier == "ranked" and s.landed_cost_complete
    assert s.duty_est is None  # no estimate components when Amazon fees present


def test_missing_amazon_fees_uses_versioned_estimate():
    s = _score(make_offer(fees=None))
    assert s.fees_source == "estimate" and s.rule_version == JORDAN_V1.version
    assert s.fees_jod == s.duty_est + s.tax_est
    assert s.rank_tier == "ranked"


def test_missing_shipping_is_unconfirmed_not_zero():
    s = _score(make_offer(shipping=None))
    assert s.shipping_jod is None and s.landed_jod is None
    assert not s.landed_cost_complete and s.rank_tier == "unconfirmed"


def test_unknown_shipping_is_unconfirmed():
    assert _score(make_offer(ships="unknown")).rank_tier == "unconfirmed"


def test_hard_exclusions_have_reasons():
    assert _score(make_offer(ships="no")).exclusion_reason == "does_not_ship"
    assert _score(make_offer(in_stock=False)).exclusion_reason == "out_of_stock"
    assert _score(make_offer(condition="renewed")).exclusion_reason == "condition_not_allowed"
    assert _score(make_offer(rating=3.9)).exclusion_reason == "review_floor"
    assert _score(make_offer(count=49)).exclusion_reason == "review_floor"
    assert _score(make_offer(rating=None)).exclusion_reason == "review_floor"
    d = MatchDecision(candidate_ref="amazon.com:B09HM94VDS", matches=False, confidence="high", canonical_brand=None,
                      canonical_model=None, variant=[], reasons=[], rejection_code="accessory")
    assert _score(make_offer(), decision=d).exclusion_reason == "accessory"
    assert _score(make_offer(), decision=None).exclusion_reason == "no_match"
    assert _score(make_offer(), decision=_match("amazon.com:B09HM94VDS", "low")).exclusion_reason == "low_match_confidence"


def test_review_floor_boundaries():
    req = HuntRequest(query="xx", min_rating=4.0, min_ratings_count=50)
    assert _score(make_offer(rating=4.0, count=50), req).rank_tier == "ranked"
    assert _score(make_offer(rating=3.99, count=50), req).rank_tier == "excluded"


def test_budget_exclusion():
    req = HuntRequest(query="xx", budget_jod=Decimal("50"))
    assert _score(make_offer(), req).exclusion_reason == "over_budget"


def test_ordering_is_deterministic_and_by_landed_cost():
    a = make_offer(asin="B000000001", price="100", fees="10")
    b = make_offer(asin="B000000002", price="90", fees="10")
    c = make_offer(asin="B000000003", price="90", fees="10", days=3)
    decs = {o.candidate_ref: _match(o.candidate_ref) for o in (a, b, c)}
    ranked, _, _ = split_tiers(score_offers([a, b, c], decs, HuntRequest(query="xx"), FxRates(None), JORDAN_V1))
    assert [r.requested_asin for r in ranked] == ["B000000003", "B000000002", "B000000001"]
    assert [r.offer_ref for r in ranked] == [r.offer_ref for r in sorted(ranked, key=sort_key)]


def test_other_sellers_inherit_buybox_import_fee_rate():
    buybox = make_offer(price="100.00", fees="20.00")                      # Amazon showed 20% import fee
    other = make_offer(price="80.00", fees=None, offer_id="SELLER01")      # same listing, no fee shown
    decs = {buybox.candidate_ref: _match(buybox.candidate_ref)}
    scored = {s.offer_ref: s for s in score_offers([buybox, other], decs, HuntRequest(query="xx"), FxRates(None), JORDAN_V1)}
    o = scored[other.offer_ref]
    assert o.fees_source == "amazon_derived" and o.fees_confidence == "medium"
    fx = FxRates(None)
    assert o.fees_jod == fx.convert(Decimal("16.00"), "USD", "JOD")[0]
    assert scored[buybox.offer_ref].fees_source == "amazon"
    # a combined shipping+fees figure must not seed the rate
    combined = make_offer(asin="B000000009", price="100.00", fees="0.00")
    combined.fees_included_in_shipping = True
    other2 = make_offer(asin="B000000009", price="80.00", fees=None, offer_id="SELLER02")
    decs2 = {combined.candidate_ref: _match(combined.candidate_ref)}
    s2 = {s.offer_ref: s for s in score_offers([combined, other2], decs2, HuntRequest(query="xx"), FxRates(None), JORDAN_V1)}
    assert s2[other2.offer_ref].fees_source == "estimate"


def test_long_offer_ids_are_hashed_in_refs():
    from shopping_hunter.models import offer_ref
    long_id = "jAXuT5aiKRPv6Ry%2B8Y3kR8AuCo0PKiyMvADLHUUt" * 3
    ref = offer_ref("amazon.com", "B09HM94VDS", long_id)
    assert ref.startswith("amazon.com:B09HM94VDS:h") and len(ref) < 40
    assert offer_ref("amazon.com", "B09HM94VDS", "AAAA1111") == "amazon.com:B09HM94VDS:AAAA1111"


def test_all_money_is_decimal():
    s = _score(make_offer(fees=None))
    for v in (s.price_jod, s.shipping_jod, s.fees_jod, s.landed_jod, s.duty_est, s.tax_est, s.fx_rate):
        assert isinstance(v, Decimal)


money = st.decimals(min_value=Decimal("0"), max_value=Decimal("5000"), places=2, allow_nan=False, allow_infinity=False)


@hsettings(max_examples=200, deadline=None)
@given(price=money, shipping=money, delta=money)
def test_landed_cost_monotone_in_price(price, shipping, delta):
    fx, rules, req = FxRates(None), JORDAN_V1, HuntRequest(query="xx")
    lo = score_offer(make_offer(price=str(price), shipping=str(shipping), fees=None), _match("amazon.com:B09HM94VDS"), req, fx, rules, "JOD")
    hi = score_offer(make_offer(price=str(price + delta), shipping=str(shipping), fees=None), _match("amazon.com:B09HM94VDS"), req, fx, rules, "JOD")
    assert hi.landed_jod >= lo.landed_jod


@hsettings(max_examples=100, deadline=None)
@given(price=money, shipping=money)
def test_estimate_never_negative_and_zero_under_de_minimis(price, shipping):
    est = estimate_import_fees(JORDAN_V1, price, shipping)
    assert est.total >= 0 and est.duty >= 0 and est.tax >= 0
    if price + shipping <= JORDAN_V1.de_minimis:
        assert est.total == 0
