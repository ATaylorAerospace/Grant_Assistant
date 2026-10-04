"""
Grant matcher shared by the search agents — Bayesian profile match + keyword score.

This replaces the two near-identical bc/<agent>/bayesian_matcher.py files. The
*domain* knowledge — which words make a grant "biomedical", the amount bands,
the priors, likelihood ratios and field weights — lives in
config/domains/<domain>/matching.json (see `features`, `featureLikelihoods`,
`keywordWeights`, `sources`). This module is the platform side: it evaluates
that configuration and talks to DynamoDB for the researcher profile.

Per-source behaviour (matching.json → `sources.<SOURCE>`):
  keywordWeights          which `keywordWeights` entry to use; "auto" picks
                          withTags/withoutTags depending on whether the grant has tags
  relevanceScore          "keyword" (US: the All tab sorts by keyword relevance) or
                          "profile" (EU: agent discovery ranks by profile match)
  defaultProfileFallback  true → a user without a profile is scored against the
                          shared default profile (EU behaviour)

Usage from an agent, after USER_PROFILE_TABLE is set:

    from common.matching import apply_dual_scoring
    scored = apply_dual_scoring(grants, cognito_user_id, query, source="GRANTS_GOV")
"""
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import domain_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Researcher profiles (DynamoDB)
# ---------------------------------------------------------------------------

# GSI on UserProfile.userId (see amplify/data/resource.ts). Amplify names the
# DynamoDB index <models>By<Field> — the same convention as the existing
# proposalsByUserId index used by the proposals-query Lambda.
USER_PROFILE_USER_ID_INDEX = os.environ.get('USER_PROFILE_USER_ID_INDEX', 'userProfilesByUserId')

# Error codes that mean "the index cannot be used here" (not deployed yet, or the
# name differs): fall back to a table scan rather than failing the search.
_INDEX_FALLBACK_ERRORS = ('ValidationException', 'ResourceNotFoundException', 'AccessDeniedException')

_dynamodb = None


