"""
Unit tests for the source connectors and the domain config loader.

No network, no AWS: httpx and boto3 are mocked. Run from the repo root:

    python -m pytest bc/common/tests -q
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

from common import domain_config  # noqa: E402
from common.sources import available_sources, get_source  # noqa: E402
from common.sources.eu_funding_portal import EuFundingPortalSource, clean_html  # noqa: E402
from common.sources.grants_gov import GrantsGovSource  # noqa: E402

REQUIRED_UI_KEYS = {
    "grantId", "title", "agency", "amount", "deadline", "description",
    "eligibility", "applicationProcess", "source", "matchedKeywords", "tags",
}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _client(post=None, get=None):
    """A context-manager httpx.Client stand-in."""
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    if post is not None:
        client.post.side_effect = post
    if get is not None:
        client.get.side_effect = get
    return client


# ---------------------------------------------------------------------------
# domain config
# ---------------------------------------------------------------------------

def test_domain_pack_loads_and_weights_sum_to_one():
    m = domain_config.matching()
    for key in ("withTags", "withoutTags", "EU_FUNDING"):
        w = m["keywordWeights"][key]
        assert abs(sum(w.values()) - 1.0) < 1e-9, key
    for feature, lik in m["featureLikelihoods"].items():
        if feature.startswith("$"):
            continue
        assert 0 < lik["relevant"] <= 1 and 0 < lik["irrelevant"] <= 1, feature
    assert set(domain_config.sources()) >= {"GRANTS_GOV", "EU_FUNDING"}


def test_missing_pack_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GROW2_DOMAIN_DIR", str(tmp_path))
    domain_config.load.cache_clear()
    with pytest.raises(FileNotFoundError, match="GROW2_DOMAIN_DIR"):
        domain_config.matching()
    domain_config.load.cache_clear()


def test_registry_lists_both_connectors():
    assert available_sources() == ["EU_FUNDING", "GRANTS_GOV"]
    assert isinstance(get_source("GRANTS_GOV"), GrantsGovSource)
    with pytest.raises(KeyError):
        get_source("NOPE")


# ---------------------------------------------------------------------------
# grants.gov
# ---------------------------------------------------------------------------

def test_grants_gov_search_returns_ui_format():
    search_payload = {"errorcode": 0, "data": {"hitCount": 1, "oppHits": [
        {"id": "123", "number": "NSF-24-001", "title": "Thermal Transport", "agency": "NSF",
         "agencyCode": "NSF", "openDate": "01/01/2026", "closeDate": "06/30/2026", "oppStatus": "posted"},
    ]}}
    detail_payload = {"errorcode": 0, "data": {"synopsis": {
        "synopsisDesc": "Heat transfer research.", "awardCeiling": "$500,000",
        "applicantEligibilityDesc": "Universities", "agencyContactEmail": "pm@nsf.gov", "agencyContactPhone": "555",
    }}}

    def post(url, json=None):
        return _Resp(search_payload if url.endswith("search2") else detail_payload)

    with patch("common.sources.grants_gov.httpx.Client", return_value=_client(post=post)):
        grants = GrantsGovSource().search("thermal transport")

    assert len(grants) == 1
    g = grants[0]
    assert REQUIRED_UI_KEYS <= set(g)
    assert g["source"] == "GRANTS_GOV"
    assert g["grantId"] == "123" and g["amount"] == 500000.0
    assert g["deadline"] == "06/30/2026" and g["opportunityNumber"] == "NSF-24-001"
    assert "relevanceScore" not in g  # the matcher owns scores


def test_grants_gov_api_error_is_raised():
    with patch("common.sources.grants_gov.httpx.Client",
               return_value=_client(post=lambda *a, **k: _Resp({"errorcode": 1, "msg": "bad"}))):
        with pytest.raises(Exception, match="API Error: bad"):
            GrantsGovSource().search("x")


def test_grants_gov_detail_failure_does_not_sink_search():
    search_payload = {"errorcode": 0, "data": {"oppHits": [{"id": "1", "title": "T", "agency": "A"}]}}

    def post(url, json=None):
        return _Resp(search_payload) if url.endswith("search2") else _Resp({}, status=500)

    with patch("common.sources.grants_gov.httpx.Client", return_value=_client(post=post)):
        grants = GrantsGovSource().search("x")
    assert grants[0]["description"] == "No description available"


# ---------------------------------------------------------------------------
# EU Funding & Tenders Portal
# ---------------------------------------------------------------------------

EU_CATALOGUE = {"fundingData": {"GrantTenderObj": [
    {"type": 1, "identifier": "HORIZON-CL4-2026-AI-01", "reference": "r1", "title": "Trustworthy AI",
     "status": {"abbreviation": "Open"}, "tags": ["ai"], "keywords": [], "flags": [],
     "frameworkProgramme": {"abbreviation": "HORIZON", "description": "Horizon Europe"},
     "deadlineDatesLong": [1780000000000]},
    {"type": 1, "identifier": "CLOSED-1", "title": "Old call", "status": {"abbreviation": "Closed"}},
    {"type": 2, "identifier": "TENDER-1", "title": "Trustworthy AI tender", "status": {"abbreviation": "Open"}},
]}}


def _eu_source_with_cache(catalogue):
    src = EuFundingPortalSource()
    body = MagicMock()
    body.read.return_value = json.dumps(catalogue).encode()
    src._s3 = MagicMock()
    src._s3.get_object.return_value = {"Body": body, "Metadata": {"download_time": "t", "grant_count": "3"}}
    return src


def test_eu_search_filters_type_status_keyword_and_formats():
    details = {"TopicDetails": {"description": "<p>Build <b>trustworthy</b> AI</p>",
               "budgetOverviewJSONItem": {"budgetTopicActionMap": {"t": [{"maxContribution": 4000000}]}}}}
    src = _eu_source_with_cache(EU_CATALOGUE)
    with patch("common.sources.eu_funding_portal.httpx.Client",
               return_value=_client(get=lambda url: _Resp(details))):
        grants = src.search("trustworthy ai", cache_bucket="bucket")

    assert [g["grantId"] for g in grants] == ["HORIZON-CL4-2026-AI-01"]  # tender + closed call dropped
    g = grants[0]
    assert REQUIRED_UI_KEYS <= set(g)
    assert g["source"] == "EU_FUNDING"
    assert g["description"] == "Build trustworthy AI"
    assert g["amount"] == 4000000
    assert g["euFrameworkProgramme"] == "HORIZON"  # string, never the raw dict
    assert g["hasDetailedInfo"] is True
    src._s3.get_object.assert_called_once_with(Bucket="bucket", Key="eu_grants_latest.json")


def test_eu_search_requires_cache_bucket():
    with pytest.raises(ValueError, match="cache_bucket"):
        EuFundingPortalSource().search("x")


def test_eu_cache_failure_is_infrastructure_error():
    src = EuFundingPortalSource()
    src._s3 = MagicMock()
    src._s3.get_object.side_effect = RuntimeError("NoSuchKey")
    with pytest.raises(Exception, match="infrastructure issue"):
        src.search("x", cache_bucket="b")


def test_clean_html():
    assert clean_html("<p>a&nbsp;&amp;  b</p>") == "a & b"
    assert clean_html("") == ""
