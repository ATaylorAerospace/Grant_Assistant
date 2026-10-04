#!/usr/bin/env python3
"""
EU Grants Search Agent V2 - AgentCore Native with Async Tasks

This agent is a self-contained orchestrator that:
1. Receives search request from initiator Lambda
2. Returns immediately with "STARTED" status
3. Spawns background thread for long-running work
4. Writes progress to DynamoDB as it works
5. Publishes events to AppSync for real-time UI updates

Key differences from V1:
- Uses app.add_async_task() for background processing
- Reads from S3 cache (NO API fallback per user requirement)
- Fetches grant details from Topic Details API
- Writes directly to DynamoDB (no processor Lambda)
- Publishes directly to AppSync (no processor Lambda)
- Supports up to 8-hour execution
- Self-contained - no external orchestration needed
- NO SSM Parameter Store access required (bucket passed via payload)

Dependencies:
- bedrock-agentcore: AgentCore Runtime SDK
- boto3: AWS SDK for DynamoDB/AppSync/S3
- httpx: HTTP client for EU Topic Details API
"""

import re
import logging
import httpx
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..'))  # local runs: bc/ on path
import json
import html
import threading
import time
import boto3
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Configure logging
# CRITICAL: stream=sys.stdout so AgentCore captures logs (it captures stdout, not stderr)
import sys
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

# Initialize AgentCore app
app = BedrockAgentCoreApp()

# Get AWS region from environment (set by AgentCore Runtime)
AWS_REGION = os.environ.get('AWS_REGION')
if not AWS_REGION:
    raise ValueError("AWS_REGION environment variable not set")

# Initialize AWS clients (will use execution role)
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)

# Table references - will be initialized in invoke() with names from payload
eu_grant_records_table = None
search_event_table = None

# AppSync client - will be initialized in invoke() with endpoint from payload
appsync_client = None

# Bayesian matcher will be imported AFTER setting USER_PROFILE_TABLE env var
# This is done in invoke() before calling apply_dual_scoring
BAYESIAN_SCORING_AVAILABLE = False
apply_dual_scoring = None

# ============================================================================
# MAIN ENTRYPOINT - Returns immediately, spawns background thread
# ============================================================================

