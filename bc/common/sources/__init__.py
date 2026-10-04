"""
Source connectors — the one place GROW2 talks to external grant databases.

Every connector implements `GrantSource` (see base.py) and returns grants in
the shared UI format, so the search agents, the Bayesian matcher, the AppSync
writers and the React UI never see a provider's raw schema. Adding a funding
database — or a different domain's catalogue — means adding a connector here
and registering it below; the agents do not change.

    from common.sources import get_source
    grants = get_source("GRANTS_GOV").search("thermal transport")
"""
from typing import Dict, Type

from .base import GrantSource
from .grants_gov import GrantsGovSource
from .eu_funding_portal import EuFundingPortalSource

_REGISTRY: Dict[str, Type[GrantSource]] = {
    GrantsGovSource.name: GrantsGovSource,
    EuFundingPortalSource.name: EuFundingPortalSource,
}


def get_source(name: str) -> GrantSource:
    """Instantiate a connector by its `source` id (e.g. GRANTS_GOV, EU_FUNDING)."""
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise KeyError(f"Unknown grant source '{name}'. Known: {sorted(_REGISTRY)}") from None


def available_sources():
    return sorted(_REGISTRY)


__all__ = ["GrantSource", "GrantsGovSource", "EuFundingPortalSource", "get_source", "available_sources"]
