#!/usr/bin/env python3
"""Local, task-scoped Claude jobs. No daemon, credential store, or external service."""
import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

import consult
from handoff import add_scope_arguments, scope_options
from workspace import snapshot, compare, repository_root

DEFAULT_STATE = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'claude-jobs'
ACTIVE = {'queued', 'running', 'interrupted'}


def save(path, data):
    """Only the worker writes job.json; readers always see a complete snapshot."""
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(temp, path)


@contextmanager
def lock(path, blocking=True):
    with path.open('a+') as f:
        os.chmod(path, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def root_path(value):
    root = value.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise ValueError('Job storage must belong to the current user.')
    os.chmod(root, 0o700)
    return root


def job_path(root, job_id):
    if not re.fullmatch(r'[0-9a-f]{12}', job_id):
        raise ValueError('Use the exact 12-character job ID returned by run/list.')
    path = root / job_id
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Job not found in this store.')
    return path


def view(path):
    data = json.loads((path / 'job.json').read_text())
    now = time.time()
    if data['status'] in {'running', 'queued'}:
        try:
            with lock(path / 'worker.lock', blocking=False):
                # The worker may have finished between our first read and lock acquisition.
                data = json.loads((path / 'job.json').read_text())
                if data['status'] == 'running' or (data['status'] == 'queued' and now - data['created_at'] > 15):
                    data['status'] = 'interrupted'
                    data['notice'] = 'Worker is absent. Inspect partial changes and processes before further execution; no automatic retry.'
        except BlockingIOError:
            pass
        if (path / 'cancel.request').exists() and data['status'] != 'interrupted':
            data['cancel_requested'] = True
    data['elapsed_seconds'] = round(max(0, data.get('finished_at', now) - data['created_at']), 1)
    data['result_path'] = str(path / 'output/result.json') if (path / 'output/result.json').exists() else None
    data['report_path'] = str(path / 'output/report.md') if (path / 'output/report.md').exists() else None
    if (path / 'assessment.json').exists():
        assessment = json.loads((path / 'assessment.json').read_text())
        data['acceptance_status'] = assessment['verdict']
        data['assessment'] = assessment
    return attention(data)


def attention(data):
    status = data['status']
    acceptance = data.get('acceptance_status')
    if status in {'queued', 'running'}:
        kind, action = 'working', 'Use status --wait 30; background submission is not completion.'
    elif status == 'incomplete_permissions':
        kind, action = 'blocked', 'Inspect result permission_denials. Handle the missing dependency/permission within existing authorization; never widen the sandbox automatically.'
    elif status in {'timed_out', 'cancelled', 'interrupted', 'closed_interrupted'}:
        kind, action = 'interrupted', 'Inspect partial changes and confirm processes stopped; resume only a verified native session.'
    elif status != 'completed':
        kind, action = 'failed', 'Inspect result errors/stop_reason or worker.log; do not retry blindly or silently change model.'
    elif acceptance in {'stale', 'unverified'}:
        kind, action = 'stale_result', 'Inspect changes and verify current code. Use a new scoped review, or explicitly assess --accept-current-state with parent verification evidence; never reuse the old verdict silently.'
    elif acceptance == 'blocked':
        kind, action = 'blocked', 'Read the parent assessment for the specific obstacle and required next step.'
    elif acceptance == 'failed':
        kind, action = 'rejected', 'Read the parent assessment, fix the demonstrated issue, then verify again.'
    elif acceptance == 'passed':
        kind, action = 'accepted', 'Parent verification is recorded. status/result recheck Git freshness; list alone does not.'
    else:
        kind, action = 'verify_result', 'Read the report, inspect actual changes and run relevant checks, then record assess with evidence.'
        if data.get('tool_error_count'):
            action += ' Tool errors occurred; check whether they were recovered or blocked a required step.'
    data.update(attention=kind, needs_attention=kind not in {'working', 'accepted'}, next_action=action)
    return data


def freshness(data):
    if data['status'] in ACTIVE:
        return data
    current = snapshot(Path(data['cwd']))
    after = data.get('workspace_after')
    since_result = compare(after, current)
    during = compare(data.get('workspace_before'), after)
    stale = since_result == 'changed' or (data['mode'] == 'review' and during == 'changed')
    assessment = data.get('assessment', {})
    since_assessment = compare(assessment.get('workspace'), current)
    lost = (bool(after and after.get('available')) and since_result == 'unknown') or (
        data['mode'] == 'review' and bool((data.get('workspace_before') or {}).get('available')) and during == 'unknown')
    lost = lost or (bool((assessment.get('workspace') or {}).get('available')) and since_assessment == 'unknown')
    data['freshness'] = {'since_result': since_result, 'during_task': during, 'since_assessment': since_assessment,
                         'verification_lost': lost, 'current': current, 'checked_at': time.time()}
    # An explicit parent check can accept a newer state without rewriting Claude's report.
    if assessment.get('verdict') == 'passed' and assessment.get('accepted_current_state') and since_assessment == 'unchanged':
        stale, lost = False, False
    if assessment.get('verdict') == 'passed' and since_assessment == 'changed':
        stale = True
    if stale and data.get('acceptance_status') not in {'failed', 'blocked'}:
        data['acceptance_status'] = 'stale'
    elif lost and data.get('acceptance_status') not in {'failed', 'blocked'}:
        data['acceptance_status'] = 'unverified'
    return attention(data)


def all_jobs(root, cwd=None):
    rows = [view(p) for p in root.iterdir()
            if p.is_dir() and re.fullmatch(r'[0-9a-f]{12}', p.name) and (p / 'job.json').exists()]
    if cwd is not None:
        rows = [r for r in rows if r['cwd'] == str(cwd)]
    return sorted(rows, key=lambda r: r['created_at'], reverse=True)


def owner_id(value=None):
    owner = value or os.environ.get('CODEX_THREAD_ID')
    if owner and (len(owner) > 200 or not re.fullmatch(r'[a-zA-Z0-9_.:-]+', owner)):
        raise ValueError('Owner must be a plain chat identifier (maximum 200 characters).')
    return owner


def select(root, cwd, job_id, owner=None):
    if job_id == 'latest':
        if not owner:
            raise ValueError('Current chat identity unavailable. Specify --job or a stable --owner; never infer another chat’s latest task.')
        rows = [r for r in all_jobs(root, cwd) if r.get('owner_chat_id') == owner]
        if not rows:
            raise ValueError('No managed Claude jobs for this project. Start a new job or use continue --resume-from for a legacy result.')
        job_id = rows[0]['job_id']
    path = job_path(root, job_id)
    if view(path)['cwd'] != str(cwd):
        raise ValueError('Job belongs to another project; specify its --cwd explicitly.')
    recorded_owner = view(path).get('owner_chat_id')
    if recorded_owner and recorded_owner != owner:
        raise ValueError('Job belongs to another chat; use list --all and its explicit --owner to manage it intentionally.')
    return path


def overlapping(a, b):
    a, b = Path(a), Path(b)
    return a.is_relative_to(b) or b.is_relative_to(a)


def start(args, root, cwd):
    if not cwd.is_dir():
        raise ValueError('Project directory does not exist.')
    if root.is_relative_to(cwd):
        raise ValueError('Job storage must be outside the project directory.')
    task = args.task_file.expanduser().read_text() if args.task_file else args.task
    if not task or not task.strip():
        raise ValueError('Task must not be empty.')
    config = consult.read_config(consult.ROOT / 'config.json')
    owner = owner_id(getattr(args, 'owner', None))
    with lock(root / 'registry.lock'):
        previous, parent_id, resume_path = {}, None, None
        if args.action == 'continue':
            if args.resume_from:
                resume_path = args.resume_from.expanduser().resolve(strict=True)
            else:
                prior = select(root, cwd, args.job, owner)
                row = view(prior)
                if row['status'] in ACTIVE:
                    raise ValueError('Previous job is active or interrupted; inspect/cancel it before continuing.')
                parent_id, resume_path = row['job_id'], prior / 'output/result.json'
            previous = consult.load_session(resume_path, cwd, args.mode)
            consult.validate_resume(previous, owner)
        mode = args.mode or previous.get('mode') or 'review'
        model = args.model or previous.get('requested_model') or config['model']
        effort = args.effort or previous.get('requested_effort') or config['effort']
        timeout = args.timeout if args.timeout is not None else config.get('timeout_seconds', 600)
        max_turns = args.max_turns if args.max_turns is not None else config.get('max_turns', 40)
        read_paths = args.read_path if args.read_path is not None else previous.get('runtime_read_grants', [])
        read_paths = consult.validate_read_paths(read_paths, cwd)
        consult.validate_options(model, effort, timeout)
        env = consult.child_environment(effort)
        binary = shutil.which('claude')
        if not binary:
            raise ValueError('Official Claude Code not found. Run doctor for setup diagnostics.')
        consult.build_command(binary, model, effort, str(uuid.uuid4()), bool(previous), mode, cwd, env, read_paths, max_turns)
        # Resolve/bound context before scheduling so an invalid ref never leaves a queued job.
        from handoff import prepare
        before = snapshot(cwd)
        task, context = prepare(task, cwd, args.diff_base, **scope_options(args))
        context['workspace_at_submission'] = before
        # A writer conflicts with all overlapping work, not just another writer.
        conflict_root = repository_root(cwd)
        for row in all_jobs(root):
            row_root = row.get('conflict_root') or repository_root(Path(row['cwd']))
            if row['status'] in ACTIVE and overlapping(row_root, conflict_root) and ('execute' in (mode, row['mode'])):
                raise ValueError(f"Overlapping job {row['job_id']} is {row['status']}; finish/cancel and inspect it first.")
            if previous and row['status'] in ACTIVE and row.get('session_id') == previous['session_id']:
                raise ValueError(f"Native session is already used by job {row['job_id']}.")
        job_id = uuid.uuid4().hex[:12]
        path = root / job_id
        path.mkdir(mode=0o700)
        consult.private_write(path / 'task.txt', task)
        save(path / 'context.json', context)
        if previous:
            consult.private_write(path / 'resume.json', json.dumps(previous, ensure_ascii=False))
        request = {'cwd': str(cwd), 'mode': mode, 'model': model, 'effort': effort,
                   'timeout': timeout, 'resume': bool(previous), 'max_turns': max_turns,
                   'read_path': read_paths, 'diff_base': args.diff_base, **scope_options(args),
                   'conflict_root': str(conflict_root), 'owner_chat_id': owner}
        save(path / 'request.json', request)
        data = {'schema': 'claude-jobs/v1', 'job_id': job_id, 'title': args.title or f'Claude {mode}',
                **request, 'parent_job': parent_id, 'status': 'queued', 'phase': 'queued',
                'acceptance_status': 'needs_parent_verification', 'workspace_before': before,
                'created_at': time.time(), 'tool_counts': {}, 'session_id': previous.get('session_id')}
        save(path / 'job.json', data)
        try:
            with (path / 'worker.log').open('x') as log:
                os.chmod(path / 'worker.log', 0o600)
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--state-dir', str(root),
                                  '_worker', job_id], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True, close_fds=True)
        except OSError as exc:
            data.update(status='failed', phase='launch_failed', error=str(exc), finished_at=time.time())
            save(path / 'job.json', data)
            raise
    if args.background:
        return view(path)
    try:
        while True:
            data = view(path)
            if data['status'] not in {'queued', 'running'}:
                return freshness(data)
            time.sleep(.25)
    except KeyboardInterrupt:
        (path / 'cancel.request').touch(mode=0o600, exist_ok=True)
        return {**view(path), 'cancel_requested': True, 'notice': 'Cancellation requested; check status for confirmation.'}