def _table():
    """UserProfile table, resolved lazily so USER_PROFILE_TABLE can be set at invoke time."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION'))
    return _dynamodb.Table(os.environ['USER_PROFILE_TABLE'])  # REQUIRED - set by CDK / the agent


def _paginate(operation, **kwargs) -> List[Dict[str, Any]]:
    """Run a DynamoDB query/scan to completion (a single call returns at most 1 MB)."""
    items: List[Dict[str, Any]] = []
    while True:
        response = operation(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def _profiles_for_user(cognito_user_id: str) -> List[Dict[str, Any]]:
    """All profiles owned by a user — one indexed query, scan fallback if the index is unavailable."""
    table = _table()
    try:
        return _paginate(
            table.query,
            IndexName=USER_PROFILE_USER_ID_INDEX,
            KeyConditionExpression=Key('userId').eq(cognito_user_id),
        )
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code not in _INDEX_FALLBACK_ERRORS:
            raise
        logger.warning(f"⚠️ Index {USER_PROFILE_USER_ID_INDEX} unavailable ({code}); scanning the profile table instead")
        return _paginate(
            table.scan,
            FilterExpression='userId = :uid',
            ExpressionAttributeValues={':uid': cognito_user_id},
        )


def _default_profile() -> Optional[Dict[str, Any]]:
    """The shared default profile, if one is flagged (paginated scan, first hit wins)."""
    table = _table()
    scan_kwargs: Dict[str, Any] = {
        'FilterExpression': 'default_profile = :default_val',
        'ExpressionAttributeValues': {':default_val': True},
    }
    while True:
        response = table.scan(**scan_kwargs)
        default_profiles = response.get('Items', [])
        if default_profiles:
            p = default_profiles[0]
            logger.info(f"✅ Found default profile: {p.get('name', 'Unknown')} ({p.get('researcherType', 'Unknown type')})")
            return p
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return None
        scan_kwargs['ExclusiveStartKey'] = last_key


def get_active_user_profile(cognito_user_id: str, default_profile_fallback: bool = False) -> Optional[Dict[str, Any]]:
    """Active profile for a user, else any of their profiles, else (optionally) the shared default."""
    try:
        profiles = _profiles_for_user(cognito_user_id)

        active = [p for p in profiles if p.get('isActive')]
        if active:
            logger.info(f"✅ Found active profile for Cognito user {cognito_user_id}")
            return active[0]
        if profiles:
            logger.info(f"✅ Found profile for Cognito user {cognito_user_id}, using first available")
            return profiles[0]

        if default_profile_fallback:
            logger.info(f"⚠️ No specific profile found for user {cognito_user_id}, checking for default profile")
            default = _default_profile()
            if default:
                return default
            logger.warning(f"❌ No profile found for Cognito user {cognito_user_id} and no default profile available")
            return None

        logger.error(f"❌ No profile found for Cognito user {cognito_user_id}")
        logger.error("   User must create a profile in the Profile tab before using Bayesian matching")
        return None
    except Exception as e:
        logger.error(f"❌ Error fetching profile for Cognito user {cognito_user_id}: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# Grant text fields — handles the UI format and both agents' native structures
# ---------------------------------------------------------------------------

def _first(*values: Any) -> str:
    for v in values:
        if v:
            return v
    return ''


def grant_text_fields(grant: Dict[str, Any]) -> Dict[str, Any]:
    """title / agency / description / tags of a grant in any of the shapes the agents produce."""
    search = grant.get('searchData') or {}
    details = grant.get('detailsData') or {}
    data = grant.get('grantData') or {}
    fp = data.get('frameworkProgramme') or {}

    title = _first(grant.get('title'), search.get('title'), data.get('title'))
    agency = _first(
        grant.get('agency'),
        search.get('agency'),
        fp.get('description') if isinstance(fp, dict) else None,
        fp if isinstance(fp, str) else None,
    )
    description = _first(
        grant.get('description'),
        (details.get('synopsis') or {}).get('synopsisDesc'),
        data.get('description'),
        data.get('descriptionByte'),
        data.get('summary'),
        details.get('description'),
    )
    tags = list(grant.get('tags') or data.get('tags') or []) + list(data.get('keywords') or [])
    return {
        'title': (title or '').lower(),
        'agency': (agency or '').lower(),
        'description': (description or '').lower(),
        'tags': [str(t) for t in tags],
    }


def grant_amount(grant: Dict[str, Any]) -> float:
    """Award amount as a number; 0 when missing or not numeric (EU uses awardCeiling, US uses amount)."""
    value = grant.get('awardCeiling', grant.get('amount', 0))
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return 0.0
    return float(value)


# ---------------------------------------------------------------------------
# Features (config-driven)
# ---------------------------------------------------------------------------

def _evaluate_feature(spec: Dict[str, Any], fields: Dict[str, Any], amount: float) -> bool:
    """One entry of matching.json `features`.

    {"fields": ["agency"] | ["combined"], "anyOf": [...]}  case-insensitive substring match
    {"amount": {"gt": n, "gte": n, "lt": n, "lte": n}}      numeric band on the award amount
    """
    if 'anyOf' in spec:
        texts = []
        for f in spec.get('fields', ['combined']):
            if f == 'combined':
                texts.append(f"{fields['title']} {fields['agency']} {fields['description']}")
            else:
                texts.append(fields.get(f, ''))
        haystack = ' '.join(texts)
        return any(term.lower() in haystack for term in spec['anyOf'])
    if 'amount' in spec:
        band = spec['amount']
        ok = amount > 0 or 'gt' not in band or band['gt'] < 0  # bands never match a missing amount
        if amount <= 0:
            return False
        if 'gt' in band and not amount > band['gt']:
            ok = False
        if 'gte' in band and not amount >= band['gte']:
            ok = False
        if 'lt' in band and not amount < band['lt']:
            ok = False
        if 'lte' in band and not amount <= band['lte']:
            ok = False
        return ok
    raise ValueError(f"Unsupported feature spec: {spec}")


def extract_grant_features(grant: Dict[str, Any]) -> Dict[str, bool]:
    """Boolean features for Bayesian analysis, as defined in matching.json `features`."""
    fields = grant_text_fields(grant)
    amount = grant_amount(grant)
    specs = domain_config.matching()['features']
    return {name: _evaluate_feature(spec, fields, amount) for name, spec in specs.items() if not name.startswith('$')}


# ---------------------------------------------------------------------------
# Bayesian profile match
# ---------------------------------------------------------------------------

def get_researcher_priors(profile: Dict[str, Any]) -> float:
    cfg = domain_config.matching()
    researcher_type = profile.get('researcherType', '').lower()
    expertise_level = profile.get('expertise_level', '').lower()

    priors = cfg['researcherPriors']
    base_prior = priors.get(researcher_type, priors.get('default', 0.10))
    multipliers = cfg['expertiseMultipliers']
    multiplier = multipliers.get(expertise_level, multipliers.get('default', 1.0))
    if profile.get('early_investigator', '').lower() == 'true':
        multiplier *= cfg.get('earlyInvestigatorMultiplier', 1.0)
    return min(base_prior * multiplier, cfg.get('priorCap', 0.30))


def get_feature_likelihoods() -> Dict[str, Dict[str, float]]:
    """P(feature|relevant) / P(feature|irrelevant) per feature. Ratios are kept small
    (1.5-3x) because matched features multiply."""
    return {k: v for k, v in domain_config.matching()['featureLikelihoods'].items() if not k.startswith('$')}


def calculate_bayesian_probability(profile: Dict[str, Any], features: Dict[str, bool]) -> float:
    """P(relevant | features) in odds form: posterior odds = likelihood ratio × prior odds."""
    prior = get_researcher_priors(profile)
    logger.info(f"🎯 Starting Bayesian calculation - Prior: {prior:.3f}")
    likelihoods = get_feature_likelihoods()

    likelihood_ratio = 1.0
    matched = []
    for feature, present in features.items():
        if present and feature in likelihoods and likelihoods[feature]['irrelevant'] > 0:
            ratio = likelihoods[feature]['relevant'] / likelihoods[feature]['irrelevant']
            likelihood_ratio *= ratio
            matched.append(f"{feature}({ratio:.1f}x)")
    if matched:
        logger.info(f"📋 Matched features ({len(matched)}): {', '.join(matched)}")
        logger.info(f"📈 Combined likelihood from features: {likelihood_ratio:.3f}x")
    else:
        logger.info("📋 No features matched - using prior only")

    # Small confidence boosts for profiles that carry keywords / preferred agencies.
    boosts = domain_config.matching().get('profileBoosts', {})
    keyword_boost = 1.0
    applied = []
    if profile.get('keywords'):
        keyword_boost *= boosts.get('keywords', 1.08); applied.append(f"keywords({boosts.get('keywords', 1.08)}x)")
    if profile.get('optimized_keywords'):
        keyword_boost *= boosts.get('optimizedKeywords', 1.03); applied.append(f"opt_keywords({boosts.get('optimizedKeywords', 1.03)}x)")
    if profile.get('agencies'):
        keyword_boost *= boosts.get('agencies', 1.04); applied.append(f"agencies({boosts.get('agencies', 1.04)}x)")
    if applied:
        logger.info(f"🚀 Profile boosts: {', '.join(applied)} = {keyword_boost:.3f}x total")
    likelihood_ratio *= keyword_boost

    odds_prior = prior / (1 - prior)
    odds_posterior = likelihood_ratio * odds_prior
    probability = max(0.0, min(1.0, odds_posterior / (1 + odds_posterior)))
    logger.info(f"🎲 Final calculation: prior({prior:.3f}) × likelihood({likelihood_ratio:.3f}) = {probability:.3f} ({probability*100:.1f}%)")
    return probability


# ---------------------------------------------------------------------------
# Keyword score (no profile)
# ---------------------------------------------------------------------------

def _weights_for(source: str, has_tags: bool) -> Dict[str, float]:
    cfg = domain_config.matching()
    choice = cfg.get('sources', {}).get(source, {}).get('keywordWeights', 'auto')
    if choice == 'auto':
        choice = 'withTags' if has_tags else 'withoutTags'
    return cfg['keywordWeights'][choice]


def calculate_keyword_score(grant: Dict[str, Any], search_query: str, source: str = 'GRANTS_GOV') -> float:
    """Share of query terms found in title / description / agency / tags, weighted per matching.json."""
    if not search_query or not search_query.strip():
        return 0.5  # neutral when there is no query

    fields = grant_text_fields(grant)
    query_terms = [t for t in search_query.lower().split() if len(t) > 2]
    total_terms = len(query_terms)
    if total_terms == 0:
        return 0.5

    tags_text = ' '.join(fields['tags']).lower()
    has_tags = bool(fields['tags'])
    w = _weights_for(source, has_tags)

    title_matches = sum(1 for t in query_terms if t in fields['title'])
    description_matches = sum(1 for t in query_terms if t in fields['description'])
    agency_matches = sum(1 for t in query_terms if t in fields['agency'])
    tags_matches = sum(1 for t in query_terms if t in tags_text) if has_tags else 0

    score = (
        (title_matches / total_terms) * w['title']
        + (description_matches / total_terms) * w['description']
        + (agency_matches / total_terms) * w['agency']
        + ((tags_matches / total_terms) * w.get('tags', 0.0) if has_tags else 0.0)
    )
    return min(score, 1.0)


# ---------------------------------------------------------------------------
# Entry points used by the agents
# ---------------------------------------------------------------------------

def apply_dual_scoring(grants: List[Dict[str, Any]], cognito_user_id: str, search_query: str = "",
                       source: str = 'GRANTS_GOV') -> List[Dict[str, Any]]:
    """Score every grant two ways and sort by keyword score.

    Sets keywordScore, profileMatchScore, bayesianProbability and relevanceScore
    on each grant. Which score becomes relevanceScore is per source
    (matching.json `sources.<SOURCE>.relevanceScore`).
    """
    cfg = domain_config.matching().get('sources', {}).get(source, {})
    relevance_from = cfg.get('relevanceScore', 'keyword')
    fallback = bool(cfg.get('defaultProfileFallback', False))

    logger.info(f"Applying dual scoring ({source}) for Cognito user {cognito_user_id} to {len(grants)} grants")
    logger.info(f"Search query: '{search_query}'")

    profile = get_active_user_profile(cognito_user_id, default_profile_fallback=fallback)
    if not profile:
        logger.warning(f"No profile found for Cognito user {cognito_user_id}, using keyword-only scoring")
        for grant in grants:
            k = calculate_keyword_score(grant, search_query, source)
            grant['keywordScore'] = k
            grant['profileMatchScore'] = 0.0
            grant['relevanceScore'] = k
        return grants

    logger.info(f"Found profile for {cognito_user_id}: {profile.get('name', 'Unknown')} ({profile.get('researcherType', 'Unknown type')})")

    scored: List[Dict[str, Any]] = []
    for grant in grants:
        try:
            k = calculate_keyword_score(grant, search_query, source)
            p = calculate_bayesian_probability(profile, extract_grant_features(grant))
            grant['keywordScore'] = k
            grant['profileMatchScore'] = p
            grant['bayesianProbability'] = p
            grant['relevanceScore'] = p if relevance_from == 'profile' else k
            logger.debug(f"Grant '{grant.get('title', '')[:40]}...' - Keyword: {k:.3f}, Profile: {p:.3f}")
        except Exception as e:
            logger.error(f"Error scoring grant {grant.get('grantId', 'unknown')}: {str(e)}")
            grant['keywordScore'] = 0.5
            grant['profileMatchScore'] = 0.0
            grant['relevanceScore'] = 0.5
        scored.append(grant)

    scored.sort(key=lambda g: g.get('keywordScore', 0), reverse=True)
    if scored:
        top = scored[0]
        logger.info(f"Dual scoring complete. Top by keyword: {top.get('title', '')[:40]}... "
                    f"(K:{top.get('keywordScore', 0):.3f}, P:{top.get('profileMatchScore', 0):.3f})")
    return scored


def apply_bayesian_scoring(grants: List[Dict[str, Any]], cognito_user_id: str, source: str = 'GRANTS_GOV') -> List[Dict[str, Any]]:
    """DEPRECATED: use apply_dual_scoring."""
    logger.warning("apply_bayesian_scoring is deprecated, use apply_dual_scoring instead")
    return apply_dual_scoring(grants, cognito_user_id, "", source)


def convert_decimal_to_float(obj):
    """Convert DynamoDB Decimal objects to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: convert_decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimal_to_float(v) for v in obj]
    return obj
