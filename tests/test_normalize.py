from shopping_hunter.normalize import clean_text, condition_from_title, group_key, model_tokens, prefilter


def test_clean_text_strips_control_and_caps():
    assert clean_text("a\x00b—c   d", 5) == "ab-c"
    assert clean_text("a\x00b—c   d") == "ab-c d"


def test_prefilter_rejects_accessories_renewed_bundles_only_when_query_does_not_ask():
    q = "Logitech MX Master 3S"
    assert prefilter(q, "Hard Case for Logitech MX Master 3S")[0] == "accessory"
    assert prefilter(q, "Logitech MX Master 3S (Renewed)")[0] == "renewed"
    assert prefilter(q, "Logitech MX Master 3S Bundle with mat")[0] == "bundle"
    assert prefilter(q, "Logitech MX Master 3S Graphite") is None
    assert prefilter("Logitech MX Master 3S renewed", "Logitech MX Master 3S (Renewed)") is None


def test_model_tokens():
    assert model_tokens("Logitech MX Master 3S") == {"3s"}
    assert "wh-1000xm5" in model_tokens("Sony WH-1000XM5")


def test_condition_and_group_key():
    assert condition_from_title("Logitech MX Master 3S (Renewed)") == "renewed"
    assert group_key("Logitech", "MX Master 3S", {"color": "Graphite"}) == group_key("Logitech", "MX Master 3S", {"color": "Pale Gray"})
    assert group_key("Logitech", "MX Master 3S", {"edition": "for Mac"}) != group_key("Logitech", "MX Master 3S", {})