def worker(root, job_id):
    path = job_path(root, job_id)
    # Cancellation uses a control file: never signal a potentially recycled PID.
    def interrupt(signum, frame):
        (path / 'cancel.request').touch(mode=0o600, exist_ok=True)
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    with lock(path / 'worker.lock', blocking=False):
        data = json.loads((path / 'job.json').read_text())
        if data['status'] != 'queued':
            return 2
        request = json.loads((path / 'request.json').read_text())
        data.update(status='running', phase='starting', started_at=time.time(), worker_pid=os.getpid())
        save(path / 'job.json', data)
        last_save = 0
        def update(progress):
            nonlocal last_save
            changed = progress.get('phase') != data['phase'] or progress.get('tool_counts') != data['tool_counts']
            data.update(progress)
            if changed or time.monotonic() - last_save >= 2:
                data['updated_at'] = time.time()
                save(path / 'job.json', data)
                last_save = time.monotonic()
        args = argparse.Namespace(cwd=Path(request['cwd']), task_file=path / 'task.txt',
                                  output_dir=path / 'output', config=consult.ROOT / 'config.json',
                                  mode=request['mode'], model=request['model'], effort=request['effort'],
                                  timeout=request['timeout'], dry_run=False,
                                  max_turns=request.get('max_turns', 40), read_path=request.get('read_path', []),
                                  diff_base=request.get('diff_base'),
                                  owner=request.get('owner_chat_id'),
                                  context_metadata=json.loads((path / 'context.json').read_text()) if (path / 'context.json').exists() else None,
                                  resume_from=path / 'resume.json' if request['resume'] else None)
        try:
            # consult's final report is stored once, not duplicated into the worker log.
            with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                code = consult.run(args, on_progress=update, cancelled=lambda: (path / 'cancel.request').exists())
            result = json.loads((path / 'output/result.json').read_text())
            data.update(status=result['status'], phase='finished', session_id=result['session_id'],
                        observed_models=result['observed_models'], exit_code=code,
                        acceptance_status=result['acceptance_status'], tool_error_count=result['tool_error_count'],
                        workspace_before=result.get('workspace_before'), workspace_after=result.get('workspace_after'),
                        stop_reason=result.get('stop_reason'),
                        permission_denial_count=len(result.get('permission_denials', [])),
                        session_confirmation=result['session_confirmation'])
        except Exception as exc:
            data.update(status='failed', phase='runner_failed', error=f'{type(exc).__name__}: {exc}')
            code = 2
        finally:
            data['finished_at'] = time.time()
            save(path / 'job.json', data)
        return code


