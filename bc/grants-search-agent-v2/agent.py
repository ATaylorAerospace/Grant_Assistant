#!/usr/bin/env python3
"""
US Grants Search Agent V2 - AgentCore Native with Async Tasks
Version: 2.0 (Testing CDK Update Workflow)

This agent is a self-contained orchestrator that:
1. Receives search request from initiator Lambda
2. Returns immediately with "STARTED" status
3. Spawns background thread for long-running work
4. Writes progress to DynamoDB as it works
5. Publishes events to AppSync for real-time UI updates

Key differences from V1:
- Uses app.add_async_task() for background processing
- Writes directly to DynamoDB (no processor Lambda)
- Publishes directly to AppSync (no processor Lambda)
- Supports up to 8-hour execution
- Self-contained - no external orchestration needed

Dependencies:
- bedrock-agentcore: AgentCore Runtime SDK
- boto3: AWS SDK for DynamoDB/AppSync
- httpx: HTTP client for grants.gov API
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
ssm = boto3.client('ssm', region_name=AWS_REGION)

# Table references - will be initialized in invoke() with names from payload
grant_records_table = None
search_event_table = None

# AppSync client - will be initialized in invoke() with endpoint from payload
appsync_client = None

# Bayesian matcher will be imported AFTER setting USER_PROFILE_TABLE env var
# This is done in invoke() before calling apply_dual_scoring
BAYESIAN_SCORING_AVAILABLE = False
apply_dual_scoring = None

# ============================================================================
# SOURCE CONNECTOR — grants.gov lives in common/sources (domain layer)
# ============================================================================
from common.sources.grants_gov import (  # noqa: E402
    GrantsGovSource,
    AGENCY_CODE_MAPPINGS_ONLY,
    STATUS_MAPPINGS,
    FUNDING_INSTRUMENT_MAPPINGS,
)

_source = GrantsGovSource()
# Names kept so existing call sites and logs read the same.
search_grants_sync = _source.search
fetch_grant_details = _source.fetch
call_grants_api = _source.call_search_api
parse_search_filters = GrantsGovSource.parse_search_filters
convert_grant_to_ui_format = GrantsGovSource.to_ui_format

# ============================================================================
# MAIN ENTRYPOINT - Returns immediately, spawns background thread
# ============================================================================

@app.entrypoint
def invoke(payload):
    """
    Main entrypoint - returns immediately with "STARTED" status
    
    Background thread handles:
    - Calling grants.gov API
    - Applying Bayesian scoring
    - Writing to DynamoDB
    - Publishing to AppSync
    """
    global grant_records_table, search_event_table
    
    try:
        logger.info(f"[V2] Agent invoked with payload: {payload}")
        
        # Extract parameters
        session_id = payload.get('sessionId')
        query = payload.get('query')
        cognito_user_id = payload.get('cognitoUserId')
        filters = payload.get('filters', {})
        sources = payload.get('sources', ['GRANTS_GOV'])
        
        # Get table names from payload (passed by Lambda)
        table_names = payload.get('tableNames', {})
        grant_records_table_name = table_names.get('grantRecords')
        search_event_table_name = table_names.get('searchEvent')
        user_profile_table_name = table_names.get('userProfile')
        appsync_endpoint = payload.get('appsyncEndpoint')
        graphql_api_id = payload.get('graphqlApiId')
        
        if not grant_records_table_name or not search_event_table_name:
            raise ValueError("Missing required table names in payload")
        
        if not appsync_endpoint or not graphql_api_id:
            raise ValueError("Missing appsyncEndpoint or graphqlApiId in payload")
        
        if not session_id or not query:
            raise ValueError("Missing required parameters: sessionId, query")
        
        logger.info(f"[V2] Session: {session_id}, Query: {query}, User: {cognito_user_id}")
        logger.info(f"[V2] Using tables: {grant_records_table_name}, {search_event_table_name}")
        logger.info(f"[V2] AppSync endpoint: {appsync_endpoint}")
        
        # Set USER_PROFILE_TABLE environment variable BEFORE importing common.matching
        if user_profile_table_name:
            os.environ['USER_PROFILE_TABLE'] = user_profile_table_name
            logger.info(f"[V2] Set USER_PROFILE_TABLE: {user_profile_table_name}")
        
        # Set APPSYNC_ENDPOINT and GRAPHQL_API_ID for AppSync client
        os.environ['APPSYNC_ENDPOINT'] = appsync_endpoint
        os.environ['GRAPHQL_API_ID'] = graphql_api_id
        
        # NOW import common.matching (after setting environment variable)
        global BAYESIAN_SCORING_AVAILABLE, apply_dual_scoring, appsync_client
        try:
            from common.matching import apply_dual_scoring as _apply_dual_scoring
            apply_dual_scoring = _apply_dual_scoring
            BAYESIAN_SCORING_AVAILABLE = True
            logger.info("[V2] ✅ Successfully imported common.matching")
        except Exception as e:
            BAYESIAN_SCORING_AVAILABLE = False
            logger.error(f"[V2] ❌ Failed to import common.matching: {str(e)}")
            logger.error("[V2] ⚠️ Bayesian scoring will be DISABLED")
        
        # Initialize AppSync client for mutations (IAM auth only)
        try:
            from appsync_client import AppSyncClient
            appsync_client = AppSyncClient()
            logger.info("[V2] ✅ AppSync client initialized")
        except Exception as e:
            logger.error(f"[V2] ❌ Failed to initialize AppSync client: {str(e)}")
            appsync_client = None
        
        # Initialize tables with names from payload (make them global for background thread)
        grant_records_table = dynamodb.Table(grant_records_table_name)
        search_event_table = dynamodb.Table(search_event_table_name)
        
        # Start async task tracking
        task_id = app.add_async_task("grant_search", {
            "sessionId": session_id,
            "query": query,
            "cognitoUserId": cognito_user_id
        })
        
        logger.info(f"[V2] Started async task: {task_id}")
        
        # Spawn background thread for long-running work
        def background_search():
            try:
                logger.info(f"[V2] Background thread started for session {session_id}")
                
                # ============================================================
                # STEP 1: SEARCH GRANTS.GOV API
                # ============================================================
                logger.info(f"[V2] ========== STEP 1: SEARCHING GRANTS.GOV ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': 'Searching grants.gov...',
                    'step': 1,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                grants = search_grants_sync(query, filters)
                logger.info(f"[V2] ✅ Found {len(grants)} grants from API")
                
                # Log first grant details for debugging
                if grants:
                    sample = grants[0]
                    logger.info(f"[V2] 📋 Sample grant from API:")
                    logger.info(f"[V2]    - grantId: {sample.get('grantId')}")
                    logger.info(f"[V2]    - title: {sample.get('title', '')[:60]}...")
                    logger.info(f"[V2]    - agency: {sample.get('agency')}")
                    logger.info(f"[V2]    - amount: {sample.get('amount')}")
                    logger.info(f"[V2]    - deadline: {sample.get('deadline')}")
                    logger.info(f"[V2]    - description length: {len(sample.get('description', ''))}")
                    logger.info(f"[V2]    - eligibility length: {len(sample.get('eligibility', ''))}")
                    logger.info(f"[V2]    - source: {sample.get('source')}")
                
                # ============================================================
                # STEP 2: APPLY BAYESIAN SCORING
                # ============================================================
                logger.info(f"[V2] ========== STEP 2: APPLYING BAYESIAN SCORING ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': f'Found {len(grants)} grants, applying scoring...',
                    'step': 2,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                if BAYESIAN_SCORING_AVAILABLE and cognito_user_id:
                    logger.info(f"[V2] 🧠 Applying Bayesian scoring for user {cognito_user_id}")
                    scored_grants = apply_dual_scoring(grants, cognito_user_id, query, source='GRANTS_GOV')
                    logger.info(f"[V2] ✅ Bayesian scoring complete")
                else:
                    logger.warning("[V2] ⚠️  Bayesian scoring not available, using default scores")
                    logger.warning(f"[V2]    - BAYESIAN_SCORING_AVAILABLE: {BAYESIAN_SCORING_AVAILABLE}")
                    logger.warning(f"[V2]    - cognito_user_id: {cognito_user_id}")
                    # Set default scores when Bayesian scoring is unavailable
                    for grant in grants:
                        grant['relevanceScore'] = 0.5
                        grant['keywordScore'] = 0.5
                        grant['profileMatchScore'] = 0.0
                    scored_grants = grants
                    logger.info(f"[V2] ✅ Default scores applied to {len(scored_grants)} grants")
                
                # Log scored grant sample
                if scored_grants:
                    sample = scored_grants[0]
                    logger.info(f"[V2] 📋 Sample scored grant:")
                    logger.info(f"[V2]    - relevanceScore: {sample.get('relevanceScore')}")
                    logger.info(f"[V2]    - keywordScore: {sample.get('keywordScore')}")
                    logger.info(f"[V2]    - profileMatchScore: {sample.get('profileMatchScore')}")
                
                # ============================================================
                # STEP 3: WRITE TO DYNAMODB VIA APPSYNC
                # ============================================================
                logger.info(f"[V2] ========== STEP 3: WRITING TO DYNAMODB VIA APPSYNC ==========")
                publish_search_event(session_id, 'PROGRESS', {
                    'message': f'Writing {len(scored_grants)} grants to database...',
                    'step': 3,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                # 3. Write grants to DynamoDB AND AppSync
                for i, grant in enumerate(scored_grants):
                    logger.info(f"[V2] 📝 Writing grant {i+1}/{len(scored_grants)}: {grant.get('grantId')}")
                    write_grant_record(session_id, grant, grant_records_table, cognito_user_id)
                    
                    # Small delay for smooth progress updates
                    if i % 5 == 0:  # Every 5 grants
                        # nosemgrep: arbitrary-sleep - Intentional: Rate limiting for DynamoDB writes
                        time.sleep(0.1)
                
                logger.info(f"[V2] ✅ Wrote {len(scored_grants)} grants to DynamoDB")
                
                # Log top results summary for easy debugging
                top5 = sorted(scored_grants, key=lambda g: g.get('relevanceScore', 0), reverse=True)[:5]
                scores = [g.get('relevanceScore', 0) for g in scored_grants]
                score_range = f"{min(scores):.2f}–{max(scores):.2f}" if scores else "n/a"
                logger.info(f"[V2] 📊 SEARCH SUMMARY: {len(scored_grants)} grants, score range {score_range}")
                for rank, g in enumerate(top5, 1):
                    logger.info(f"[V2]   #{rank} [{g.get('relevanceScore', 0):.2f}] {g.get('title', '')[:80]} ({g.get('agency', '')})")
                
                # ============================================================
                # STEP 4: PUBLISH COMPLETION EVENT
                # ============================================================
                logger.info(f"[V2] ========== STEP 4: PUBLISHING COMPLETION EVENT ==========")
                publish_search_event(session_id, 'SEARCH_COMPLETE', {
                    'message': f'Search complete - found {len(scored_grants)} grants',
                    'totalGrants': len(scored_grants),
                    'step': 4,
                    'totalSteps': 4
                }, search_event_table, cognito_user_id)
                
                logger.info(f"[V2] ========== BACKGROUND SEARCH COMPLETE ==========")
                logger.info(f"[V2] ✅ Session {session_id} completed successfully")
                
            except Exception as e:
                logger.error(f"[V2] Error in background search: {str(e)}")
                
                # Publish error event
                publish_search_event(session_id, 'SEARCH_ERROR', {
                    'message': f'Search failed: {str(e)}',
                    'error': str(e)
                }, search_event_table, cognito_user_id)
                
            finally:
                # Mark async task as complete
                app.complete_async_task(task_id)
                logger.info(f"[V2] Async task {task_id} completed")
        
        # Start background thread
        threading.Thread(target=background_search, daemon=True).start()
        
        # Return immediately
        return {
            "status": "STARTED",
            "sessionId": session_id,
            "message": f"Search started for: {query}",
            "taskId": task_id,
            "version": "V2"
        }
        
    except Exception as e:
        logger.error(f"[V2] Error in entrypoint: {str(e)}")
        return {
            "status": "ERROR",
            "error": str(e),
            "version": "V2"
        }

# ============================================================================
# GRANTS.GOV API FUNCTIONS (same as V1)
# ============================================================================

# ============================================================================
# DYNAMODB FUNCTIONS - Agent writes directly (no processor Lambda)
# ============================================================================

def write_grant_record(session_id: str, grant: Dict[str, Any], table, cognito_user_id: str = None):
    """Write GrantRecord via AppSync mutation (which writes to DynamoDB and triggers subscriptions)"""
    try:
        if not appsync_client:
            logger.error("[V2] AppSync client not available, cannot write GrantRecord")
            return
        
        # Log what we received
        logger.info(f"[V2] 🔍 Grant object keys: {list(grant.keys())}")
        logger.info(f"[V2] 🔍 Grant data sample - grantId: {grant.get('grantId')}, title: {grant.get('title', '')[:50]}, agency: {grant.get('agency')}, amount: {grant.get('amount')}, description length: {len(grant.get('description', ''))}")
        
        # Prepare record for AppSync
        record = {
            "sessionId": session_id,
            "grantId": grant['grantId'],
            "title": grant['title'],
            "agency": grant.get('agency', ''),
            "amount": float(grant.get('amount', 0)),
            "deadline": grant.get('deadline', ''),
            "description": grant.get('description', ''),
            "eligibility": grant.get('eligibility', ''),
            "applicationProcess": grant.get('applicationProcess', ''),
            "source": grant.get('source', 'GRANTS_GOV'),
            "relevanceScore": float(grant.get('relevanceScore', 0.85)),
            # Use `is not None` so a legitimately-computed 0.0 score is preserved
            # rather than being coerced to null (matches the EU agent's fix).
            "profileMatchScore": float(grant.get('profileMatchScore', 0)) if grant.get('profileMatchScore') is not None else None,
            "keywordScore": float(grant.get('keywordScore', 0)) if grant.get('keywordScore') is not None else None,
            "matchedKeywords": grant.get('matchedKeywords', []),
            "tags": grant.get('tags', [])
        }
        
        logger.info(f"[V2] 🔍 Record being sent - agency: {record['agency']}, amount: {record['amount']}, description length: {len(record['description'])}")
        
        # Call AppSync mutation (which writes to DDB and triggers subscription)
        appsync_client.create_grant_record(record)
        logger.info(f"[V2] Created GrantRecord via AppSync: {grant['grantId']}")
        
    except Exception as e:
        logger.error(f"[V2] Error writing GrantRecord: {str(e)}")
        raise

def publish_search_event(session_id: str, event_type: str, data: Dict[str, Any], table, cognito_user_id: str = None):
    """Publish SearchEvent via AppSync mutation (which writes to DynamoDB and triggers subscriptions)"""
    if not appsync_client:
        error_msg = "[V2] AppSync client not available, cannot publish SearchEvent"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    # Call AppSync mutation (which writes to DDB and triggers subscription)
    success = appsync_client.create_search_event(
        session_id=session_id,
        event_type=event_type,
        data=data,
        cognito_user_id=cognito_user_id
    )
    
    if not success:
        error_msg = f"[V2] Failed to publish SearchEvent via AppSync: {event_type}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info(f"[V2] Published SearchEvent via AppSync: {event_type} for session {session_id}")

# ============================================================================
# RUN
# ============================================================================

if __name__ == "__main__":
    print("[US Grants V2] *** CODE VERSION: 2026-03-05-v2 — search summary logging ***", flush=True)
    logger.info("[V2] Starting US Grants Search Agent V2")
    app.run()
