"""
GrantSource — the interface every external funding database is wrapped in.

The contract is deliberately small: a connector turns a query into a list of
grants in the **UI format** below, and can fetch one grant's details. Everything
downstream (Bayesian scoring, DynamoDB/AppSync writes, the React UI) consumes
that format and is therefore domain- and provider-agnostic.

UI format (keys every connector must set; others are optional extras):

    grantId             str   provider's stable id
    title               str
    agency              str   funder / programme name
    amount              float | None   award ceiling if known
    deadline            str   ISO date 'YYYY-MM-DD' or ''
    description         str   plain text (no HTML)
    eligibility         str
    applicationProcess  str
    source              str   the connector's `name` (e.g. 'GRANTS_GOV')
    matchedKeywords     list  left empty; filled by the matcher
    tags                list  provider tags/keywords if any

Scores (`relevanceScore`, `bayesianScore`, ...) are NOT set here — the matcher
owns them.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class GrantSource(ABC):
    #: Stable id used in payloads (`sources: [...]`), the `source` field of every
    #: grant, and the registry in common.sources.__init__.
    name: str = ""

    @abstractmethod
    def search(self, query: str, filters: Optional[Dict[str, Any]] = None, **context: Any) -> List[Dict[str, Any]]:
        """Return grants matching `query`, newest/most relevant first, in UI format.

        `context` carries per-deployment inputs a connector may need (for example
        the EU connector takes `cache_bucket`). Connectors must ignore context
        keys they do not understand.
        """

    @abstractmethod
    def fetch(self, identifier: str) -> Dict[str, Any]:
        """Return the provider's detail record for one grant, or {'error': ...}."""

    # Shared helpers -------------------------------------------------------

    @staticmethod
    def ui_grant(**fields: Any) -> Dict[str, Any]:
        """Build a UI-format grant with every required key present."""
        base: Dict[str, Any] = {
            "grantId": "",
            "title": "No title",
            "agency": "Unknown agency",
            "amount": None,
            "deadline": "",
            "description": "",
            "eligibility": "",
            "applicationProcess": "",
            "source": "",
            "matchedKeywords": [],
            "tags": [],
        }
        base.update(fields)
        return base