def doctor(cwd=None):
    binary = shutil.which('claude')
    result = {'cli_found': bool(binary), 'defaults': consult.read_config(consult.ROOT / 'config.json'),
              'auth': 'unknown', 'login_action': 'None unless official CLI reports missing/expired authentication.'}
    if not binary:
        return result
    env = consult.child_environment(result['defaults']['effort'])
    version = subprocess.run([binary, '--version'], env=env, text=True, capture_output=True, timeout=10, check=True)
    result['cli_version'] = version.stdout.strip()
    auth = subprocess.run([binary, 'auth', 'status', '--json'], env=env, text=True, capture_output=True, timeout=15)
    try:
        info = json.loads(auth.stdout)
        result.update(auth='logged_in' if info.get('loggedIn') else 'not_logged_in',
                      auth_method=info.get('authMethod'), provider=info.get('apiProvider'))
    except (ValueError, AttributeError):
        result['auth'] = 'status_unavailable'
    result['note'] = 'Reuses local official CLI authentication; no login performed or credentials read. Model availability and sandbox behavior require a real invocation.'
    if cwd:
        cwd = cwd.expanduser().resolve(strict=True)
        result['project'] = str(cwd)
        result['runtime_paths'] = {name: shutil.which(name) for name in ('node', 'npm', 'python3', 'uv')}
        result['sandbox_auto_reads'] = consult.runtime_read_paths(env)
        result['execution_policy'] = consult.execution_settings(cwd, env)
        result['probe_status'] = 'Configuration inspected only; runtime paths do not prove sandbox compatibility.'
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ['run', 'continue']:
        q = sub.add_parser(action)
        q.add_argument('--cwd', type=Path, default=Path.cwd())
        task = q.add_mutually_exclusive_group(required=True)
        task.add_argument('--task')
        task.add_argument('--task-file', type=Path)
        q.add_argument('--mode', choices=consult.POLICIES)
        q.add_argument('--title')
        q.add_argument('--model')
        q.add_argument('--effort', choices=consult.EFFORTS)
        q.add_argument('--timeout', type=float)
        q.add_argument('--max-turns', type=int, help='1–200; default 40')
        q.add_argument('--read-path', type=Path, action='append', help='Specific runtime/library read grant for this execute task')
        add_scope_arguments(q)
        q.add_argument('--owner', help='Chat identity; defaults to CODEX_THREAD_ID')
        q.add_argument('--background', action='store_true')
        if action == 'continue':
            choice = q.add_mutually_exclusive_group()
            choice.add_argument('--job', default='latest')
            choice.add_argument('--resume-from', type=Path)
    for action in ['list', 'status', 'result', 'cancel', 'resolve', 'assess']:
        q = sub.add_parser(action)
        q.add_argument('--cwd', type=Path, default=Path.cwd())
        q.add_argument('--owner', help='Chat identity; defaults to CODEX_THREAD_ID')
        if action == 'list':
            q.add_argument('--all', action='store_true')
            q.add_argument('--limit', type=int, default=20)
            q.add_argument('--attention', action='store_true', help='Check freshness of the latest --limit tasks and show those needing attention')
        else:
            q.add_argument('--job', default='latest')
        if action == 'status':
            q.add_argument('--wait', type=float, default=0, help='Bounded wait, 0–60 seconds')
        if action == 'result':
            q.add_argument('--json', action='store_true')
        if action == 'resolve':
            q.add_argument('--note', required=True, help='Evidence that orphan processes and partial changes were checked')
        if action == 'assess':
            q.add_argument('--verdict', choices=['passed', 'failed', 'blocked'], required=True)
            q.add_argument('--note', required=True, help='Parent verification evidence: commands, results and diff checks; does not run tests')
            q.add_argument('--accept-current-state', action='store_true', help='Explicitly accept the current fingerprint after inspecting changes and rerunning checks; original model report remains tied to its earlier state')
    q = sub.add_parser('doctor')
    q.add_argument('--cwd', type=Path, help='Inspect project toolchain configuration without running project code')
    q = sub.add_parser('_worker', help=argparse.SUPPRESS)
    q.add_argument('job_id')
    args = p.parse_args()
    try:
        if args.action == 'doctor':
            data = doctor(args.cwd)
        else:
            root = root_path(args.state_dir)
            if args.action == '_worker':
                return worker(root, args.job_id)
            cwd = args.cwd.expanduser().resolve(strict=True)
            if args.action in {'run', 'continue'}:
                data = start(args, root, cwd)
            elif args.action == 'list':
                if not 1 <= args.limit <= 200:
                    raise ValueError('--limit must be 1–200.')
                rows = all_jobs(root, None if args.all else cwd)
                data = (rows if args.all else [r for r in rows if r.get('owner_chat_id') == owner_id(args.owner)])[:args.limit]
                if args.attention:
                    data = [r for r in map(freshness, data) if r['needs_attention']]
            else:
                path = select(root, cwd, args.job, owner_id(args.owner))
                data = view(path)
                if args.action == 'status':
                    if not 0 <= args.wait <= 60:
                        raise ValueError('--wait must be 0–60 seconds.')
                    deadline = time.monotonic() + args.wait
                    while data['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                        time.sleep(min(.25, max(0, deadline - time.monotonic())))
                        data = view(path)
                    data = freshness(data)
                elif args.action == 'cancel':
                    if data['status'] == 'interrupted':
                        raise ValueError('Worker is absent; cannot confirm cancellation. Inspect partial changes and processes manually.')
                    if data['status'] in {'queued', 'running'}:
                        (path / 'cancel.request').touch(mode=0o600, exist_ok=True)
                        data['cancel_requested'] = True
                        data['notice'] = 'Request recorded, not yet confirmed. Use status --wait 10.'
                elif args.action == 'resolve':
                    with lock(root / 'registry.lock'):
                        data = view(path)
                        if data['status'] != 'interrupted' or not args.note.strip():
                            raise ValueError('resolve only closes an interrupted job after manual process/change inspection; supply its evidence in --note.')
                        with lock(path / 'worker.lock', blocking=False):
                            data.update(status='closed_interrupted', phase='manually_resolved',
                                        resolution_note=args.note, finished_at=time.time())
                            save(path / 'job.json', data)
                elif args.action == 'assess':
                    with lock(root / 'registry.lock'):
                        data = freshness(view(path))
                        if data['status'] in ACTIVE or not args.note.strip():
                            raise ValueError('Assess only a finished task; provide concrete parent verification evidence.')
                        if args.verdict == 'passed' and data['status'] != 'completed':
                            raise ValueError('An incomplete invocation cannot be marked passed.')
                        if args.accept_current_state and (args.verdict != 'passed' or not data.get('freshness', {}).get('current', {}).get('available')):
                            raise ValueError('--accept-current-state requires passed and a measurable current Git state; resolve fingerprint errors first.')
                        if args.verdict == 'passed' and not args.accept_current_state and (
                                data.get('freshness', {}).get('verification_lost') or
                                data.get('freshness', {}).get('since_result') == 'changed' or
                                (data['mode'] == 'review' and data.get('freshness', {}).get('during_task') == 'changed')):
                            raise ValueError('Code changed or freshness verification was lost. Inspect changes and verify current code; use a new task or explicitly --accept-current-state with evidence.')
                        save(path / 'assessment.json', {'verdict': args.verdict, 'note': args.note,
                             'workspace': data.get('freshness', {}).get('current'),
                             'accepted_current_state': args.accept_current_state,
                             'source': 'parent_assessment', 'recorded_at': time.time()})
                        data = freshness(view(path))
                elif args.action == 'result':
                    data = freshness(data)
                    if data['status'] in ACTIVE or not data['result_path']:
                        raise ValueError(f"Job is {data['status']}; no final result available. Use status for details.")
                    result = json.loads(Path(data['result_path']).read_text())
                    result.update({key: data[key] for key in ('acceptance_status', 'attention', 'needs_attention', 'next_action', 'freshness', 'assessment') if key in data})
                    if not args.json:
                        print(f"Job {data['job_id']} | invocation: {data['status']} | acceptance: {data.get('acceptance_status', 'needs_parent_verification')} | {data['mode']} | {data['model']} / {data['effort']}\n")
                        if 'assessment' in data:
                            print('Parent verification: ' + data['assessment']['note'] + '\n')
                        print('Next action: ' + data['next_action'] + '\n')
                        print(result['result'])
                        return 0 if data['status'] == 'completed' and data.get('acceptance_status') not in {'failed', 'blocked', 'stale', 'unverified'} else 2
                    data = result
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if args.action == 'result':
            return 0 if data['status'] == 'completed' and data.get('acceptance_status') not in {'failed', 'blocked', 'stale', 'unverified'} else 2
        if args.action in {'run', 'continue'} and data['status'] not in {'queued', 'running', 'completed'}:
            return 2
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
