import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
import shutil
import subprocess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location("consult", Path(__file__).parents[1] / "scripts/consult.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class Contracts(unittest.TestCase):
    def test_effort_cannot_be_overridden_by_parent(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_EFFORT_LEVEL": "low"}, clear=True):
            self.assertEqual(c.child_environment("high")["CLAUDE_CODE_EFFORT_LEVEL"], "high")
            self.assertEqual(os.environ["CLAUDE_CODE_EFFORT_LEVEL"], "low")

    def test_rejects_token_and_gateway_without_echoing_secret(self):
        for key, value in [("CLAUDE_CODE_OAUTH_TOKEN", "secret-value"),
                           ("ANTHROPIC_AUTH_TOKEN", "secret-value"),
                           ("ANTHROPIC_BASE_URL", "https://unapproved.test")]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}, clear=True):
                with self.assertRaises(ValueError) as err:
                    c.child_environment("high")
                self.assertNotIn(value, str(err.exception))

    def test_explicit_own_api_key_is_passed_not_printed(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-not-real"}, clear=True):
            self.assertEqual(c.child_environment("high")["ANTHROPIC_API_KEY"], "test-not-real")

    def test_rejects_aliases_and_command_injection(self):
        for model in ["opus", "claude-opus-5-5;touch x", "--bad", "$(echo bad)"]:
            with self.subTest(model=model), self.assertRaises(ValueError):
                c.validate_options(model, "high", 60)

    def test_invalid_timeout_and_effort(self):
        for effort, timeout in [("ultra", 10), ("high", -1), ("high", True), ("high", 3601)]:
            with self.assertRaises(ValueError):
                c.validate_options("claude-opus-5-5", effort, timeout)

    def test_no_write_shell_mcp_or_fallback(self):
        cmd = c.build_command("claude", "claude-opus-5-5", "high", "session", False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertIn("--restricted", cmd)
        self.assertIn("--safe-mode", cmd)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertEqual(settings["availableModels"], ["claude-opus-5-5"])
        self.assertFalse(settings["switchModelsOnFlag"])
        self.assertEqual(settings["fallbackModel"], [])

    def test_execution_requires_strict_sandbox_and_scoped_edits(self):
        cwd = Path('/tmp/example-project')
        cmd = c.build_command('claude', 'claude-opus-5-5', 'high', 'session', False,
                              'execute', cwd, {'EXAMPLE_SECRET': 'never-log-this'})
        settings = json.loads(cmd[cmd.index('--settings') + 1])
        sandbox = settings['sandbox']
        self.assertTrue(sandbox['enabled'])
        self.assertTrue(sandbox['failIfUnavailable'])
        self.assertFalse(sandbox['allowUnsandboxedCommands'])
        self.assertEqual(sandbox['excludedCommands'], [])
        self.assertFalse(sandbox['filesystem']['disabled'])
        self.assertEqual(sandbox['filesystem']['allowWrite'], [])
        self.assertEqual(sandbox['network']['allowedDomains'], [])
        self.assertTrue(sandbox['network']['strictAllowlist'])
        self.assertFalse(sandbox['allowAppleEvents'])
        self.assertIn({'name': 'EXAMPLE_SECRET', 'mode': 'deny'}, sandbox['credentials']['envVars'])
        self.assertNotIn('never-log-this', json.dumps(settings))
        self.assertIn('Bash', cmd[cmd.index('--tools') + 1])
        self.assertIn('Bash', cmd[cmd.index('--allowedTools') + 1])
        self.assertNotIn('Write', cmd[cmd.index('--allowedTools') + 1])

    def test_refuses_overbroad_execution_directories(self):
        for cwd in [Path('/'), Path.home().resolve(), c.ROOT, c.ROOT.parent, c.ROOT / 'scripts', Path('/tmp/a*')]:
            with self.subTest(cwd=cwd), self.assertRaises(ValueError):
                c.execution_settings(cwd, {})

    def test_execution_refuses_unvalidated_old_cli(self):
        for version in ['2.1.310 (Claude Code)', 'unknown']:
            with patch.object(c.subprocess, 'run') as run:
                run.return_value.stdout = version
                with self.assertRaises(ValueError):
                    c.verify_execution_cli('claude', {})
        with patch.object(c.subprocess, 'run') as run:
            run.return_value.stdout = '2.1.311 (Claude Code)'
            c.verify_execution_cli('claude', {})

    def test_execution_cannot_overwrite_its_result_directory(self):
        cwd = Path('/tmp/project')
        for out in [cwd, cwd / 'work/report']:
            with self.assertRaises(ValueError):
                c.validate_output_location(out, cwd, 'execute')
        c.validate_output_location(Path('/tmp/results'), cwd, 'execute')
        c.validate_output_location(cwd / 'report', cwd, 'review')

    def test_resume_cannot_silently_change_modes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'result.json'
            for mode, policy in c.POLICIES.items():
                data = {'schema': 'claude-review/v1', 'policy': policy, 'cwd': d,
                        'session_id': '12345678-1234-4234-8234-123456789012'}
                path.write_text(json.dumps(data))
                self.assertEqual(c.load_session(path, Path(d))['mode'], mode)
                other = 'execute' if mode == 'review' else 'review'
                with self.assertRaises(ValueError):
                    c.load_session(path, Path(d), other)

    def test_changed_model_is_detected(self):
        events = [{"type": "assistant", "message": {"model": "claude-sonnet-5-5"}},
                  {"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5-5": {}}}]
        final, observed, mismatch, malformed = c.summarize_stream(
            io.StringIO("\n".join(map(json.dumps, events))), "claude-opus-5-5")
        self.assertEqual(mismatch, ["claude-sonnet-5-5"])
        self.assertEqual(malformed, 0)

    def test_unparseable_output_is_not_silently_accepted(self):
        final, observed, mismatch, malformed = c.summarize_stream(io.StringIO("not json\n"), "claude-opus-5-5")
        self.assertIsNone(final)
        self.assertEqual(malformed, 1)

    def test_malformed_event_fields_are_recorded_not_crashed(self):
        events = [{'type': 'assistant', 'message': None},
                  {'type': 'assistant', 'message': {'model': 42}},
                  {'type': 'result', 'modelUsage': None}]
        final, observed, mismatch, malformed = c.summarize_stream(
            io.StringIO('\n'.join(map(json.dumps, events))), 'claude-opus-5-5')
        self.assertEqual(malformed, 3)
        self.assertEqual(observed, [])

    def test_cleanup_kills_group_after_leader_has_exited(self):
        from unittest.mock import Mock
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch.object(c.os, 'killpg') as kill:
            c.stop_process(process)
            self.assertEqual(kill.call_args_list[0].args, (12345, c.signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (12345, c.signal.SIGKILL))
            process.wait.assert_called_once()

    def test_resume_is_bound_to_cwd_and_policy(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "result.json"
            data = {"schema": "claude-review/v1", "policy": "read-only-v1", "cwd": d,
                    "session_id": "12345678-1234-4234-8234-123456789012"}
            file.write_text(json.dumps(data))
            self.assertEqual(c.load_session(file, Path(d))["session_id"], data["session_id"])
            with self.assertRaises(ValueError):
                c.load_session(file, Path(d) / "other")
            data["policy"] = "writable"
            file.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                c.load_session(file, Path(d))

    def test_empty_error_wrong_session_and_denied_results_do_not_pass(self):
        final = {"subtype": "success", "result": "A finding", "session_id": "session"}
        observed = ["claude-opus-5-5"]
        self.assertEqual(c.classify_result(final, 0, observed, [], 0, "session"), "completed")
        for change, status in [({"result": "  "}, "empty_result"),
                               ({"is_error": True}, "failed"),
                               ({"session_id": "other"}, "session_mismatch"),
                               ({"permission_denials": [{"tool_name": "Read"}]}, "incomplete_permissions")]:
            with self.subTest(status=status):
                self.assertEqual(c.classify_result({**final, **change}, 0, observed, [], 0, "session"), status)
        self.assertEqual(c.classify_result(final, 1, observed, [], 0, "session"), "failed")
        self.assertEqual(c.classify_result(final, 0, [], [], 0, "session"), "unverified_model_or_output")

    def test_outputs_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / "result.json"
            c.private_write(file, "first")
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                c.private_write(file, "second")
            self.assertEqual(file.read_text(), "first")

    def test_shared_secret_denials_and_review_still_cannot_execute(self):
        for mode in c.POLICIES:
            cmd = c.build_command('claude', 'claude-opus-5-5', 'high', 'session', False,
                                  mode, Path('/tmp/project'), {})
            settings = json.loads(cmd[cmd.index('--settings') + 1])
            for path in ('.env', '.env.*', '.npmrc', '*.pem', '.ssh/**'):
                self.assertIn('Read(//**/' + path + ')', settings['permissions']['deny'])
            if mode == 'review':
                self.assertNotIn('Bash', cmd[cmd.index('--tools') + 1])

    def test_cancelled_session_init_and_conflicting_session_ids(self):
        sid = '12345678-1234-4234-8234-123456789012'
        events = [{'type': 'system', 'subtype': 'init', 'session_id': sid}]
        evidence = c.session_evidence(io.StringIO('\n'.join(map(json.dumps, events))), sid)
        c.validate_resume({'session_id': sid, **evidence})
        self.assertEqual(evidence['session_confirmation'], 'init')
        events.append({'type':'result', 'session_id':'wrong'})
        evidence = c.session_evidence(io.StringIO('\n'.join(map(json.dumps, events))), sid)
        with self.assertRaises(ValueError):
            c.validate_resume({'session_id': sid, **evidence})

    def test_runtime_read_grants_are_specific_and_never_write(self):
        home = Path.home().resolve()
        prefix = home / '.nvm/versions/node/v24.19.0'
        with patch.object(c.shutil, 'which', return_value=str(prefix / 'bin/node')):
            self.assertEqual(c.runtime_read_paths({}), [str(prefix)])
        with patch.object(c.shutil, 'which', return_value=str(home / 'bin/node')):
            self.assertEqual(c.runtime_read_paths({}), [])
        with self.assertRaises(ValueError):
            c.validate_read_paths([home], Path('/tmp/project'))
        with tempfile.TemporaryDirectory() as d:
            settings = c.execution_settings(Path('/tmp/project'), {}, [Path(d)])
            self.assertIn(str(Path(d).resolve()), settings['filesystem']['allowRead'])
            self.assertEqual(settings['filesystem']['allowWrite'], [])

    def test_turn_limit_rejects_unbounded_and_invalid_values(self):
        for turns in (0, -1, 201, True, 2.5):
            with self.assertRaises(ValueError):
                c.build_command('claude', 'claude-opus-5-5', 'high', 'session', False, max_turns=turns)

    @unittest.skipUnless(shutil.which('npm'), 'npm not installed')
    def test_npm_can_load_isolated_user_and_global_configuration(self):
        with patch.dict(os.environ, {'npm_config_globalconfig':'/dev/null', 'npm_config_userconfig':'/dev/null'}):
            env=c.child_environment('high')
        result=subprocess.run(['npm','--version'],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^\d+\.\d+\.\d+')


if __name__ == "__main__":
    unittest.main()
