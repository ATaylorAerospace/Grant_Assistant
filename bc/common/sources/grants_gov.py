"""
grants.gov connector (US federal opportunities).

Extracted from bc/grants-search-agent-v2/agent.py; behaviour is unchanged:
search2 for the first page of results, then fetchOpportunity per hit for the
synopsis, then convert to the UI format.
"""
import logging
from typing import Any, Dict, List, Optional

import httpx

from .base import GrantSource
from .. import domain_config

logger = logging.getLogger(__name__)

# Natural-language → grants.gov parameter mappings (kept for filter parsing).
AGENCY_CODE_MAPPINGS_ONLY = {
    "hhs": "HHS-NIH11",
    "dod": "DOD",
    "doc": "DOC",
    "nasa": "NASA",
    "neh": "NEH",
    "usda": "USDA",
    "dhs": "DHS",
    "dol": "DOL",
    "dot": "DOT",
    "va": "VA",
    "hud": "HUD",
    "epa": "EPA",
    "ed": "ED",
    "nih": "HHS-NIH11",
    "cdc": "HHS-CDC",
    "ahrq": "HHS-AHRQ",
    "fema": "DHS-DHS",
    "onr": "DOD-ONR",
    "navair": "DOD-ONR-AIR",
    "darpa dso": "DOD-DARPA-DSO",
    "nsf": "NSF",
}

STATUS_MAPPINGS = {
    "posted": "posted",
    "closed": "closed",
    "archived": "archived",
    "forecasted": "forecasted",
    "active": "posted",
    "open": "posted",
}

FUNDING_INSTRUMENT_MAPPINGS = {
    "grants": "G",
    "grant": "G",
    "cooperative agreements": "CA",
    "cooperative agreement": "CA",
    "contracts": "PC",
    "contract": "PC",
    "procurement": "PC",
}


class GrantsGovSource(GrantSource):
    name = "GRANTS_GOV"

    def __init__(self) -> None:
        cfg = domain_config.sources().get("GRANTS_GOV", {})
        self.search_url = cfg.get("searchUrl", "https://api.grants.gov/v1/api/search2")
        self.fetch_url = cfg.get("fetchUrl", "https://api.grants.gov/v1/api/fetchOpportunity")
        self.max_results = int(cfg.get("maxResults", 25))
        self.user_agent = cfg.get("userAgent", "Mozilla/5.0 (compatible; GrantsAgentV2/1.0)")
        self._headers = {"Content-Type": "application/json", "User-Agent": self.user_agent}

    # -- GrantSource -------------------------------------------------------

    def search(self, query: str, filters: Optional[Dict[str, Any]] = None, **context: Any) -> List[Dict[str, Any]]:
        logger.info(f"[grants.gov] Searching for: {query}")
        parsed = self.parse_search_filters(query)
        payload = {
            "rows": self.max_results,
            "startRecordNum": 0,
            "resultType": "json",
            "searchOnly": False,
            "keyword": parsed["keyword"],
            "oppStatuses": parsed["oppStatuses"],
        }
        raw_grants = self.call_search_api(payload).get("oppHits", [])[: self.max_results]

        grants: List[Dict[str, Any]] = []
        for i, grant in enumerate(raw_grants, 1):
            logger.info(f"[grants.gov] Processing grant {i}/{len(raw_grants)}: {grant.get('id', 'Unknown')}")
            record = {
                "searchData": {
                    "id": grant.get("id", ""),
                    "number": grant.get("number", ""),
                    "title": grant.get("title", ""),
                    "agencyCode": grant.get("agencyCode", ""),
                    "agency": grant.get("agency", ""),
                    "openDate": grant.get("openDate", ""),
                    "closeDate": grant.get("closeDate", ""),
                    "oppStatus": grant.get("oppStatus", ""),
                    "docType": grant.get("docType", ""),
                    "cfdaList": grant.get("cfdaList", []),
                },
                "detailsData": None,
                "error": None,
            }
            opp_id = grant.get("id")
            if opp_id:
                try:
                    details = self.fetch(opp_id)
                    if details and not details.get("error"):
                        record["detailsData"] = details
                    else:
                        record["error"] = details.get("error", "Failed to fetch details")
                except Exception as e:  # noqa: BLE001 - one bad detail must not sink the search
                    record["error"] = str(e)
            grants.append(self.to_ui_format(record))

        logger.info(f"[grants.gov] Converted {len(grants)} grants to UI format")
        return grants

    def fetch(self, identifier: str) -> Dict[str, Any]:
        try:
            payload = {"opportunityId": int(identifier)}
            with httpx.Client(timeout=10.0, headers=self._headers) as client:
                response = client.post(self.fetch_url, json=payload)
                if response.status_code != 200:
                    return {"error": f"HTTP {response.status_code}"}
                result = response.json()
                if result.get("errorcode", 0) != 0:
                    return {"error": f"API Error: {result.get('msg', 'Unknown error')}"}
                return result.get("data", {})
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    # -- grants.gov specifics ---------------------------------------------

    @staticmethod
    def parse_search_filters(user_input: str) -> Dict[str, str]:
        """Convert natural language to API filters (simple keyword matching)."""
        return {
            "keyword": user_input.strip(),
            "agencies": "",
            "fundingCategories": "",
            "eligibilities": "",
            "oppStatuses": "posted",
            "fundingInstruments": "",
            "aln": "",
            "oppNum": "",
            "dateRange": "",
        }

    def call_search_api(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info(f"[grants.gov] Calling search API: {self.search_url}")
        try:
            with httpx.Client(timeout=30.0, headers=self._headers) as client:
                response = client.post(self.search_url, json=payload)
                response.raise_for_status()
            result = response.json()
            if result.get("errorcode", 0) != 0:
                raise Exception(f"API Error: {result.get('msg', 'Unknown error')}")
            data = result.get("data", {})
            logger.info(f"[grants.gov] API returned {len(data.get('oppHits', []))} grants (total: {data.get('hitCount', 0)})")
            return data
        except Exception as e:
            logger.error(f"[grants.gov] API call failed: {e}")
            raise

    @classmethod
    def to_ui_format(cls, grant_data: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a search hit + its fetched details into the UI format."""
        search_data = grant_data.get("searchData", {})
        details_data = grant_data.get("detailsData") or {}
        synopsis = details_data.get("synopsis", {})

        amount = 0.0
        amount_str = synopsis.get("awardCeiling", "0")
        if amount_str and str(amount_str).lower() != "none":
            try:
                clean = str(amount_str).replace("$", "").replace(",", "").strip()
                if clean:
                    amount = float(clean)
            except ValueError:
                amount = 0.0

        return cls.ui_grant(
            grantId=search_data.get("id", ""),
            title=search_data.get("title", "No title"),
            agency=search_data.get("agency", "Unknown agency"),
            amount=amount,
            deadline=search_data.get("closeDate", ""),
            description=synopsis.get("synopsisDesc", "No description available"),
            eligibility=synopsis.get("applicantEligibilityDesc", "See grant details"),
            applicationProcess=f"Contact: {synopsis.get('agencyContactEmail', '')}",
            contactEmail=synopsis.get("agencyContactEmail", ""),
            contactPhone=synopsis.get("agencyContactPhone", ""),
            openDate=search_data.get("openDate", ""),
            opportunityNumber=search_data.get("number", ""),
            source=cls.name,
        )