@app.entrypoint
def invoke(payload):
    """
    Main entrypoint - returns immediately with "STARTED" status
    
    Background thread handles:
    - Reading from S3 cache (NO API fallback)
    - Fetching grant details from Topic Details API
    - Applying Bayesian scoring
    - Writing to DynamoDB
    - Publishing to AppSync
    """
    global eu_grant_records_table, search_event_table
    
    try:
        logger.info(f"[EU V2] Agent invoked with payload: {payload}")
        
        # Extract parameters
        session_id = payload.get('sessionId')
        query = payload.get('query')
        cognito_user_id = payload.get('cognitoUserId')
        filters = payload.get('filters', {})
        sources = payload.get('sources', ['EU_FUNDING'])
        
        # Get table names from payload (passed by Lambda)
        table_names = payload.get('tableNames', {})
        eu_grant_records_table_name = table_names.get('euGrantRecords')
        search_event_table_name = table_names.get('searchEvent')
        user_profile_table_name = table_names.get('userProfile')
        
        # Get S3 cache bucket from payload (passed by Lambda)
        eu_cache_bucket = payload.get('euCacheBucket')
        
        # Get AppSync info from payload
        appsync_endpoint = payload.get('appsyncEndpoint')
        graphql_api_id = payload.get('graphqlApiId')
        
        if not eu_grant_records_table_name or not search_event_table_name:
            raise ValueError("Missing required table names in payload")
        
        if not eu_cache_bucket:
            raise ValueError("Missing euCacheBucket in payload")
        
        if not appsync_endpoint or not graphql_api_id:
            raise ValueError("Missing appsyncEndpoint or graphqlApiId in payload")
        
        if not session_id or not query:
            raise ValueError("Missing required parameters: sessionId, query")
        
        logger.info(f"[EU V2] Session: {session_id}, Query: {query}, User: {cognito_user_id}")
        logger.info(f"[EU V2] Using tables: {eu_grant_records_table_name}, {search_event_table_name}")
        logger.info(f"[EU V2] S3 cache bucket: {eu_cache_bucket}")
        logger.info(f"[EU V2] AppSync endpoint: {appsync_endpoint}")
        
        # Set USER_PROFILE_TABLE environment variable BEFORE importing common.matching
        if user_profile_table_name:
            os.environ['USER_PROFILE_TABLE'] = user_profile_table_name
            logger.info(f"[EU V2] Set USER_PROFILE_TABLE: {user_profile_table_name}")
        
        # Set APPSYNC_ENDPOINT and GRAPHQL_API_ID for AppSync client
        os.environ['APPSYNC_ENDPOINT'] = appsync_endpoint
        os.environ['GRAPHQL_API_ID'] = graphql_api_id
        
        # NOW import common.matching (after setting environment variable)
        global BAYESIAN_SCORING_AVAILABLE, apply_dual_scoring, appsync_client
        try:
            from common.matching import apply_dual_scoring as _apply_dual_scoring
            apply_dual_scoring = _apply_dual_scoring
            BAYESIAN_SCORING_AVAILABLE = True
            logger.info("[EU V2] ✅ Successfully imported common.matching")
        except Exception as e:
            BAYESIAN_SCORING_AVAILABLE = False
            logger.error(f"[EU V2] ❌ Failed to import common.matching: {str(e)}")
            logger.error("[EU V2] ⚠️ Bayesian scoring will be DISABLED")
        
        # Initialize AppSync client for mutations (IAM auth only)
        try:
            from appsync_client import AppSyncClient
            appsync_client = AppSyncClient()
            logger.info("[EU V2] ✅ AppSync client initialized")
        except Exception as e:
            logger.error(f"[EU V2] ❌ Failed to initialize AppSync client: {str(e)}")
            appsync_client = None
        
        # Initialize tables with names from payload (make them global for background thread)
        eu_grant_records_table = dynamodb.Table(eu_grant_records_table_name)
        search_event_table = dynamodb.Table(search_event_table_name)
        
        # Start async task tracking
        task_id = app.add_async_task("eu_grant_search", {
            "sessionId": session_id,
            "query": query,
            "cognitoUserId": cognito_user_id
        })
        
        logger.info(f"[EU V2] Started async task: {task_id}")
        
        # Spawn background thread for long-running work
        def background_search():
            try:
                logger.info(f"[EU V2] Background thread started for session {session_id}")
                
                # ============================================================
                # STEP 1: READ FROM S3 CACHE (NO API FALLBACK)
                # ============================================================
                logger.info(f"[EU V2] ========== STEP 1: READING FROM S3 CACHE ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': 'Reading EU grants from S3 cache...',
                    'step': 1,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                grants = read_grants_from_s3_cache(query, filters, eu_cache_bucket)
                logger.info(f"[EU V2] ✅ Found {len(grants)} grants from S3 cache")
                
                # ============================================================
                # STEP 2: APPLY BAYESIAN SCORING
                # ============================================================
                logger.info(f"[EU V2] ========== STEP 2: APPLYING BAYESIAN SCORING ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': f'Found {len(grants)} grants, applying scoring...',
                    'step': 2,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                if BAYESIAN_SCORING_AVAILABLE and cognito_user_id:
                    logger.info(f"[EU V2] 🧠 Applying Bayesian scoring for user {cognito_user_id}")
                    scored_grants = apply_dual_scoring(grants, cognito_user_id, query, source='EU_FUNDING')
                    logger.info(f"[EU V2] ✅ Bayesian scoring complete")
                else:
                    logger.warning("[EU V2] ⚠️  Bayesian scoring not available, using default scores")
                    for grant in grants:
                        grant['relevanceScore'] = 0.5
                        grant['keywordScore'] = 0.5
                        grant['profileMatchScore'] = 0.0
                    scored_grants = grants
                
                # ============================================================
                # STEP 3: WRITE TO DYNAMODB VIA APPSYNC
                # ============================================================
                logger.info(f"[EU V2] ========== STEP 3: WRITING TO DYNAMODB VIA APPSYNC ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': f'Writing {len(scored_grants)} grants to database...',
                    'step': 3,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                for i, grant in enumerate(scored_grants):
                    logger.info(f"[EU V2] 📝 Writing grant {i+1}/{len(scored_grants)}: {grant.get('grantId')}")
                    write_eu_grant_record(session_id, grant, eu_grant_records_table, cognito_user_id)
                    
                    if i % 5 == 0:  # Every 5 grants
                        # nosemgrep: arbitrary-sleep - Intentional: Rate limiting for DynamoDB writes
                        time.sleep(0.1)
                
                logger.info(f"[EU V2] ✅ Wrote {len(scored_grants)} grants to DynamoDB")
                
                # Log top results summary for easy debugging
                top5 = sorted(scored_grants, key=lambda g: g.get('relevanceScore', 0), reverse=True)[:5]
                scores = [g.get('relevanceScore', 0) for g in scored_grants]
                score_range = f"{min(scores):.2f}–{max(scores):.2f}" if scores else "n/a"
                logger.info(f"[EU V2] 📊 SEARCH SUMMARY: {len(scored_grants)} grants, score range {score_range}")
                for rank, g in enumerate(top5, 1):
                    logger.info(f"[EU V2]   #{rank} [{g.get('relevanceScore', 0):.2f}] {g.get('title', '')[:80]} ({g.get('agency', '')})")
                
                # ============================================================
                # STEP 4: PUBLISH COMPLETION EVENT
                # ============================================================
                logger.info(f"[EU V2] ========== STEP 4: PUBLISHING COMPLETION EVENT ==========")
                publish_search_event(session_id, 'SEARCH_COMPLETE', {
                    'message': f'Search complete - found {len(scored_grants)} grants',
                    'totalGrants': len(scored_grants),
                    'step': 4,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                logger.info(f"[EU V2] ========== BACKGROUND SEARCH COMPLETE ==========")
                
            except Exception as e:
                logger.error(f"[EU V2] Error in background search: {str(e)}")
                publish_search_event(session_id, 'SEARCH_ERROR', {
                    'message': f'Search failed: {str(e)}',
                    'error': str(e)
                }, search_event_table, cognito_user_id)
                
            finally:
                app.complete_async_task(task_id)
        
        # Start background thread
        threading.Thread(target=background_search, daemon=True).start()
        
        # Return immediately
        return {
            "status": "STARTED",
            "sessionId": session_id,
            "message": f"EU search started for: {query}",
            "taskId": task_id,
            "version": "V2"
        }
        
    except Exception as e:
        logger.error(f"[EU V2] Error in entrypoint: {str(e)}")
        return {
            "status": "ERROR",
            "error": str(e),
            "version": "V2"
        }

# ============================================================================
# S3 CACHE READING (NO API FALLBACK)
# ============================================================================

# ============================================================================
# SOURCE CONNECTOR — the EU portal lives in common/sources (domain layer)
# ============================================================================
from common.sources.eu_funding_portal import EuFundingPortalSource, clean_html  # noqa: E402,F401

_source = EuFundingPortalSource()


def read_grants_from_s3_cache(query: str, filters: dict = None, cache_bucket: str = None) -> List[Dict[str, Any]]:
    """Search the nightly S3 cache (no live-API fallback). Kept as the agent's call site name."""
    return _source.search(query, filters, cache_bucket=cache_bucket)


# Names kept for existing callers/tests.
filter_eu_grants = _source.filter_grants
fetch_grant_details_for_top_matches = _source.fetch_details_for
fetch_eu_grant_details = _source.fetch
convert_eu_grant_to_ui_format = EuFundingPortalSource.to_ui_format

# ============================================================================
# DYNAMODB FUNCTIONS - Agent writes directly via AppSync
# ============================================================================

def write_eu_grant_record(session_id: str, grant: Dict[str, Any], table, cognito_user_id: str = None):
    """Write EuGrantRecord via AppSync mutation"""
    try:
        if not appsync_client:
            logger.error("[EU V2] ❌ AppSync client not available, cannot write EuGrantRecord")
            return
        
        # Log what we received
        logger.info(f"[EU V2] 🔍 Grant object keys: {list(grant.keys())}")
        logger.info(f"[EU V2] 🔍 Grant data sample - grantId: {grant.get('grantId')}, title: {grant.get('title', '')[:50]}, agency: {grant.get('agency')}, amount: {grant.get('amount')}, description length: {len(grant.get('description', ''))}")
        
        record = {
            "sessionId": session_id,
            "grantId": grant['grantId'],
            "title": grant['title'],
            "agency": grant.get('agency', ''),
            "amount": float(grant.get('amount', 0)) if grant.get('amount') else None,
            "deadline": grant.get('deadline', ''),
            "description": grant.get('description', ''),
            "eligibility": grant.get('eligibility', ''),
            "applicationProcess": grant.get('applicationProcess', ''),
            "source": grant.get('source', 'EU_FUNDING'),
            "relevanceScore": float(grant.get('relevanceScore', 0.85)),
            "profileMatchScore": float(grant.get('profileMatchScore', 0)) if grant.get('profileMatchScore') is not None else None,
            "keywordScore": float(grant.get('keywordScore', 0)) if grant.get('keywordScore') is not None else None,
            "hasDetailedInfo": grant.get('hasDetailedInfo', False),
            "euReference": grant.get('euReference', ''),
            "euIdentifier": grant.get('euIdentifier', ''),
            "euFrameworkProgramme": grant.get('euFrameworkProgramme', ''),
            "euStatus": grant.get('euStatus', '')
        }
        
        logger.info(f"[EU V2] 🔍 Record being sent - agency: {record['agency']}, amount: {record['amount']}, description length: {len(record['description'])}, hasDetailedInfo: {record['hasDetailedInfo']}")
        
        created = appsync_client.create_eu_grant_record(record)
        if created:
            logger.info(f"[EU V2] ✅ Created EuGrantRecord via AppSync: {grant['grantId']}")
        else:
            # Surface the failure instead of swallowing it — previously the return
            # value was ignored, so rejected mutations looked like successes.
            logger.error(f"[EU V2] ❌ create_eu_grant_record returned failure for grantId={grant['grantId']}; record not persisted")

    except Exception as e:
        logger.error(f"[EU V2] ❌ Error writing EuGrantRecord: {str(e)}")
        raise

def publish_search_event(session_id: str, event_type: str, data: Dict[str, Any], table, cognito_user_id: str = None):
    """Publish SearchEvent via AppSync mutation"""
    if not appsync_client:
        error_msg = "[EU V2] ❌ AppSync client not available, cannot publish SearchEvent"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info(f"[EU V2] 📤 Publishing SearchEvent: {event_type} for session {session_id}")
    logger.info(f"[EU V2] 📤 Event data: {data}")
    
    success = appsync_client.create_search_event(
        session_id=session_id,
        event_type=event_type,
        data=data,
        cognito_user_id=cognito_user_id
    )
    
    if not success:
        error_msg = f"[EU V2] ❌ Failed to publish SearchEvent via AppSync: {event_type}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info(f"[EU V2] ✅ Published SearchEvent via AppSync: {event_type} for session {session_id}")

# ============================================================================
# RUN
# ============================================================================

if __name__ == "__main__":
    print("[EU Grants V2] *** CODE VERSION: 2026-03-05-v2 — search summary logging ***", flush=True)
    logger.info("[EU V2] Starting EU Grants Search Agent V2")
    app.run()
