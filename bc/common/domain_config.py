"""
Loader for the domain config pack.

A domain pack is a directory of JSON files that carries everything about *what*
GROW2 matches and generates — scoring weights, source endpoints, agency prompt
sets — as opposed to *how* the platform runs. The grants pack lives in
`config/domains/grants/`. Swapping the domain means swapping the pack (and the
source connectors), not the agents.

Resolution order for the pack directory:
  1. $GROW2_DOMAIN_DIR            — local development, points at config/domains/<name>
  2. <this package>/domain/        — the copy baked into the agent image
                                     (agentcore-stack.ts copies the pack here at synth;
                                     bc/invoke-local.sh does the same for local runs)
"""
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


_PACKAGED_DIR = Path(__file__).resolve().parent / "domain"


def domain_dir() -> Path:
    env = os.environ.get("GROW2_DOMAIN_DIR")
    if env:
        return Path(env)
    return _PACKAGED_DIR


@lru_cache(maxsize=None)
def load(name: str) -> Dict[str, Any]:
    """Load `<domain dir>/<name>.json`. Raises a clear error if the pack is missing."""
    path = domain_dir() / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"Domain config '{name}.json' not found in {path.parent}. "
            "Set GROW2_DOMAIN_DIR to config/domains/<domain>, or run through "
            "bc/invoke-local.sh / the CDK build which copy the pack into bc/common/domain/."
        )
    with path.open() as f:
        return json.load(f)


def matching() -> Dict[str, Any]:
    """Scoring weights for the Bayesian / keyword matcher (matching.json)."""
    return load("matching")


def sources() -> Dict[str, Any]:
    """Endpoints and limits for the external source connectors (sources.json)."""
    return load("sources")
