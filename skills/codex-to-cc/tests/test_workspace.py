import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import workspace as w


class Workspace(unittest.TestCase):
    def test_changes_index_untracked_and_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            def git(*args):
                subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, check=True)
            git('init')
            (cwd/'.gitignore').write_text('cache/\n')
            (cwd/'code.txt').write_text('one\n'); git('add', '.')
            before = w.snapshot(cwd)
            self.assertTrue(before['available'], before)
            (cwd/'code.txt').write_text('two\n')
            self.assertEqual(w.compare(before, w.snapshot(cwd)), 'changed')
            (cwd/'code.txt').write_text('one\n')
            self.assertEqual(w.compare(before, w.snapshot(cwd)), 'unchanged')
            (cwd/'cache').mkdir(); (cwd/'cache/generated').write_text('ignore')
            (cwd/'.env').write_text('SYNTHETIC_SECRET')
            self.assertEqual(w.compare(before, w.snapshot(cwd)), 'unchanged')
            (cwd/'new.txt').write_text('new')
            self.assertEqual(w.compare(before, w.snapshot(cwd)), 'changed')
            updated = w.snapshot(cwd); git('add', 'new.txt')
            self.assertEqual(w.compare(updated, w.snapshot(cwd)), 'changed')
            with patch.object(w, 'MAX_BYTES', 1):
                self.assertFalse(w.snapshot(cwd)['available'])

    def test_non_git_and_special_files_are_unknown_without_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = Path(d).resolve()
            self.assertFalse(w.snapshot(cwd)['available'])
            subprocess.run(['git', '-C', str(cwd), 'init'], capture_output=True, check=True)
            os.mkfifo(cwd/'pipe')
            # Git may omit FIFO entries; either way there must be no attempt to read it.
            result = w.snapshot(cwd)
            self.assertIn('available', result)
            self.assertEqual(w.compare(None, result), 'unknown')


if __name__ == '__main__':
    unittest.main()
