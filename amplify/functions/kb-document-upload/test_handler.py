"""
Unit tests for the Knowledge Base document-upload Lambda (AppSync resolver).

Contract under test (handler.py):
  - validate_file() returns (error_message | None, cleaned_filename)
  - lambda_handler() returns the upload record directly on success and RAISES
    on any error — AppSync turns the exception into a GraphQL error; there is
    no HTTP statusCode envelope on this path.
  - success_response()/error_response() still exist for non-AppSync callers.

No AWS calls: s3_client / dynamodb are patched.
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

# Placeholder config so module-level boto3 clients construct without an account.
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ['DOCUMENT_BUCKET'] = 'test-bucket'
os.environ['DOCUMENT_TABLE'] = 'test-table'

sys.path.insert(0, os.path.dirname(__file__))
import handler  # noqa: E402


def _event(**input_fields):
    return {
        'identity': {'claims': {'sub': 'user-123', 'email': 'test@example.com'}},
        'arguments': {'input': input_fields},
    }


class TestExtractUserIdentity(unittest.TestCase):

    def test_from_claims(self):
        user_id, email = handler.extract_user_identity(_event())
        self.assertEqual(user_id, 'user-123')
        self.assertEqual(email, 'test@example.com')

    def test_from_request_context(self):
        event = {'requestContext': {'identity': {'sub': 'user-456', 'email': 'u@example.com'}}}
        user_id, email = handler.extract_user_identity(event)
        self.assertEqual(user_id, 'user-456')
        self.assertEqual(email, 'u@example.com')

    def test_missing(self):
        user_id, email = handler.extract_user_identity({})
        self.assertIsNone(user_id)
        self.assertIsNone(email)


class TestValidateFile(unittest.TestCase):
    """validate_file -> (error | None, cleaned_filename)"""

    def test_valid_pdf(self):
        self.assertEqual(handler.validate_file('document.pdf', 'application/pdf', 1024000), (None, 'document.pdf'))

    def test_valid_docx(self):
        error, name = handler.validate_file(
            'document.docx',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            2048000,
        )
        self.assertIsNone(error)
        self.assertEqual(name, 'document.docx')

    def test_valid_txt(self):
        self.assertEqual(handler.validate_file('notes.txt', 'text/plain', 512000), (None, 'notes.txt'))

    def test_filename_is_trimmed(self):
        error, name = handler.validate_file('  padded.pdf  ', 'application/pdf', 1024)
        self.assertIsNone(error)
        self.assertEqual(name, 'padded.pdf')

    def test_invalid_content_type(self):
        error, _ = handler.validate_file('image.jpg', 'image/jpeg', 1024000)
        self.assertIn('Invalid content type', error)

    def test_extension_mismatch(self):
        error, _ = handler.validate_file('document.txt', 'application/pdf', 1024000)
        self.assertIn('extension does not match', error)

    def test_file_too_large(self):
        error, _ = handler.validate_file('large.pdf', 'application/pdf', 100 * 1024 * 1024)
        self.assertIn('too large', error)

    def test_file_too_small(self):
        error, _ = handler.validate_file('empty.pdf', 'application/pdf', 0)
        self.assertIn('too small', error)

    def test_path_separators_rejected(self):
        error, _ = handler.validate_file('file/with/slashes.pdf', 'application/pdf', 1024)
        self.assertIn('path separators', error)
        error, _ = handler.validate_file('back\\slash.pdf', 'application/pdf', 1024)
        self.assertIn('path separators', error)

    def test_path_traversal_rejected(self):
        # Extension check fires first for '../../../etc/passwd' (no .txt); either
        # way the upload is refused and no S3 key can escape the user prefix.
        error, _ = handler.validate_file('../../../etc/passwd', 'text/plain', 1024)
        self.assertIsNotNone(error)
        error, _ = handler.validate_file('../../escape.txt', 'text/plain', 1024)
        self.assertIn('path separators', error)

    def test_filename_too_long(self):
        error, _ = handler.validate_file('a' * 300 + '.pdf', 'application/pdf', 1024)
        self.assertIn('filename length', error)


class TestGeneratePresignedUploadUrl(unittest.TestCase):

    @patch('handler.s3_client')
    def test_presigned_url(self, mock_s3):
        mock_s3.generate_presigned_url.return_value = 'https://s3.amazonaws.com/presigned-url'
        url = handler.generate_presigned_upload_url(
            bucket='test-bucket', key='user-123/doc.pdf', content_type='application/pdf', expiration=3600,
        )
        self.assertEqual(url, 'https://s3.amazonaws.com/presigned-url')
        mock_s3.generate_presigned_url.assert_called_once()


class TestCreateDocumentMetadata(unittest.TestCase):

    @patch('handler.dynamodb')
    def test_put_item(self, mock_dynamodb):
        mock_table = Mock()
        mock_dynamodb.Table.return_value = mock_table

        handler.create_document_metadata(
            document_id='doc-123', user_id='user-456', filename='test.pdf',
            content_type='application/pdf', file_size=1024000,
            s3_key='user-456/doc-123/test.pdf', s3_bucket='test-bucket', category='research',
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]['Item']
        self.assertEqual(item['documentId'], 'doc-123')
        self.assertEqual(item['userId'], 'user-456')
        self.assertEqual(item['filename'], 'test.pdf')
        self.assertEqual(item['status'], 'uploading')
        self.assertEqual(item['category'], 'research')
        self.assertFalse(item['vectorIndexed'])


class TestLambdaHandler(unittest.TestCase):

    @patch('handler.s3_client')
    @patch('handler.dynamodb')
    def test_success_returns_upload_record(self, mock_dynamodb, mock_s3):
        mock_s3.generate_presigned_url.return_value = 'https://s3.amazonaws.com/upload-url'
        mock_dynamodb.Table.return_value = Mock()

        response = handler.lambda_handler(
            _event(filename='test.pdf', contentType='application/pdf', fileSize=1024000, category='research'),
            None,
        )

        self.assertEqual(response['uploadUrl'], 'https://s3.amazonaws.com/upload-url')
        self.assertEqual(response['status'], 'uploading')
        self.assertEqual(response['s3Bucket'], 'test-bucket')
        self.assertEqual(response['expiresIn'], handler.PRESIGNED_URL_EXPIRATION)
        self.assertTrue(response['s3Key'].startswith('user-user-123/'))
        self.assertTrue(response['s3Key'].endswith('/test.pdf'))
        self.assertIn(response['documentId'], response['s3Key'])
        mock_dynamodb.Table.return_value.put_item.assert_called_once()

    def test_missing_user_identity_raises(self):
        event = {'arguments': {'input': {'filename': 'test.pdf', 'contentType': 'application/pdf', 'fileSize': 1}}}
        with self.assertRaisesRegex(Exception, 'Unauthorized'):
            handler.lambda_handler(event, None)

    def test_missing_required_fields_raises(self):
        with self.assertRaisesRegex(Exception, 'Missing required fields'):
            handler.lambda_handler(_event(filename='test.pdf'), None)

    def test_invalid_file_type_raises(self):
        with self.assertRaisesRegex(Exception, 'Invalid content type'):
            handler.lambda_handler(_event(filename='image.jpg', contentType='image/jpeg', fileSize=1024000), None)

    def test_file_too_large_raises(self):
        with self.assertRaisesRegex(Exception, 'too large'):
            handler.lambda_handler(
                _event(filename='huge.pdf', contentType='application/pdf', fileSize=100 * 1024 * 1024), None,
            )

    @patch('handler.s3_client')
    @patch('handler.dynamodb')
    def test_presign_failure_is_wrapped(self, mock_dynamodb, mock_s3):
        mock_s3.generate_presigned_url.side_effect = RuntimeError('boom')
        with self.assertRaisesRegex(Exception, 'Failed to generate upload URL'):
            handler.lambda_handler(_event(filename='t.pdf', contentType='application/pdf', fileSize=10), None)
        mock_dynamodb.Table.return_value.put_item.assert_not_called()


class TestResponseHelpers(unittest.TestCase):

    def test_success_response(self):
        response = handler.success_response({'key': 'value'})
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(json.loads(response['body']), {'key': 'value'})
        self.assertIn('Content-Type', response['headers'])

    def test_error_response(self):
        response = handler.error_response(400, 'Test error')
        self.assertEqual(response['statusCode'], 400)
        self.assertEqual(json.loads(response['body'])['error'], 'Test error')
        self.assertIn('Content-Type', response['headers'])


if __name__ == '__main__':
    unittest.main()
