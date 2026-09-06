"""CSS selectors per Amazon domain. IDs and data attributes preferred over visible text.

Amazon changes markup often; every entry is a list of fallbacks tried in order. Keep this file
as the single place to patch when a selector drifts. Same base for all three domains; overrides
per domain below where they differ.
"""
from __future__ import annotations

ALLOWED_HOSTS = {
    "www.amazon.com", "amazon.com",
    "www.amazon.ae", "amazon.ae",
    "www.amazon.sa", "amazon.sa",
}

BASE: dict[str, list[str]] = {
    # ---- search results page
    "search_result": ['div.s-main-slot div[data-component-type="s-search-result"][data-asin]'],
    "search_title": ["h2.a-text-normal", '[data-cy="title-recipe"] h2[aria-label]', "h2 a span",
                     "a.s-line-clamp-2", "a.s-line-clamp-4", "h2 span"],
    "search_brand": ["h2.a-size-mini", '[data-cy="title-recipe"] .a-size-base-plus.a-color-base:not(.a-text-normal)'],
    "search_price": [".a-price:not(.a-text-price) .a-offscreen", ".a-price .a-offscreen"],
    "search_rating": ['i[data-cy="reviews-ratings-slot"] span.a-icon-alt', "span.a-icon-alt"],
    "search_count": ['a[href*="customerReviews"] span.s-underline-text', "span.s-underline-text",
                     'span[aria-label$="ratings"]', 'span[aria-label$="rating"]'],
    "search_sponsored": [".puis-sponsored-label-text", ".s-sponsored-label-text", 'span:has-text("Sponsored")'],
    "search_prime": ["i.a-icon-prime", 'span[aria-label="Prime"]'],

    # ---- product page
    "title": ["#productTitle"],
    "price": [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
        "#apex_desktop .a-price:not(.a-text-price) .a-offscreen",
        "#price_inside_buybox", "#priceblock_ourprice", "#priceblock_dealprice",
        "#newBuyBoxPrice", "#tp_price_block_total_price_ww .a-offscreen",
    ],
    "availability": ["#availability", "#availabilityInsideBuyBox_feature_div", "#outOfStock"],
    "delivery_block": [
        "#deliveryBlockMessage", "#mir-layout-DELIVERY_BLOCK", "#delivery-message",
        "#ddmDeliveryMessage", "#amazonGlobal_feature_div", "#shippingMessageInsideBuyBox_feature_div",
    ],
    "import_fees": [
        "#amazonGlobal_feature_div", "#priceInsideBuyBox_feature_div", "#deliveryBlockMessage",
        "#corePriceDisplay_desktop_feature_div", "#apex_desktop",
    ],
    "seller": ["#sellerProfileTriggerId", "#merchant-info", "#merchantInfoFeature_feature_div",
               "#tabular-buybox .tabular-buybox-text[tabular-attribute-name='Sold by']"],
    "rating": ["#acrPopover span.a-icon-alt", '#averageCustomerReviews span[data-hook="rating-out-of-text"]',
               "#acrPopover"],
    "count": ["#acrCustomerReviewText", '[data-hook="total-review-count"]'],
    "asin_input": ["input#ASIN", 'input[name="ASIN"]', 'input[name="ASIN.0"]'],
    "parent_asin": ['input[name="parentAsin"]', "#parentAsin"],
    # Buy-box condition only. "#usedBuySection" is the "Save with Used" upsell, not the buy box.
    "condition": ["#renewedProgramDescriptionAtf", "#condition-status",
                  "#buybox .a-color-secondary:has-text('Condition')"],
    "add_to_cart": ["#add-to-cart-button", 'input[name="submit.add-to-cart"]'],
    "cart_count": ["#nav-cart-count"],
    "atc_confirm": ["#NATC_SMART_WAGON_CONF_MSG_SUCCESS", "#sw-atc-details-single-container",
                    "#huc-v2-order-row-confirm-text", "#attach-added-to-cart-message",
                    'h1:has-text("Added to Cart")', '#sw-atc-confirmation'],

    # ---- deliver-to (glow) and login
    "glow_line": ["#glow-ingress-line2", "#glow-ingress-line1", "#nav-global-location-slot"],
    "glow_link": ["#nav-global-location-popover-link", "#glow-ingress-block"],
    "glux_country_select": ["#GLUXCountryList", 'select[name="GLUXCountryList"]'],
    "glux_country_dropdown": ["#GLUXCountryListDropdown", "span.a-button-text[role='radiogroup']"],
    "glux_address_list": ["#GLUXAddressList", ".GLUX_Address_Book"],
    "glux_done": ['button[name="glowDoneButton"]', "#GLUXConfirmClose", ".a-popover-footer .a-button-primary input"],
    "glux_change_country_link": ["#GLUXChangePostalCodeLink", "#GLUXCountryListDropdown"],
    "account_greeting": ["#nav-link-accountList-nav-line-1", "#nav-link-accountList"],

    # ---- robot check / captcha
    "robot_form": ['form[action*="validateCaptcha"]', "#captchacharacters", 'input[name="field-keywords"][id="captchacharacters"]'],

    # ---- all offers display (other sellers), opened with /dp/<asin>?aod=1
    "aod_container": ["#all-offers-display", "#aod-container"],
    "aod_offer": ["#aod-offer", "div[id='aod-offer']"],
    "aod_pinned": ["#aod-pinned-offer"],
    "aod_price": ["[id^='aod-price-'] .aok-offscreen", "[id^='aod-price-'] .a-offscreen",
                  ".apex-pricetopay-accessibility-label", ".aod-offer-price .a-price .a-offscreen",
                  ".a-price .a-offscreen"],
    "aod_seller": ["#aod-offer-soldBy a", "#aod-offer-soldBy .a-col-right", ".aod-offer-soldBy"],
    "aod_condition": ["#aod-offer-heading", ".aod-offer-heading"],
    "aod_delivery": ["#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE", "[id^='mir-layout-DELIVERY_BLOCK']",
                     ".aod-delivery-promise", "#delivery-message"],
    "aod_shipping": ["#aod_ship_charge_row", "[id^='aod-bottlingDepositFee']"],
    "aod_offer_id": ['input[name$="[offerListingId]"]', 'input[name="offeringID.1"]', 'input[name^="offeringID"]'],
    "aod_atc": ['input[name="submit.addToCart"]', "[id^='a-autoid'] input.a-button-input"],
    "aod_added": ["[id^='aod-offer-added-to-cart-']:not(.aok-hidden)", "[id^='aod-offer-updated-cart-']:not(.aok-hidden)"],
}

OVERRIDES: dict[str, dict[str, list[str]]] = {
    "amazon.ae": {},
    "amazon.sa": {},
}


def selectors_for(marketplace: str) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in BASE.items()}
    for k, v in OVERRIDES.get(marketplace, {}).items():
        out[k] = list(v) + out.get(k, [])
    return out


AOD_PATH = "/dp/{asin}?aod=1&th=1&psc=1"

LANGUAGE_PARAM = {"amazon.com": "en_US", "amazon.ae": "en_AE", "amazon.sa": "en_AE"}
