"""
Tests for bc/common/matching.py — the config-driven matcher that replaced the two
per-agent bayesian_matcher.py files.

fixtures/matching_golden.json was generated from the ORIGINAL per-agent modules
before they were deleted (US = grants-search-agent-v2, EU = eu-grants-search-
agent-v2). The unified matcher must reproduce their numbers:

  features   — the US extractor's results (it read every grant shape the agents
               produce); where the US extractor crashed on a string amount or
               ignored `awardCeiling`, the EU extractor's safer result is expected
  keyword    — US scores with source=GRANTS_GOV; EU scores with source=EU_FUNDING
               on flat (UI-format) grants, the only shape the EU agent ever scored
  bayes      — identical given identical features

No AWS: the profile table is mocked.
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "bc"))
os.environ.setdefault("GROW2_DOMAIN_DIR", str(REPO / "config" / "domains" / "grants"))
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("USER_PROFILE_TABLE", "UserProfile-test")

from common import domain_config, matching  # noqa: E402

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "matching_golden.json").read_text())
GRANTS = GOLDEN["grants"]
QUERIES = GOLDEN["queries"]
PROFILES = GOLDEN["profiles"]


def _expected_features(name):
    us, eu = GOLDEN["golden"]["features"][name]["US"], GOLDEN["golden"]["features"][name]["EU"]
    if isinstance(us, str) and us.startswith("RAISES"):
        return eu  # old US code crashed on a string amount; EU handled it
    if "awardCeiling" in GRANTS[name]:
        return eu  # old US code ignored awardCeiling; EU read it
    return us


# ---------------------------------------------------------------------------
# config integrity
# ---------------------------------------------------------------------------

def test_every_feature_has_a_likelihood_and_vice_versa():
    m = domain_config.matching()
    features = {k for k in m["features"] if not k.startswith("$")}
    likelihoods = {k for k in m["featureLikelihoods"] if not k.startswith("$")}
    assert features == likelihoods


def test_sources_reference_existing_weight_sets():
    m = domain_config.matching()
    for src, cfg in m["sources"].items():
        if src.startswith("$"):
            continue
        assert cfg["keywordWeights"] == "auto" or cfg["keywordWeights"] in m["keywordWeights"]
        assert cfg["relevanceScore"] in ("keyword", "profile")


# ---------------------------------------------------------------------------
# golden comparisons against the original modules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(GRANTS))
def test_features_match_original_extractor(name):
    assert matching.extract_grant_features(dict(GRANTS[name])) == _expected_features(name)


@pytest.mark.parametrize("name", sorted(GRANTS))
@pytest.mark.parametrize("query", QUERIES)
def test_keyword_score_matches_original_us(name, query):
    expected = GOLDEN["golden"]["keyword"][name][query]["US"]
    assert matching.calculate_keyword_score(dict(GRANTS[name]), query, source="GRANTS_GOV") == pytest.approx(expected)


@pytest.mark.parametrize("name", sorted(n for n in GRANTS if "nested" not in n))
@pytest.mark.parametrize("query", QUERIES)
def test_keyword_score_matches_original_eu_on_flat_grants(name, query):
    expected = GOLDEN["golden"]["keyword"][name][query]["EU"]
    assert matching.calculate_keyword_score(dict(GRANTS[name]), query, source="EU_FUNDING") == pytest.approx(expected)


@pytest.mark.parametrize("profile_name", sorted(PROFILES))
@pytest.mark.parametrize("name", sorted(GRANTS))
def test_bayesian_probability_matches_original(profile_name, name):
    # Same rule as _expected_features: the EU numbers are the reference where the
    # old US extractor raised (string amount) or ignored awardCeiling.
    side = "EU" if _expected_features(name) is GOLDEN["golden"]["features"][name]["EU"] else "US"
    expected = GOLDEN["golden"]["bayes"][profile_name][name][side]
    features = matching.extract_grant_features(dict(GRANTS[name]))
    assert matching.calculate_bayesian_probability(PROFILES[profile_name], features) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# behaviour the config now controls
# ---------------------------------------------------------------------------

def test_string_amount_is_treated_as_missing_not_a_crash():
    f = matching.extract_grant_features({"title": "x", "amount": "50000"})
    assert not (f["hasLargeAmount"] or f["hasMediumAmount"] or f["hasSmallAmount"])


def test_feature_spec_errors_are_explicit():
    with pytest.raises(ValueError, match="Unsupported feature spec"):
        matching._evaluate_feature({"bogus": 1}, {"title": "", "agency": "", "description": "", "tags": []}, 0)


def _profile_table(profiles, default=None):
    table = MagicMock()
    table.query.return_value = {"Items": profiles}
    table.scan.return_value = {"Items": [default] if default else []}
    return table


@patch("common.matching._table")
def test_relevance_score_follows_source_config(mock_table):
    mock_table.return_value = _profile_table([{"userId": "u", "isActive": True, "researcherType": "biomedical"}])
    grants = [dict(GRANTS["us_flat_nih_cancer"]), dict(GRANTS["eu_flat_horizon_tags"])]

    us = matching.apply_dual_scoring([dict(g) for g in grants], "u", "cancer immunotherapy", source="GRANTS_GOV")
    for g in us:
        assert g["relevanceScore"] == g["keywordScore"]
    assert us[0]["keywordScore"] >= us[1]["keywordScore"]  # sorted by keyword score

    eu = matching.apply_dual_scoring([dict(g) for g in grants], "u", "cancer immunotherapy", source="EU_FUNDING")
    for g in eu:
        assert g["relevanceScore"] == g["profileMatchScore"] == g["bayesianProbability"]


@patch("common.matching._table")
def test_default_profile_fallback_only_for_sources_that_opt_in(mock_table):
    default = {"userId": "shared", "default_profile": True, "researcherType": "engineering", "name": "Default"}
    mock_table.return_value = _profile_table([], default=default)
    grant = dict(GRANTS["us_nested_nsf_ai"])

    eu = matching.apply_dual_scoring([dict(grant)], "nobody", "ai", source="EU_FUNDING")
    assert eu[0]["profileMatchScore"] > 0  # scored against the default profile

    us = matching.apply_dual_scoring([dict(grant)], "nobody", "ai", source="GRANTS_GOV")
    assert us[0]["profileMatchScore"] == 0.0  # keyword-only, no fallback


@patch("common.matching._table")
def test_index_unavailable_falls_back_to_scan(mock_table):
    from botocore.exceptions import ClientError
    table = MagicMock()
    table.query.side_effect = ClientError({"Error": {"Code": "ValidationException", "Message": "no index"}}, "Query")
    table.scan.return_value = {"Items": [{"userId": "u", "isActive": True, "researcherType": "biomedical"}]}
    mock_table.return_value = table
    assert matching.get_active_user_profile("u")["researcherType"] == "biomedical"
    table.scan.assert_called_once()


def test_pagination_follows_last_evaluated_key():
    op = MagicMock(side_effect=[
        {"Items": [1, 2], "LastEvaluatedKey": {"k": 1}},
        {"Items": [3]},
    ])
    assert matching._paginate(op, IndexName="x") == [1, 2, 3]
    assert op.call_args_list[1][1]["ExclusiveStartKey"] == {"k": 1}
