"""Codex wire protocol, failure handling, and pipeline output compatibility."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import llm


def stream(text='{"answer":"ok"}', usage=None):
    return '\n'.join(json.dumps(e) for e in [
        {'type': 'thread.started', 'thread_id': 'thread-1'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': 's1', 'type': 'web_search'}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'progress'}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
        {'type': 'turn.completed', 'usage': usage or {'input_tokens': 100, 'cached_input_tokens': 60, 'output_tokens': 8}},
    ])


class Ledger:
    def __init__(self, folder):
        self.dir, self.costs = Path(folder), []

    def add_cost(self, stage, **fields):
        self.costs.append((stage, fields))


class CodexTests(unittest.TestCase):
    @patch('lib.llm.subprocess.run')
    def test_native_jsonl_and_chatgpt_environment(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stream(), '')
        with patch.dict(os.environ, {'CODEX_HOME': '/isolated', 'CODEX_API_KEY': 'secret',
                                     'OPENAI_API_KEY': 'secret', 'ANTHROPIC_API_KEY': 'secret', 'HQ_TOKEN': 'secret'}):
            text, meta = llm._codex_exec('system', 'user', web_search=True)
        self.assertEqual(json.loads(text), {'answer': 'ok'})
        self.assertEqual(meta['input_tokens'], 100)  # cached tokens are a subset, not added twice
        self.assertEqual(meta['cached_input_tokens'], 60)
        self.assertEqual(meta['web_searches'], 1)
        self.assertIsNone(meta['cost_usd'])
        self.assertEqual(meta['session_id'], 'thread-1')
        args, kw = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(args[1], 'exec')
        self.assertEqual(args[-1], '-')
        self.assertIn('web_search="live"', args)
        self.assertIn('features.shell_tool=false', args)
        self.assertIn('read-only', args)
        self.assertNotIn('user', args)
        self.assertIn('user', kw['input'])
        self.assertEqual(kw['env']['CODEX_HOME'], '/isolated')
        for key in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'HQ_TOKEN'):
            self.assertNotIn(key, kw['env'])
        self.assertIn('web_search="disabled"', llm.codex_command('test'))

    @patch('lib.llm.time.sleep')
    @patch('lib.llm.subprocess.run')
    def test_explicit_quota_returns_immediately(self, run, sleep):
        raw = json.dumps({'type': 'turn.failed', 'error': {'message': 'You have hit your usage limit'}})
        run.return_value = subprocess.CompletedProcess([], 1, raw, '')
        with self.assertRaisesRegex(llm.LLMError, '사용량 한도') as error:
            llm._codex_exec('', '')
        self.assertEqual(error.exception.raw, raw)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    @patch('lib.llm.time.sleep')
    @patch('lib.llm.subprocess.run')
    def test_incomplete_empty_and_nonzero_output_fail_closed(self, run, sleep):
        for code, raw in [(0, '{}'), (0, stream('')), (1, stream()),
                          (0, stream() + '\n' + json.dumps({'type': 'turn.failed', 'error': {'message': 'bad'}}))]:
            run.reset_mock()
            run.return_value = subprocess.CompletedProcess([], code, raw, '')
            with self.assertRaisesRegex(llm.LLMError, '실패'):
                llm._codex_exec('', '')
            self.assertEqual(run.call_count, 3)

    @patch('lib.llm.subprocess.run')
    def test_timeout_preserves_partial_stream(self, run):
        run.side_effect = subprocess.TimeoutExpired(['codex'], 1, output=b'partial')
        with self.assertRaisesRegex(llm.LLMError, '타임아웃') as error:
            llm._codex_exec('', '', timeout=1)
        self.assertEqual(error.exception.raw, 'partial')

    @patch('lib.llm.load_config', return_value={'llm': {'backend': 'codex', 'model': 'configured-model'}})
    @patch('lib.llm._codex_exec')
    def test_records_raw_before_json_parse_and_failures(self, execute, config):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(folder)
            execute.return_value = ('invalid JSON', {'backend': 'codex'})
            with self.assertRaisesRegex(RuntimeError, 'JSON 추출 실패'):
                llm.ask_json('s', 'u', ledger=ledger)
            execute.assert_called_with('s', 'u', web_search=False, timeout=900, model='configured-model')
            self.assertEqual(len(list((ledger.dir / 'llm_raw').iterdir())), 1)
            execute.side_effect = llm.LLMError('사용량 한도', 'failure raw', {'model': 'test'})
            with self.assertRaises(llm.LLMError):
                llm.ask_json('s', 'u', ledger=ledger)
            self.assertEqual(len(list((ledger.dir / 'llm_raw').iterdir())), 2)
            self.assertIn('error', ledger.costs[-1][1])

    @patch('lib.llm.subprocess.run')
    @patch('lib.llm.load_config', return_value={'llm': {'backend': 'anthropic_api'}})
    def test_removed_backend_does_not_fall_back(self, config, run):
        with self.assertRaisesRegex(llm.LLMError, 'codex만 지원'):
            llm.ask_json('', '')
        run.assert_not_called()

    def test_existing_json_contract(self):
        self.assertEqual(llm.extract_json('prose ```json\n{"blocks":[{"id":1}]}\n```'), {'blocks': [{'id': 1}]})


if __name__ == '__main__':
    unittest.main()
