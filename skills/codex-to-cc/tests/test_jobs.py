"""Real subprocess lifecycle tests using an isolated synthetic CLI, never Anthropic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jobs as managed

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/jobs.py'
FAKE = '''import json, sys, time
if '--version' in sys.argv:
    print('2.1.311 (synthetic test CLI)'); raise SystemExit(0)
if 'auth' in sys.argv:
    print(json.dumps({'loggedIn':True, 'authMethod':'claude.ai', 'apiProvider':'firstParty', 'email':'PRIVATE_EMAIL'})); raise SystemExit(0)
args=sys.argv
model=args[args.index('--model')+1]
sid=args[args.index('--resume')+1] if '--resume' in args else args[args.index('--session-id')+1]
task=sys.stdin.read()
print(json.dumps({'type':'system','subtype':'init','session_id':sid}),flush=True)
print(json.dumps({'type':'assistant','message':{'model':model,'content':[{'type':'tool_use','id':'one','name':'Read','input':{'file_path':'DO_NOT_PERSIST_IN_STATUS'}}]}}),flush=True)
if 'SLOW' in task:
    time.sleep(30)
if 'PAUSE' in task:
    time.sleep(1)
if 'BAD_MODEL' in task:
    model='claude-other-1'
print(json.dumps({'type':'result','subtype':'success','session_id':sid,'modelUsage':{model:{}},'result':'Synthetic verified result','permission_denials':[{'tool_name':'Bash'}] if 'DENIED' in task else []}),flush=True)
'''


class ManagedJobs(unittest.TestCase):
    def git_fixture(self):
        for args in [('init',), ('config','user.email','fixture@example.invalid'), ('config','user.name','Fixture')]:
            subprocess.run(['git','-C',str(self.project),*args], capture_output=True, check=True)
        (self.project/'app.py').write_text('value = 1\n')
        subprocess.run(['git','-C',str(self.project),'add','.'], check=True)
        subprocess.run(['git','-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','-C',str(self.project),'commit','-m','baseline'], capture_output=True, check=True)

    def test_freshness_invalidates_passed_and_rejects_stale_acceptance(self):
        self.git_fixture()
        first = self.command('run', '--task', 'simple')
        self.assertEqual(first['freshness']['since_result'], 'unchanged')
        self.command('assess','--verdict','passed','--note','Parent checked code at current revision.')
        self.assertEqual(self.command('list','--attention'), [])
        (self.project/'app.py').write_text('value = 2\n')
        row = self.command('status')
        self.assertEqual(row['acceptance_status'], 'stale')
        self.assertEqual(row['attention'], 'stale_result')
        self.assertEqual(self.command('result','--json',ok=False).returncode,2)
        self.assertEqual(self.command('assess','--verdict','passed','--note','Old result',ok=False).returncode,2)
        self.assertEqual(self.command('list','--attention')[0]['job_id'],first['job_id'])
        fresh = self.command('run','--task','Review current code','--path','app.py')
        self.assertEqual(fresh['freshness']['since_result'],'unchanged')
        self.assertEqual(self.command('assess','--verdict','passed','--note','Verified updated code.')['acceptance_status'],'passed')

    def test_permission_failure_is_actionable_not_waiting_for_input(self):
        p = self.command('run','--task','DENIED',ok=False)
        self.assertEqual(p.returncode,2)
        row = json.loads(p.stdout)
        self.assertEqual(row['status'],'incomplete_permissions')
        self.assertEqual(row['attention'],'blocked')
        self.assertEqual(row['permission_denial_count'],1)
        self.assertTrue(row['needs_attention'])
        self.assertIn('permission',row['next_action'])

    def test_generated_artifacts_need_explicit_current_state_acceptance(self):
        self.git_fixture()
        self.command('run','--task','simple')
        (self.project/'junit.xml').write_text('<testsuite failures="0"/>')
        self.assertEqual(self.command('assess','--verdict','passed','--note','Old result',ok=False).returncode,2)
        accepted = self.command('assess','--verdict','passed','--accept-current-state','--note','Parent checked only junit.xml was generated and reran acceptance tests successfully.')
        self.assertEqual(accepted['acceptance_status'],'passed')
        self.assertEqual(accepted['freshness']['since_result'],'changed')
        self.assertTrue(accepted['assessment']['accepted_current_state'])
        self.assertEqual(self.command('result','--json')['acceptance_status'],'passed')
        (self.project/'app.py').write_text('unexpected = True\n')
        self.assertEqual(self.command('result','--json',ok=False).returncode,2)

    def test_review_changes_during_run_and_sibling_writer_conflicts(self):
        self.git_fixture()
        first=self.command('run','--task','PAUSE','--background')
        (self.project/'app.py').write_text('value = 2\n')
        row=self.command('status','--wait','10')
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['freshness']['during_task'],'changed')
        self.assertEqual(row['acceptance_status'],'stale')
        self.assertEqual(self.command('assess','--verdict','passed','--note','Old review',ok=False).returncode,2)
        root=self.project
        (root/'a').mkdir();(root/'b').mkdir()
        self.project=root/'a'
        self.command('run','--task','SLOW','--mode','execute','--background')
        self.project=root/'b'
        self.assertEqual(self.command('run','--task','simple',ok=False).returncode,2)
        self.project=root/'a';self.command('cancel');self.command('status','--wait','10')
        self.project=root

    def test_view_rechecks_final_state_after_lock_acquisition(self):
        first=self.command('run','--task','simple')
        path=self.state/first['job_id'];file=path/'job.json'
        final=json.loads(file.read_text());running={**final,'status':'running'}
        file.write_text(json.dumps(running))
        @contextmanager
        def finishing_worker(*args,**kwargs):
            file.write_text(json.dumps(final))
            yield
        with patch.object(managed,'lock',finishing_worker):
            self.assertEqual(managed.view(path)['status'],'completed')

    def test_lost_fingerprint_and_failure_attention_priority(self):
        known={'available':True,'sha256':'known'}
        base={'status':'completed','cwd':str(self.project),'mode':'review',
              'acceptance_status':'needs_parent_verification','workspace_before':known,'workspace_after':known}
        with patch.object(managed,'snapshot',return_value={'available':False,'reason':'changed_during_snapshot'}):
            row=managed.freshness(dict(base))
            self.assertEqual(row['acceptance_status'],'unverified')
            self.assertTrue(row['freshness']['verification_lost'])
        with patch.object(managed,'snapshot',return_value={'available':True,'sha256':'changed'}):
            for status,expected in [('timed_out','interrupted'),('incomplete_permissions','blocked'),('failed','failed')]:
                row=managed.freshness({**base,'status':status})
                self.assertEqual(row['attention'],expected)

    def test_transient_end_snapshot_failure_can_be_explicitly_reverified(self):
        self.git_fixture()
        first=self.command('run','--task','simple')
        path=self.state/first['job_id']/'job.json'
        data=json.loads(path.read_text())
        data['workspace_after']={'available':False,'reason':'changed_during_snapshot'}
        path.write_text(json.dumps(data))
        self.assertEqual(self.command('status')['acceptance_status'],'unverified')
        self.assertEqual(self.command('assess','--verdict','passed','--note','Cannot use missing snapshot',ok=False).returncode,2)
        accepted=self.command('assess','--verdict','passed','--accept-current-state','--note','Parent inspected current diff and ran required checks after the snapshot race ended.')
        self.assertEqual(accepted['acceptance_status'],'passed')
        (self.project/'app.py').write_text('later = True\n')
        self.assertEqual(self.command('status')['acceptance_status'],'stale')

    def test_empty_scope_fails_before_a_job_or_model_is_started(self):
        self.git_fixture()
        self.assertEqual(self.command('run','--task','review','--review-scope','worktree',ok=False).returncode,2)
        self.assertEqual(self.command('list'),[])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'project'
        self.project.mkdir()
        self.state = self.base / 'state'
        binary = self.base / 'bin'
        binary.mkdir()
        cli = binary / 'claude'
        cli.write_text('#!' + sys.executable + '\n' + FAKE)
        cli.chmod(0o700)
        self.env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'], 'CODEX_THREAD_ID':'test-chat-A'}
        for key in ['CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                    'CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY']:
            self.env.pop(key, None)

    def command(self, action, *args, ok=True):
        cmd = [sys.executable, str(SCRIPT), '--state-dir', str(self.state), action]
        if action != 'doctor':
            cmd += ['--cwd', str(self.project)]
        p = subprocess.run(cmd + list(args), env=self.env, capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            return json.loads(p.stdout)
        return p

    def tearDown(self):
        # Always ask still-live test workers to stop before TemporaryDirectory cleanup.
        if self.state.exists():
            for path in self.state.glob('*/job.json'):
                (path.parent / 'cancel.request').touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                rows = self.command('list', '--all')
                if not any(r['status'] in ['queued','running'] for r in rows):
                    break
                time.sleep(.1)
        self.temp.cleanup()

    def test_foreground_continue_result_and_project_isolation(self):
        first = self.command('run', '--task', 'simple', '--title', 'first')
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['model'], 'claude-opus-5-5')
        self.assertEqual(first['effort'], 'high')
        second = self.command('continue', '--task', 'followup')
        self.assertEqual(second['parent_job'], first['job_id'])
        self.assertEqual(second['session_id'], first['session_id'])
        result = self.command('result', '--json')
        self.assertEqual(result['result'], 'Synthetic verified result')
        refused = self.command('continue', '--mode', 'execute', '--task', 'no upgrade', ok=False)
        self.assertEqual(refused.returncode, 2)
        other = self.base / 'other'; other.mkdir()
        original = self.project; self.project = other
        self.assertEqual(self.command('list'), [])
        self.assertEqual(self.command('status', '--job', first['job_id'], ok=False).returncode, 2)
        self.project = original
        self.assertEqual((self.state / first['job_id'] / 'task.txt').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / first['job_id'] / 'worker.log').read_text(), '')

    def test_background_progress_cancel_and_writer_conflicts(self):
        first = self.command('run', '--mode', 'execute', '--task', 'SLOW', '--background')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.command('status', '--job', first['job_id'])
            if row['tool_counts'].get('Read'):
                break
            time.sleep(.1)
        self.assertEqual(row['tool_counts']['Read'], 1)
        self.assertNotIn('DO_NOT_PERSIST_IN_STATUS', json.dumps(row))
        self.assertEqual(self.command('run', '--task', 'other', ok=False).returncode, 2)
        nested = self.project / 'nested'; nested.mkdir()
        original = self.project; self.project = nested
        self.assertEqual(self.command('run', '--mode', 'execute', '--task', 'other', ok=False).returncode, 2)
        self.project = original
        self.assertTrue(self.command('cancel')['cancel_requested'])
        row = self.command('status', '--wait', '10')
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(json.loads(Path(row['result_path']).read_text())['status'], 'cancelled')
        self.assertEqual(self.command('cancel')['status'], 'cancelled')
        self.assertEqual(self.command('run', '--task', 'new')['status'], 'completed')

    def test_timeout_and_model_mismatch_are_not_success(self):
        p = self.command('run', '--task', 'SLOW', '--timeout', '1', ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)['status'], 'timed_out')
        self.assertEqual(self.command('result', '--json', ok=False).returncode, 2)
        p = self.command('run', '--task', 'BAD_MODEL', ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)['status'], 'unverified_model_or_output')
        self.assertEqual(self.command('continue', '--task', 'followup', ok=False).returncode, 2)

    def test_missing_worker_stays_interrupted_until_explicit_resolution(self):
        first = self.command('run', '--task', 'simple')
        path = self.state / first['job_id'] / 'job.json'
        data = json.loads(path.read_text()); data['status'] = 'running'; data.pop('finished_at')
        path.write_text(json.dumps(data))
        self.assertEqual(self.command('status')['status'], 'interrupted')
        self.assertEqual(self.command('cancel', ok=False).returncode, 2)
        self.assertEqual(self.command('run', '--mode', 'execute', '--task', 'new', ok=False).returncode, 2)
        self.assertEqual(self.command('resolve', '--note', 'Synthetic worker already finished; no orphan processes.')['status'], 'closed_interrupted')
        self.assertEqual(self.command('run', '--mode', 'execute', '--task', 'new')['status'], 'completed')

    def test_doctor_reuses_auth_without_exposing_identity(self):
        info = self.command('doctor')
        self.assertEqual(info['auth'], 'logged_in')
        self.assertNotIn('PRIVATE_EMAIL', json.dumps(info))
        self.assertFalse(self.state.exists())

    def test_paths_and_waits_fail_closed(self):
        self.assertEqual(self.command('status', '--job', '../bad', ok=False).returncode, 2)
        first = self.command('run', '--task', 'simple')
        self.assertEqual(self.command('status', '--wait', 'nan', ok=False).returncode, 2)
        self.assertEqual(self.command('status', '--wait', '61', ok=False).returncode, 2)
        self.project = self.base
        self.assertEqual(self.command('run', '--task', 'bad store', ok=False).returncode, 2)

    def test_chats_cannot_select_or_cancel_each_others_jobs(self):
        first = self.command('run', '--task', 'SLOW A', '--background')
        self.env['CODEX_THREAD_ID'] = 'test-chat-B'
        second = self.command('run', '--task', 'SLOW B', '--background')
        self.assertEqual(self.command('status')['job_id'], second['job_id'])
        self.env['CODEX_THREAD_ID'] = 'test-chat-A'
        self.assertEqual(self.command('status')['job_id'], first['job_id'])
        self.assertEqual([r['job_id'] for r in self.command('list')], [first['job_id']])
        self.assertEqual(self.command('cancel', '--job', second['job_id'], ok=False).returncode, 2)
        self.assertEqual(self.command('cancel')['job_id'], first['job_id'])
        self.assertFalse((self.state / second['job_id'] / 'cancel.request').exists())
        self.assertEqual(self.command('cancel', '--job', second['job_id'], '--owner', 'test-chat-B')['job_id'], second['job_id'])

    def test_cancelled_session_can_resume_after_native_init(self):
        first = self.command('run', '--task', 'SLOW', '--background')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.command('status')['tool_counts'].get('Read'):
                break
            time.sleep(.1)
        self.command('cancel')
        row = self.command('status', '--wait', '10')
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['session_confirmation'], 'init')
        resumed = self.command('continue', '--task', 'finish')
        self.assertEqual(resumed['session_id'], row['session_id'])
        self.assertEqual(resumed['parent_job'], first['job_id'])
        self.assertEqual(resumed['status'], 'completed')
        self.assertEqual(resumed['acceptance_status'], 'needs_parent_verification')

    def test_no_owner_never_guesses_latest(self):
        self.env.pop('CODEX_THREAD_ID', None)
        first = self.command('run', '--task', 'simple')
        self.assertEqual(self.command('status', ok=False).returncode, 2)
        self.assertEqual(self.command('status', '--job', first['job_id'])['job_id'], first['job_id'])

    def test_result_path_cannot_bypass_chat_ownership(self):
        first = self.command('run', '--task', 'simple')
        self.env['CODEX_THREAD_ID'] = 'test-chat-B'
        self.assertEqual(self.command('continue', '--resume-from', first['result_path'], '--task', 'followup', ok=False).returncode, 2)
        self.assertEqual(self.command('continue', '--resume-from', first['result_path'], '--owner', 'test-chat-A', '--task', 'followup')['session_id'], first['session_id'])

    def test_assessment_is_explicit_and_cannot_mark_failed_invocation_passed(self):
        first = self.command('run', '--task', 'simple')
        self.assertEqual(first['acceptance_status'], 'needs_parent_verification')
        checked = self.command('assess', '--verdict', 'passed', '--note', 'Synthetic parent verification evidence.')
        self.assertEqual(checked['acceptance_status'], 'passed')
        result = self.command('result', '--json')
        self.assertEqual(result['assessment']['source'], 'parent_assessment')
        self.assertEqual(json.loads(Path(first['result_path']).read_text())['acceptance_status'], 'needs_parent_verification')
        self.command('assess', '--verdict', 'failed', '--note', 'Parent detected failing acceptance test.')
        self.assertEqual(self.command('result', '--json', ok=False).returncode, 2)
        self.command('run', '--task', 'BAD_MODEL', ok=False)
        self.assertEqual(self.command('assess', '--verdict', 'passed', '--note', 'cannot', ok=False).returncode, 2)

    def test_task_context_is_frozen_at_submission(self):
        (self.project/'CLAUDE.md').write_text('ORIGINAL_RULE')
        first = self.command('run', '--task', 'simple')
        (self.project/'CLAUDE.md').write_text('CHANGED_RULE')
        saved = (self.state/first['job_id']/'task.txt').read_text()
        self.assertEqual(saved.count('ORIGINAL_RULE'), 1)
        self.assertNotIn('CHANGED_RULE', saved)
        result = json.loads(Path(first['result_path']).read_text())
        self.assertEqual(result['context']['root_rules'], ['CLAUDE.md'])


if __name__ == '__main__':
    unittest.main()
