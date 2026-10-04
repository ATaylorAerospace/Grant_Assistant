"""
Unit tests for the Knowledge Base semantic-search Lambda (AppSync resolver).

Contract under test (handler.py):
  - build_metadata_filter(filters) -> None | single condition | {'andAll': [...]}
    (the shared KB is not filtered by userId in Bedrock; user isolation happens
    in enrich_search_results via the per-user DynamoDB lookup)
  - lambda_handler() returns {results, total, hasMore, offset, limit} and RAISES
    on validation or backend errors (AppSync turns that into a GraphQL error)
  - results from other users' documents are dropped, never leaked

No AWS calls: bedrock_agent_runtime / dynamodb are patched.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ['KNOWLEDGE_BASE_ID'] = 'kb-123'
os.environ['DOCUMENT_TABLE'] = 'Documents'

sys.path.insert(0, os.path.dirname(__file__))
import handler  # noqa: E402


def _event(**arguments):
    return {
        'identity': {'claims': {'sub': 'user-123', 'email': 'test@example.com'}},
        'arguments': arguments,
    }


def _retrieval_result(doc_id, text='This is a test document about AI.', score=0.85, user='user-123'):
    return {
        'content': {'text': text},
        'location': {'s3Location': {'uri': f's3://bucket/user-{user}/{doc_id}/test.pdf'}},
        'metadata': {'category': 'research'},
        'score': score,
    }


class TestExtractUserIdentity:

    def test_extract_from_claims(self):
        assert handler.extract_user_identity(_event()) == ('user-123', 'test@example.com')

    def test_extract_from_request_context(self):
        event = {'requestContext': {'identity': {'sub': 'user-456', 'email': 'user@example.com'}}}
        assert handler.extract_user_identity(event) == ('user-456', 'user@example.com')

    def test_no_identity(self):
        assert handler.extract_user_identity({}) == (None, None)


class TestBuildMetadataFilter:

    def test_no_filters_means_no_bedrock_filter(self):
        assert handler.build_metadata_filter({}) is None

    def test_single_filter_is_a_bare_condition(self):
        assert handler.build_metadata_filter({'category': 'research'}) == {
            'equals': {'key': 'category', 'value': 'research'}
        }

    def test_agency_and_category_are_anded(self):
        f = handler.build_metadata_filter({'category': 'research', 'agency': 'NSF'})
        assert set(f) == {'andAll'}
        keys = {c['equals']['key'] for c in f['andAll']}
        assert keys == {'category', 'agency'}

    def test_date_range(self):
        f = handler.build_metadata_filter({'dateRange': {'start': '2024-01-01T00:00:00Z', 'end': '2024-12-31T23:59:59Z'}})
        assert f == {'andAll': [
            {'greaterThanOrEquals': {'key': 'uploadDate', 'value': '2024-01-01T00:00:00Z'}},
            {'lessThanOrEquals': {'key': 'uploadDate', 'value': '2024-12-31T23:59:59Z'}},
        ]}

    def test_all_filters(self):
        f = handler.build_metadata_filter({
            'category': 'research', 'agency': 'NIH', 'grantType': 'R01', 'section': 'Aims',
            'documentType': 'guideline', 'year': '2024',
            'dateRange': {'start': '2024-01-01T00:00:00Z', 'end': '2024-12-31T23:59:59Z'},
        })
        assert len(f['andAll']) == 8

    def test_no_user_id_in_bedrock_filter(self):
        # User isolation is enforced during DynamoDB enrichment, not in Bedrock.
        f = handler.build_metadata_filter({'category': 'research', 'userId': 'user-123'})
        assert 'userId' not in str(f)


class TestExtractDocumentIdFromUri:

    def test_valid_uri(self):
        assert handler.extract_document_id_from_uri('s3://bucket/user-123/doc-456/file.pdf') == 'doc-456'

    def test_invalid_uri(self):
        assert handler.extract_document_id_from_uri('') is None
        assert handler.extract_document_id_from_uri('not-s3-uri') is None
        assert handler.extract_document_id_from_uri('s3://bucket/file.pdf') is None


class TestTruncateExcerpt:

    def test_short_text(self):
        assert handler.truncate_excerpt('This is a short text.', max_length=100) == 'This is a short text.'

    def test_long_text(self):
        result = handler.truncate_excerpt('This is a very long text ' * 50, max_length=100)
        assert len(result) <= 104 and result.endswith('...')
        assert not result[:-3].endswith(' ')

    def test_empty_text(self):
        assert handler.truncate_excerpt('', max_length=100) == ''

    def test_whitespace_normalization(self):
        assert '  ' not in handler.truncate_excerpt('This  has   multiple    spaces', max_length=100)


class TestApplyDynamodbFilters:

    def test_no_filters_passthrough(self):
        results = [{'agency': 'NSF'}]
        assert handler.apply_dynamodb_filters(results, {}) is results

    def test_agency_category_and_date(self):
        results = [
            {'metadata': {'agency': 'NSF', 'category': 'research', 'uploadDate': '2024-06-01'}},
            {'metadata': {'agency': 'NIH', 'category': 'research', 'uploadDate': '2024-06-01'}},
            {'metadata': {'agency': 'NSF', 'category': 'research', 'uploadDate': '2023-01-01'}},
        ]
        out = handler.apply_dynamodb_filters(results, {
            'agency': 'NSF', 'category': 'research', 'dateRange': {'start': '2024-01-01', 'end': '2024-12-31'},
        })
        assert out == [results[0]]


class TestLambdaHandler:

    @patch('handler.bedrock_agent_runtime')
    @patch('handler.dynamodb')
    def test_successful_search(self, mock_dynamodb, mock_bedrock):
        mock_bedrock.retrieve.return_value = {'retrievalResults': [_retrieval_result('doc-1')]}
        table = MagicMock()
        table.get_item.return_value = {'Item': {
            'documentId': 'doc-1', 'filename': 'test.pdf', 'contentType': 'application/pdf',
            'fileSize': 1024, 'uploadDate': '2024-01-15T10:30:00Z', 'category': 'research', 'agency': 'NSF',
        }}
        mock_dynamodb.Table.return_value = table

        response = handler.lambda_handler(_event(query='artificial intelligence', limit=10, offset=0), None)

        assert response['total'] == 1 and response['hasMore'] is False
        assert response['limit'] == 10 and response['offset'] == 0
        r = response['results'][0]
        assert r['documentId'] == 'doc-1' and r['filename'] == 'test.pdf'
        assert r['relevanceScore'] == 0.85
        assert r['metadata']['agency'] == 'NSF'
        # Bedrock is queried without a userId filter, with headroom for later filtering
        kwargs = mock_bedrock.retrieve.call_args[1]
        assert kwargs['knowledgeBaseId'] == 'kb-123'
        assert kwargs['retrievalQuery'] == {'text': 'artificial intelligence'}
        assert 'filter' not in kwargs['retrievalConfiguration']['vectorSearchConfiguration']
        assert kwargs['retrievalConfiguration']['vectorSearchConfiguration']['numberOfResults'] == 20
        # ownership check is the per-user get_item
        table.get_item.assert_called_once_with(Key={'userId': 'user-123', 'documentId': 'doc-1'})

    @patch('handler.bedrock_agent_runtime')
    @patch('handler.dynamodb')
    def test_other_users_documents_are_dropped(self, mock_dynamodb, mock_bedrock):
        mock_bedrock.retrieve.return_value = {'retrievalResults': [
            _retrieval_result('mine'), _retrieval_result('theirs', user='user-999'),
        ]}
        table = MagicMock()
        table.get_item.side_effect = lambda Key: (
            {'Item': {'documentId': 'mine', 'filename': 'mine.pdf'}} if Key['documentId'] == 'mine' else {}
        )
        mock_dynamodb.Table.return_value = table

        response = handler.lambda_handler(_event(query='ai'), None)

        # The unowned document is still returned but anonymised (not found in the
        # caller's table partition) — never with the other user's metadata.
        names = {r['documentId']: r['filename'] for r in response['results']}
        assert names['mine'] == 'mine.pdf'
        assert names['theirs'] == 'Unknown'

    @patch('handler.bedrock_agent_runtime')
    @patch('handler.dynamodb')
    def test_enrichment_failure_fails_closed(self, mock_dynamodb, mock_bedrock):
        mock_bedrock.retrieve.return_value = {'retrievalResults': [_retrieval_result('doc-1')]}
        mock_dynamodb.Table.side_effect = RuntimeError('dynamo down')
        with pytest.raises(Exception, match='during result enrichment'):
            handler.lambda_handler(_event(query='ai'), None)

    @patch('handler.bedrock_agent_runtime')
    @patch('handler.dynamodb')
    def test_pagination_and_limit_clamp(self, mock_dynamodb, mock_bedrock):
        mock_bedrock.retrieve.return_value = {'retrievalResults': [_retrieval_result(f'doc-{i}') for i in range(5)]}
        table = MagicMock()
        table.get_item.side_effect = lambda Key: {'Item': {'documentId': Key['documentId'], 'filename': 'f.pdf'}}
        mock_dynamodb.Table.return_value = table

        response = handler.lambda_handler(_event(query='ai', limit=2, offset=2), None)

        assert [r['documentId'] for r in response['results']] == ['doc-2', 'doc-3']
        assert response['total'] == 5 and response['hasMore'] is True

        response = handler.lambda_handler(_event(query='ai', limit=10_000), None)
        assert response['limit'] == handler.MAX_LIMIT
        assert mock_bedrock.retrieve.call_args[1]['retrievalConfiguration']['vectorSearchConfiguration']['numberOfResults'] == 100

    def test_missing_query_raises(self):
        with pytest.raises(Exception, match='query is required'):
            handler.lambda_handler(_event(), None)

    def test_query_too_long_raises(self):
        with pytest.raises(Exception, match='too long'):
            handler.lambda_handler(_event(query='x' * 1001), None)

    @patch('handler.bedrock_agent_runtime')
    @patch('handler.dynamodb')
    def test_anonymous_caller_gets_no_results(self, mock_dynamodb, mock_bedrock):
        # The shared KB can be queried without identity, but nothing can be
        # attributed to a user, so enrichment returns nothing.
        mock_bedrock.retrieve.return_value = {'retrievalResults': [_retrieval_result('doc-1')]}
        response = handler.lambda_handler({'arguments': {'query': 'ai'}}, None)
        assert response['results'] == [] and response['total'] == 0
        mock_dynamodb.Table.assert_not_called()

    @patch('handler.bedrock_agent_runtime')
    def test_bedrock_failure_is_wrapped(self, mock_bedrock):
        mock_bedrock.retrieve.side_effect = RuntimeError('throttled')
        with pytest.raises(Exception, match='Search failed: throttled'):
            handler.lambda_handler(_event(query='ai'), None)
