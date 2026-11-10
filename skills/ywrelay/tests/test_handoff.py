import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))

spec = importlib.util.spec_from_file_location('handoff', Path(__file__).parents[1] / 'scripts/handoff.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class Handoff(unittest.TestCase):
    def test_branch_scope_excludes_upstream_and_local_changes(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            def git(*args):
                return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-C', str(cwd), *args], stderr=subprocess.DEVNULL)
            git('init', '-b', 'main'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
            (cwd/'base.txt').write_text('base\n'); git('add', '.'); git('commit', '-m', 'base')
            git('checkout', '-b', 'feature')
            (cwd/'feature.txt').write_text('FEATURE\n'); git('add', '.'); git('commit', '-m', 'feature')
            git('checkout', 'main')
            (cwd/'upstream.txt').write_text('UPSTREAM\n'); git('add', '.'); git('commit', '-m', 'upstream')
            git('checkout', 'feature'); (cwd/'base.txt').write_text('LOCAL_EDIT\n')
            task, meta = h.prepare('Review branch', cwd, branch_base='main')
            self.assertEqual(meta['changed_paths'], ['feature.txt'])
            self.assertIn('+FEATURE', task)
            self.assertNotIn('UPSTREAM', task); self.assertNotIn('LOCAL_EDIT', task)
            literal, _ = h.prepare('Literal comparison', cwd, 'main')
            self.assertIn('-UPSTREAM', literal)

    def test_staged_commit_root_and_literal_path_filters(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            def git(*args):
                return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', '-C', str(cwd), *args], stderr=subprocess.DEVNULL)
            git('init'); git('config', 'user.email', 'fixture@example.invalid'); git('config', 'user.name', 'Fixture')
            (cwd/'a[1].txt').write_text('INITIAL\n'); git('add', '.')
            task, meta = h.prepare('Staged before first commit', cwd, review_scope='staged')
            self.assertIn('+INITIAL', task)
            git('commit', '-m', 'root')
            git('config', 'log.showRoot', 'false')
            task, meta = h.prepare('Root commit', cwd, commit='HEAD')
            self.assertIn('+INITIAL', task); self.assertIsNone(meta['review_scope']['base'])
            with self.assertRaisesRegex(ValueError, 'no readable changes'):
                h.prepare('Empty range', cwd, review_scope='worktree')
            (cwd/'a[1].txt').write_text('STAGED\n'); git('add', '.')
            (cwd/'a[1].txt').write_text('UNSTAGED\n'); (cwd/'other.txt').write_text('UNTRACKED\n')
            task, meta = h.prepare('Staged', cwd, review_scope='staged', path=['a[1].txt'])
            self.assertIn('+STAGED', task); self.assertNotIn('UNSTAGED', task)
            self.assertEqual(meta['untracked_paths'], [])
            with self.assertRaisesRegex(ValueError, 'no readable changes'):
                h.prepare('Wrong path', cwd, path=['misspelled'])
            task, meta = h.prepare('Selected path', cwd, path=['a[1].txt'])
            self.assertIn('+UNSTAGED', task); self.assertEqual(meta['changed_paths'], ['a[1].txt'])
            self.assertEqual(meta['untracked_paths'], [])
            with self.assertRaises(ValueError): h.prepare('bad', cwd, branch_base='--help')
            with self.assertRaises(ValueError): h.prepare('bad', cwd, path=['../outside'])
            with self.assertRaises(ValueError): h.prepare('bad', cwd, 'HEAD', branch_base='HEAD')

    def test_diff_rules_secret_exclusion_and_no_external_driver(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            def git(*args):
                return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, check=True)
            git('init')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'user.name', 'Fixture')
            for name, text in {'app.py':'value = 1\n', '.env':'CANARY=before\n', 'AGENTS.md':'Keep unrelated changes.\n'}.items():
                (cwd/name).write_text(text)
            git('add', '.')
            git('commit', '-m', 'fixture baseline')
            (cwd/'app.py').write_text('value = 2\n')
            (cwd/'.env').write_text('CANARY=never-include-this\n')
            (cwd/'new.py').write_text('new = 1\n')
            (cwd/'private-link').symlink_to(cwd/'.env')
            git('config', 'diff.external', 'false')
            task, metadata = h.prepare('Review changes.', cwd, 'HEAD')
            self.assertIn('+value = 2', task)
            self.assertIn('Keep unrelated changes.', task)
            self.assertNotIn('never-include-this', task)
            self.assertIn('.env', metadata['excluded_paths'])
            self.assertEqual(metadata['untracked_paths'], ['new.py'])
            self.assertEqual(metadata['root_rules'], ['AGENTS.md'])

    def test_outside_symlink_and_oversize_rules_are_not_attached(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d).resolve(); cwd=base/'project'; cwd.mkdir()
            (base/'outside').write_text('PRIVATE')
            (cwd/'CLAUDE.md').symlink_to(base/'outside')
            task, _ = h.prepare('task', cwd)
            self.assertNotIn('PRIVATE', task)
            (cwd/'AGENTS.md').write_text('x' * (h.MAX_CONTEXT+1))
            with self.assertRaises(ValueError):
                h.prepare('task', cwd)

    def test_cloud_credentials_and_symlink_directory_alias_are_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            cwd=Path(d).resolve()
            private=cwd/'.config/gcloud'; private.mkdir(parents=True)
            (private/'application_default_credentials.json').write_text('synthetic')
            (cwd/'alias').symlink_to(private, target_is_directory=True)
            self.assertFalse(h.safe_path(cwd, '.config/gcloud/application_default_credentials.json'))
            self.assertFalse(h.safe_path(cwd, 'alias/application_default_credentials.json'))

    def test_local_secret_store_and_environment_script_are_excluded(self):
        from consult import validate_read_paths
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); cwd=root/'project'; cwd.mkdir()
            secret=root/'.config/secrets'; secret.mkdir(parents=True)
            (secret/'backup-fixture').write_text('SYNTHETIC_ONLY')
            (cwd/'env.zsh').write_text('SYNTHETIC_ONLY')
            self.assertFalse(h.safe_path(root, '.config/secrets/backup-fixture'))
            self.assertFalse(h.safe_path(cwd, 'env.zsh'))
            with self.assertRaisesRegex(ValueError, 'credential paths'):
                validate_read_paths([secret],cwd)

    def test_parent_rules_apply_to_subdirectory_in_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve(); (root/'.git').mkdir()
            cwd=root/'packages/web'; cwd.mkdir(parents=True)
            (root/'AGENTS.md').write_text('ROOT_RULE')
            (root/'packages/CLAUDE.md').write_text('PACKAGE_RULE')
            (cwd/'AGENTS.md').write_text('LOCAL_RULE')
            task, metadata=h.prepare('task', cwd)
            for marker in ['ROOT_RULE','PACKAGE_RULE','LOCAL_RULE']:
                self.assertIn(marker, task)
            self.assertEqual(metadata['root_rules'], ['AGENTS.md','packages/CLAUDE.md','packages/web/AGENTS.md'])


if __name__ == '__main__':
    unittest.main()
