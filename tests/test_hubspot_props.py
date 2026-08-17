"""Tests for HubSpot property schema coercion (dropdown/number safety)."""

from skysnap.hubspot_props import PropertySchema, normalize_option_key

_BRANZA_OPTIONS = [
    {"value": "Samorzad"},
    {"value": "Generalni Wykonawcy"},
    {"value": "Zarządzanie Odpadamy"},
    {"value": "Inne"},
]

_API_RESULTS = [
    {"name": "name", "type": "string", "fieldType": "text"},
    {"name": "branza", "type": "enumeration", "fieldType": "select", "options": _BRANZA_OPTIONS},
    {
        "name": "leads_score",
        "type": "enumeration",
        "fieldType": "select",
        "options": [{"value": "P1"}, {"value": "P2"}, {"value": "P3"}, {"value": "P4"}],
    },
    {
        "name": "uslugi_swiadczone",
        "type": "enumeration",
        "fieldType": "checkbox",
        "options": [{"value": "Inspekcje"}, {"value": "Inne"}],
    },
    {"name": "numemployees", "type": "number", "fieldType": "number"},
    {
        "name": "createdate",
        "type": "datetime",
        "fieldType": "date",
        "modificationMetadata": {"readOnlyValue": True},
    },
]


def _schema() -> PropertySchema:
    return PropertySchema.from_api_results("companies", _API_RESULTS)


def test_normalize_option_key_ignores_case_diacritics_punctuation():
    assert normalize_option_key("Generalni wykonawcy") == normalize_option_key("Generalni Wykonawcy")
    assert normalize_option_key("Samorząd") == normalize_option_key("Samorzad")
    assert normalize_option_key("Inne.") == normalize_option_key("Inne")


def test_coerce_maps_enumeration_values_onto_options():
    coerced, dropped = _schema().coerce(
        {"name": "Test", "branza": "Generalni wykonawcy", "leads_score": "P2"}
    )
    assert coerced["branza"] == "Generalni Wykonawcy"
    assert coerced["leads_score"] == "P2"
    assert coerced["name"] == "Test"
    assert dropped == []


def test_coerce_matches_option_despite_hubspot_typo():
    coerced, dropped = _schema().coerce({"branza": "Zarządzanie Odpadami"})
    assert coerced["branza"] == "Zarządzanie Odpadamy"
    assert dropped == []


def test_coerce_drops_unmatched_enumeration_value_with_reason():
    coerced, dropped = _schema().coerce({"leads_score": "75 — dobry fit"})
    assert "leads_score" not in coerced
    assert len(dropped) == 1
    assert dropped[0].property_name == "leads_score"
    assert "no matching option" in dropped[0].reason


def test_coerce_drops_unknown_and_read_only_properties():
    coerced, dropped = _schema().coerce({"notes": "meta", "createdate": "2026-01-01"})
    assert coerced == {}
    reasons = {d.property_name: d.reason for d in dropped}
    assert "does not exist" in reasons["notes"]
    assert "read-only" in reasons["createdate"]


def test_coerce_extracts_number_for_numeric_property():
    coerced, dropped = _schema().coerce({"numemployees": "about 42 people"})
    assert coerced["numemployees"] == "42"
    assert dropped == []


def test_coerce_multi_value_checkbox_keeps_only_valid_options():
    coerced, _ = _schema().coerce({"uslugi_swiadczone": "Inspekcje;Nieznane"})
    assert coerced["uslugi_swiadczone"] == "Inspekcje"


def test_coerce_passthrough_when_schema_unavailable():
    schema = PropertySchema.unavailable("companies")
    coerced, dropped = schema.coerce({"anything": "value"})
    assert coerced == {"anything": "value"}
    assert dropped == []
