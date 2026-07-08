"""
Lambda handler for querying proposals
Updated: 2026-02-01 - Removed presigned URL generation (now using Lambda proxy for downloads)
"""
import json
import os
import boto3
from datetime import datetime
from boto3.dynamodb.conditions import Key
from decimal import Decimal

dynamodb = boto3.resource('dynamodb')

def decimal_to_float(obj):
    """
    Recursively convert all Decimal values to float in nested structures
    """
    if isinstance(obj, list):
        return [decimal_to_float(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: decimal_to_float(value) for key, value in obj.items()}
    elif isinstance(obj, Decimal):
        return float(obj)
    else:
        return obj

def handler(event, context):
    """
    Handle proposal queries
    """
    print(f"📥 Event: {json.dumps(event)}")
    
    # Get table name from environment
    table_name = os.environ.get('PROPOSAL_TABLE_NAME')
    if not table_name:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'PROPOSAL_TABLE_NAME not configured'})
        }
    
    table = dynamodb.Table(table_name)
    
    # Get the field name and arguments
    # AppSync Direct Lambda resolvers have fieldName at top level
    field_name = event.get('fieldName') or event.get('info', {}).get('fieldName')
    arguments = event.get('arguments', {})

    # SECURITY: derive the user from the authenticated Cognito identity, never
    # from a client-supplied argument. Using arguments['userId'] directly allowed
    # any authenticated user to read another user's proposals (IDOR).
    identity = event.get('identity', {})
    authed_user_id = identity.get('sub') or identity.get('username')

    print(f"🔍 Field: {field_name}, Authenticated user: {authed_user_id}")

    try:
        if field_name == 'listProposalsByUser':
            if not authed_user_id:
                return {'error': 'Unauthorized - no user identity', 'items': []}

            requested_user_id = arguments.get('userId')
            if requested_user_id and requested_user_id != authed_user_id:
                print(f"❌ Access denied: requested userId={requested_user_id}, caller={authed_user_id}")
                return {'error': 'Access denied - you can only list your own proposals', 'items': []}

            user_id = authed_user_id

            # Query using GSI
            response = table.query(
                IndexName='proposalsByUserId',
                KeyConditionExpression=Key('userId').eq(user_id),
                ScanIndexForward=False  # Sort by most recent first
            )
            
            items = response.get('Items', [])
            print(f"✅ Found {len(items)} proposals for user {user_id}")
            
            # Convert ALL Decimal values to float (DynamoDB returns Decimals, but JSON doesn't support them)
            items = decimal_to_float(items)
            print(f"✅ Converted all Decimal values to float")
            
            # Note: Presigned URLs removed - downloads now use Lambda proxy (downloadProposal query)
            # This eliminates credential expiration issues and works with Block Public Access
            
            return {
                'items': items
            }
        
        else:
            return {'error': f'Unknown field: {field_name}', 'items': []}

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        # Return a consistent object shape (not a JSON string) so the client can
        # reliably distinguish success from failure.
        return {'error': str(e), 'items': []}
