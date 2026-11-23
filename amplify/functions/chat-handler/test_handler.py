"""Unit tests for the chat handler's pure logic (no AWS calls)."""
import os
import sys
from unittest.mock import MagicMock, patch

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ.setdefault('CHAT_SESSIONS_TABLE', 'test-chat-sessions')
os.environ.setdefault('CHAT_CONTEXT_BUCKET', 'test-chat-context')
os.environ.setdefault('APPSYNC_API_ID', 'test-api')
os.environ.setdefault('APPSYNC_ENDPOINT', 'https://example.invalid/graphql')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handler  # noqa: E402


class TestBuildConversation:
    def test_plain_history_is_preserved(self):
        history = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
        assert handler.build_conversation(history, 'next') == [
            {'role': 'user', 'content': 'hi'},
            {'role': 'assistant', 'content': 'hello'},
            {'role': 'user', 'content': 'next'},
        ]

    def test_empty_history(self):
        assert handler.build_conversation([], 'q') == [{'role': 'user', 'content': 'q'}]

    def test_empty_assistant_turn_is_dropped_and_user_turns_merge(self):
        history = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': ''}]
        convo = handler.build_conversation(history, 'b')
        assert convo == [{'role': 'user', 'content': 'a\n\nb'}]

    def test_never_starts_with_assistant(self):
        history = [{'role': 'assistant', 'content': 'orphan'}, {'role': 'user', 'content': 'u'},
                   {'role': 'assistant', 'content': 'r'}]
        convo = handler.build_conversation(history, 'q')
        assert convo[0]['role'] == 'user'
        assert [m['role'] for m in convo] == ['user', 'assistant', 'user']

    def test_roles_always_alternate_and_content_non_empty(self):
        history = [{'role': 'user', 'content': 'a'}, {'role': 'user', 'content': 'b'},
                   {'role': 'assistant', 'content': '   '}, {'role': 'system', 'content': 'x'},
                   {'role': 'assistant', 'content': 'c'}, {'content': 'no role'}]
        convo = handler.build_conversation(history, 'q')
        roles = [m['role'] for m in convo]
        assert all(a != b for a, b in zip(roles, roles[1:]))
        assert all(m['content'].strip() for m in convo)
        assert roles[0] == 'user' and roles[-1] == 'user'

    def test_only_last_messages_are_used(self):
        history = []
        for i in range(20):
            history += [{'role': 'user', 'content': f'u{i}'}, {'role': 'assistant', 'content': f'a{i}'}]
        convo = handler.build_conversation(history, 'q', max_history=4)
        assert [m['content'] for m in convo] == ['u18', 'a18', 'u19', 'a19', 'q']


class TestListSessions:
    def _run(self, items, arguments):
        table = MagicMock()
        table.query.return_value = {'Items': items}
        with patch.object(handler.dynamodb, 'Table', return_value=table):
            result = handler.handle_list_sessions({'identity': {'sub': 'user-1'}, 'arguments': arguments}, None)
        return result, table

    def test_null_limit_falls_back_to_default(self):
        _, table = self._run([], {'limit': None})
        assert table.query.call_args.kwargs['Limit'] == 10

    def test_limit_is_clamped(self):
        _, table = self._run([], {'limit': 5000})
        assert table.query.call_args.kwargs['Limit'] == 100

    def test_malformed_item_does_not_blank_the_list(self):
        items = [
            {'SK': 'SESSION#partial', 'updatedAt': '2026-01-01T00:00:00Z', 'messages': [{}]},
            {'sessionId': 's2', 'userId': 'user-1', 'createdAt': 'c', 'updatedAt': 'u'},
        ]
        result, _ = self._run(items, {})
        assert [s['sessionId'] for s in result] == ['partial', 's2']
        assert result[0]['userId'] == 'user-1' and result[0]['messageCount'] == 1
