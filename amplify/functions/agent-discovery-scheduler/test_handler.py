"""Unit tests for the agent discovery scheduler (no AWS calls)."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_REGION', 'us-east-1')
os.environ['STATE_MACHINE_ARN'] = 'arn:aws:states:us-east-1:123456789012:stateMachine:test'
os.environ['AGENT_CONFIG_TABLE'] = 'test-agent-config'

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handler  # noqa: E402


def _run(configs, event=None, start_side_effect=None):
    table = MagicMock()
    table.scan.return_value = {'Items': configs}
    sfn = MagicMock()
    if start_side_effect is not None:
        sfn.start_execution.side_effect = start_side_effect
    else:
        sfn.start_execution.side_effect = lambda **kw: {'executionArn': f"arn:exec:{kw['name']}"}
    with patch.object(handler.dynamodb, 'Table', return_value=table), patch.object(handler, 'sfn_client', sfn):
        return handler.handler(event or {}, None), sfn


def _old(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_two_configs_for_one_user_get_distinct_execution_names():
    configs = [{'id': 'cfg-a', 'userId': 'user-0001-same'}, {'id': 'cfg-b', 'userId': 'user-0001-same'}]
    result, sfn = _run(configs)
    names = [c.kwargs['name'] for c in sfn.start_execution.call_args_list]
    assert result['executionsStarted'] == 2 and len(set(names)) == 2
    assert all(len(n) <= 80 for n in names)


def test_execution_name_is_valid_for_unusual_ids():
    result, sfn = _run([{'id': 'cfg with spaces/and:colons' + 'x' * 100, 'userId': 'u'}])
    name = sfn.start_execution.call_args.kwargs['name']
    assert result['executionsStarted'] == 1 and len(name) <= 80
    assert all(ch.isalnum() or ch in '-_' for ch in name)


def test_one_bad_config_does_not_stop_the_rest():
    configs = [
        {'id': 'bad-date', 'userId': 'u1', 'lastRun': 'not-a-date'},
        {'id': 'no-user'},
        {'id': 'good', 'userId': 'u2'},
    ]
    result, sfn = _run(configs)
    assert result['executionsStarted'] == 1 and result['failed'] == 2
    assert result['executions'][0]['configId'] == 'good'


def test_start_execution_failure_is_isolated():
    calls = {'n': 0}

    def flaky(**kw):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('ExecutionAlreadyExists')
        return {'executionArn': 'arn:ok'}

    result, _ = _run([{'id': 'c1', 'userId': 'u1'}, {'id': 'c2', 'userId': 'u2'}], start_side_effect=flaky)
    assert result['executionsStarted'] == 1 and result['failed'] == 1


def test_naive_last_run_timestamp_is_treated_as_utc():
    naive_old = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(tzinfo=None).isoformat()
    result, _ = _run([{'id': 'c', 'userId': 'u', 'lastRun': naive_old, 'timeInterval': 24}])
    assert result['executionsStarted'] == 1 and result['failed'] == 0


def test_interval_is_respected():
    configs = [
        {'id': 'recent', 'userId': 'u1', 'lastRun': _old(2), 'timeInterval': 24},
        {'id': 'due', 'userId': 'u2', 'lastRun': _old(25), 'timeInterval': 24},
    ]
    result, _ = _run(configs)
    assert [e['configId'] for e in result['executions']] == ['due']


def test_force_run_ignores_interval():
    result, _ = _run([{'id': 'recent', 'userId': 'u1', 'lastRun': _old(1)}], event={'source': 'manual-force-run'})
    assert result['executionsStarted'] == 1
