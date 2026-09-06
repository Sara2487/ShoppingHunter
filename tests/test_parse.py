from datetime import date
from decimal import Decimal

from shopping_hunter.browser import parse as P


def test_prices_across_marketplaces_and_scripts():
    assert P.parse_price("$99.99", "USD") .amount == Decimal("99.99")
    assert P.parse_price("US$1,299.00", "USD").amount == Decimal("1299.00")
    m = P.parse_price("AED 349.00", "AED"); assert (m.amount, m.currency) == (Decimal("349.00"), "AED")
    m = P.parse_price("349.00 د.إ", "USD"); assert m.currency == "AED"
    m = P.parse_price("SAR 379.00", "SAR"); assert m.amount == Decimal("379.00")
    m = P.parse_price("٣٧٩٫٠٠ ر.س".replace("٫", "."), "SAR"); assert m.amount == Decimal("379.00") and m.currency == "SAR"
    assert P.parse_price("", "USD") is None
    assert P.parse_price("Price not available", "USD") is None


def test_rating_and_count():
    assert P.parse_rating("4.6 out of 5 stars") == 4.6
    assert P.parse_rating("4,6 من 5 نجوم") == 4.6
    assert P.parse_rating("4.6") == 4.6
    assert P.parse_rating("stars") is None
    assert P.parse_count("31,240 ratings") == 31240
    assert P.parse_count("(2,100)") == 2100
    assert P.parse_count("12K") == 12000
    assert P.parse_count("٣١٬٢٤٠".replace("٬", ",")) == 31240


def test_availability_and_condition():
    assert P.parse_availability("In Stock") is True
    assert P.parse_availability("Only 3 left in stock - order soon.") is True
    assert P.parse_availability("Currently unavailable.") is False
    assert P.parse_availability("") is None
    assert P.parse_condition("Renewed") == "renewed"
    assert P.parse_condition("Used - Like New") == "used"
    assert P.parse_condition("New") == "new"


def test_delivery_days_formats():
    today = date(2026, 9, 5)
    assert P.parse_delivery_days("FREE delivery Thursday, September 18", today) == 13
    assert P.parse_delivery_days("Delivery 18 - 25 September", today) == 13
    assert P.parse_delivery_days("Arrives Sep 18 - 25", today) == 13
    assert P.parse_delivery_days("Get it by Tomorrow", today) == 1
    assert P.parse_delivery_days("Delivery Jan 3", today) == (date(2027, 1, 3) - today).days
    assert P.parse_delivery_days("no date here", today) is None


def test_shipping_and_import_fees_combined_vs_separate():
    ship, fees, combined, w = P.parse_shipping_and_fees(
        "Delivery Thursday, September 18", "$99.99\n+ $45.13 Shipping & Import Fees Deposit to Jordan", "USD")
    assert ship.amount == Decimal("45.13") and fees.amount == 0 and combined
    ship, fees, combined, w = P.parse_shipping_and_fees(
        "AED 30.00 delivery Tuesday, 16 September to Jordan", "Import Fees Deposit: AED 45.00", "AED")
    assert ship.amount == Decimal("30.00") and fees.amount == Decimal("45.00") and not combined
    ship, fees, combined, w = P.parse_shipping_and_fees("FREE delivery Monday, September 15", "", "USD")
    assert ship.amount == 0 and fees is None
    ship, fees, combined, w = P.parse_shipping_and_fees("Delivery Monday", "", "USD")
    assert ship is None and "shipping_cost_unrecognized" in w


def test_ships_to_tri_state():
    names = ("Jordan", "الأردن")
    assert P.parse_ships_to("This item cannot be shipped to your selected delivery location.", "Deliver to Jordan", names, None)[0] == "no"
    assert P.parse_ships_to("FREE delivery September 18", "Deliver to Jordan", names, 13)[0] == "yes"
    assert P.parse_ships_to("Delivery September 18 to Jordan", "", names, 13)[0] == "yes"
    assert P.parse_ships_to("Delivery September 18", "Deliver to United States", names, 13)[0] == "unknown"
    assert P.parse_ships_to("", "Deliver to Jordan", names, None)[0] == "unknown"
