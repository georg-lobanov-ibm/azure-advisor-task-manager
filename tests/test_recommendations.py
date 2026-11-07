"""Shaping Azure Advisor results for the UI (pure functions)."""

import pytest

from advisor_agent.web import recommendations as rec
from fakes.fake_mcp_server import DEFAULT_RECOMMENDATIONS

SUB = "00000000-0000-0000-0000-000000000001"


def test_normalize_reads_the_known_fields():
    row = rec.normalize(DEFAULT_RECOMMENDATIONS[0])

    assert row["recommendation_type_id"] == "e10b1381-5f0a-47ff-8c7b-37bd4eaea707"
    assert row["resource_id"].endswith("/virtualMachines/vm-a")
    assert row["subscription_id"] == SUB
    assert row["resource_group"] == "rg-a"
    assert row["resource_name"] == "vm-a"
    assert row["category"] == "Cost" and row["impact"] == "High"
    assert row["problem"] == "VM is underused" and row["solution"] == "Resize the VM"
    assert row["advisor_id"] == "11111111-1111-1111-1111-111111111111"
    assert row["linkable"] is True
    assert row["raw"] is DEFAULT_RECOMMENDATIONS[0]


def test_resource_id_falls_back_to_the_prefix_of_the_advisor_id():
    item = {
        "id": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
        "/providers/Microsoft.Advisor/recommendations/abc",
        "properties": {"recommendationTypeId": "t1"},
    }

    row = rec.normalize(item)

    assert row["resource_id"] == f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"
    assert row["linkable"] is True
    assert row["advisor_id"] == "abc"


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"properties": None},
        {"properties": "text"},
        {"properties": {"shortDescription": "x", "resourceMetadata": []}},
        {"id": 5, "name": 7, "properties": {"category": ["Cost"], "impact": {"a": 1}}},
    ],
)
def test_missing_or_mistyped_fields_do_not_raise_and_are_not_linkable(item):
    row = rec.normalize(item)

    assert row is not None
    assert row["linkable"] is False
    assert all(isinstance(row[key], str) for key in ("category", "impact", "problem", "resource_id"))


@pytest.mark.parametrize("item", [None, 3, "x", ["a"]])
def test_non_objects_are_skipped(item):
    assert rec.normalize(item) is None
    assert rec.normalize_all([item, DEFAULT_RECOMMENDATIONS[0]])[0]["advisor_id"].startswith("1111")


def test_long_text_is_clipped():
    item = {"properties": {"description": "x" * 100_000, "recommendationTypeId": "t", "resourceMetadata": {"resourceId": "r" * 5000}}}

    row = rec.normalize(item)

    assert len(row["description"]) <= 4000
    assert row["resource_id"] == ""  # over the length limit: dropped, not trusted


def test_row_ids_are_unique_and_case_insensitive():
    a = DEFAULT_RECOMMENDATIONS[0]
    upper = {**a, "properties": {**a["properties"], "resourceMetadata": {"resourceId": a["properties"]["resourceMetadata"]["resourceId"].upper()}}}

    rows = rec.normalize_all([a, upper, a])

    assert len({row["row_id"] for row in rows}) == 3


def test_normalize_all_rejects_a_non_list():
    with pytest.raises(ValueError):
        rec.normalize_all({"recommendations": []})


def test_payload_with_truncation_flag():
    payload = {"results": {"recommendations": DEFAULT_RECOMMENDATIONS, "areResultsTruncated": True}}

    rows, truncated = rec.recommendations_from_payload(payload)

    assert len(rows) == 2 and truncated is True
    assert rec.recommendations_from_payload({"results": {"recommendations": []}}) == ([], False)
    assert rec.recommendations_from_payload({"results": {"recommendations": [], "areResultsTruncated": "yes"}})[1] is False


def test_payload_without_results_is_an_empty_list():
    assert rec.recommendations_from_payload({"status": 200}) == ([], False)


# -- what may be forwarded to Azure -------------------------------------------------


def test_list_arguments_forwards_only_the_allowlist():
    arguments = rec.list_arguments(
        {
            "status": "New",
            "category": "Cost",
            "impact": "High",
            "subscription": SUB,
            "top": "100000",
            "resource-group": "x",
            "tenant": "t",
            "refresh": "1",
        }
    )

    assert arguments == {"top": 100, "status": "New", "category": "Cost", "impact": "High", "subscription": SUB}


def test_list_arguments_defaults_to_the_maximum_page():
    assert rec.list_arguments({}) == {"top": 100}


@pytest.mark.parametrize(
    "params",
    [
        {"status": "New; rm -rf"},
        {"category": "a" * 100},
        {"impact": "1"},
        {"impact": "High\n"},
        {"subscription": "my subscription name"},
        {"subscription": SUB + "x"},
    ],
)
def test_list_arguments_rejects_bad_values(params):
    with pytest.raises(ValueError):
        rec.list_arguments(params)


# -- subscriptions ----------------------------------------------------------------


def test_subscriptions_are_read_from_the_likely_spellings():
    payload = {"results": {"subscriptions": [{"subscriptionId": SUB.upper(), "displayName": "Prod"}]}}
    assert rec.subscriptions_from_payload(payload) == [{"id": SUB, "name": "Prod"}]

    value = {"results": [{"id": f"/subscriptions/{SUB}", "name": "Dev"}]}
    assert rec.subscriptions_from_payload(value) == [{"id": SUB, "name": "Dev"}]


@pytest.mark.parametrize("payload", [None, {}, {"results": None}, {"results": {"subscriptions": "x"}}, {"results": [{"id": "nope"}, 5]}])
def test_unknown_subscription_shapes_give_an_empty_list(payload):
    assert rec.subscriptions_from_payload(payload) == []
