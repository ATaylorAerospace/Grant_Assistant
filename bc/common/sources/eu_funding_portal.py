"""
EU Funding & Tenders Portal connector (Horizon Europe and other EU programmes).

Extracted from bc/eu-grants-search-agent-v2/agent.py; behaviour is unchanged.
The portal's full catalogue (~100 MB JSON) is downloaded nightly to S3 by the
eu-grants-cache-downloader Lambda; `search()` reads that cache (there is no
live API fallback by design — a cache miss is an infrastructure problem), filters
it, then fetches Topic Details for the top matches.
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
import httpx

from .base import GrantSource
from .. import domain_config

logger = logging.getLogger(__name__)


def clean_html(html_text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    if not html_text:
        return ""
    text = re.sub(r"<[^>]+>", "", html_text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


class EuFundingPortalSource(GrantSource):
    name = "EU_FUNDING"

    def __init__(self) -> None:
        cfg = domain_config.sources().get("EU_FUNDING", {})
        self.cache_key = cfg.get("cacheKey", "eu_grants_latest.json")
        self.topic_details_url = cfg.get(
            "topicDetailsUrl",
            "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/{identifier}.json",
        )
        self.portal_url = cfg.get(
            "portalUrl",
            "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{identifier}",
        )
        self.max_filtered = int(cfg.get("maxFiltered", 100))
        self.max_results = int(cfg.get("maxResults", 25))
        self.statuses = cfg.get("statuses", ["Open", "Forthcoming"])
        self._s3 = None

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
        return self._s3

    # -- GrantSource -------------------------------------------------------

    def search(self, query: str, filters: Optional[Dict[str, Any]] = None, **context: Any) -> List[Dict[str, Any]]:
        cache_bucket = context.get("cache_bucket")
        if not cache_bucket:
            raise ValueError("cache_bucket is required (the EU grants S3 cache bucket)")
        try:
            logger.info(f"[EU] Reading from S3 cache: {cache_bucket}/{self.cache_key}")
            response = self.s3.get_object(Bucket=cache_bucket, Key=self.cache_key)
            cache_data = json.loads(response["Body"].read())
            metadata = response.get("Metadata", {})
            logger.info(
                f"[EU] Loaded from S3 cache (downloaded: {metadata.get('download_time', 'unknown')}, "
                f"grants: {metadata.get('grant_count', 'unknown')})"
            )
            all_grants = cache_data.get("fundingData", {}).get("GrantTenderObj", [])
            logger.info(f"[EU] Loaded {len(all_grants)} total grants/tenders from S3 cache")

            filtered = self.filter_grants(all_grants, query, filters or {})
            with_details = self.fetch_details_for(filtered[: self.max_results])
            return [self.to_ui_format(g) for g in with_details]
        except Exception as e:
            logger.error(f"[EU] S3 cache read failed: {e}")
            raise Exception(f"S3 cache read failed - infrastructure issue: {e}")

    def fetch(self, identifier: str) -> Dict[str, Any]:
        try:
            url = self.topic_details_url.format(identifier=identifier.lower())
            with httpx.Client(timeout=30.0) as client:
                response = client.get(url)
                response.raise_for_status()
            topic_details = response.json().get("TopicDetails", {})
            if not topic_details:
                return {"error": f"No details found for {identifier}"}
            topic_details["portalUrl"] = self.portal_url.format(identifier=identifier)
            return {"grantDetails": topic_details}
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EU] Error fetching details for {identifier}: {e}")
            return {"error": str(e)}

    # -- portal specifics --------------------------------------------------

    def filter_grants(self, all_grants: List[Dict], query: str, filters: Dict) -> List[Dict]:
        """Type (grants only), status, then comprehensive keyword filter — matches the portal."""
        grants_only = [g for g in all_grants if g.get("type") == 1]
        logger.info(f"[EU] Type filter: {len(all_grants)} → {len(grants_only)}")

        status_filtered = [
            g for g in grants_only if g.get("status", {}).get("abbreviation", "") in self.statuses
        ]
        logger.info(f"[EU] Status filter: {len(grants_only)} → {len(status_filtered)}")

        if not query:
            return status_filtered[: self.max_filtered]

        query_lower = query.lower()
        # Keyword expansion: "artificial intelligence" should also match "-AI-" in identifiers
        search_terms = [query_lower]
        if query_lower == "artificial intelligence":
            search_terms.append("-ai-")

        keyword_filtered = []
        for grant in status_filtered:
            title = grant.get("title", "").lower()
            call_title = grant.get("callTitle", "").lower()
            identifier = grant.get("identifier", "").lower()
            tags_text = self._join(grant.get("tags", []))
            keywords_text = self._join(grant.get("keywords", []))
            flags_text = self._join(grant.get("flags", []))
            fp = grant.get("frameworkProgramme", {})
            framework_text = (
                (fp.get("description", "") + " " + fp.get("abbreviation", "")).lower()
                if isinstance(fp, dict) else str(fp).lower()
            )
            for term in search_terms:
                if (term in title or term in call_title or term in identifier
                        or term in tags_text or term in keywords_text or term in flags_text
                        or term in framework_text):
                    keyword_filtered.append(grant)
                    break

        logger.info(f"[EU] Keyword filter: {len(status_filtered)} → {len(keyword_filtered)}")
        return keyword_filtered[: self.max_filtered]

    def fetch_details_for(self, grants: List[Dict]) -> List[Dict]:
        for i, grant in enumerate(grants, 1):
            identifier = grant.get("identifier", "")
            logger.info(f"[EU] Fetching details {i}/{len(grants)}: {identifier}")
            try:
                details = self.fetch(identifier)
                if details and not details.get("error"):
                    grant["_detailsData"] = details.get("grantDetails", {})
                    grant["hasDetailedInfo"] = True
                else:
                    grant["hasDetailedInfo"] = False
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[EU] Failed to fetch details for {identifier}: {e}")
                grant["hasDetailedInfo"] = False
        return grants

    @classmethod
    def to_ui_format(cls, grant: Dict[str, Any]) -> Dict[str, Any]:
        try:
            fp = grant.get("frameworkProgramme", {})
            if isinstance(fp, dict):
                agency = fp.get("description") or fp.get("abbreviation") or "European Commission"
                framework = fp.get("abbreviation") or fp.get("description") or ""
            else:
                agency = "European Commission"
                framework = fp or ""

            deadline = ""
            deadline_dates = grant.get("deadlineDatesLong", [])
            if deadline_dates and deadline_dates[0]:
                try:
                    deadline = datetime.fromtimestamp(deadline_dates[0] / 1000).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError):
                    pass

            details = grant.get("_detailsData", {})
            if details and details.get("description"):
                description = clean_html(details["description"])
            else:
                tags = grant.get("tags", [])
                description = f"Topics: {', '.join(tags[:5])}" if tags else "No description"

            amount = None
            if details and details.get("budgetOverviewJSONItem"):
                budget_map = details["budgetOverviewJSONItem"].get("budgetTopicActionMap", {})
                for actions in budget_map.values():
                    for action in actions:
                        if action.get("maxContribution"):
                            amount = action["maxContribution"]
                            break
                    if amount is not None:
                        break

            status = grant.get("status")
            return cls.ui_grant(
                grantId=grant.get("identifier", grant.get("reference", "")),
                title=grant.get("title", "No title"),
                agency=agency,
                amount=amount,
                deadline=deadline,
                description=description,
                eligibility="EU eligibility requirements apply",
                applicationProcess="Apply through EU portal",
                source=cls.name,
                tags=grant.get("tags", []) if isinstance(grant.get("tags"), list) else [],
                hasDetailedInfo=grant.get("hasDetailedInfo", False),
                euReference=grant.get("reference", ""),
                euIdentifier=grant.get("identifier", ""),
                # String field in the schema — never pass the raw dict.
                euFrameworkProgramme=framework,
                euStatus=status.get("abbreviation", "") if isinstance(status, dict) else (status or ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EU] Error converting grant: {e}")
            return cls.ui_grant(
                grantId=f"error_{hash(str(grant))}",
                title=grant.get("title", "Error"),
                agency="European Commission",
                description="Error processing grant",
                source=cls.name,
            )

    @staticmethod
    def _join(value: Any) -> str:
        return " ".join(value).lower() if isinstance(value, list) else str(value).lower()
