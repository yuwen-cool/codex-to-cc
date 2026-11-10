"""Bounded code-state fingerprints; never persist file contents or execute repo code."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

from handoff import git, safe_path

MAX_FILES = 50000
MAX_BYTES = 256 * 1024 * 1024


def repository_root(cwd):
    try:
        return Path(git(cwd, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError):
        return cwd


def snapshot(cwd):
    started = time.monotonic()
    try:
        root = Path(git(cwd, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        # Include the whole repository: dependencies/rules outside cwd can affect a result.
        index = git(root, 'ls-files', '--stage', '-z')
        names = set(git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''}
        if len(names) > MAX_FILES:
            raise ValueError('file_limit')
        digest = hashlib.sha256(index)
        # HEAD matters even when two commits have identical trees; unborn HEAD is allowed.
        try:
            digest.update(git(root, 'rev-parse', '--verify', 'HEAD'))
        except ValueError:
            digest.update(b'unborn')
        count, excluded, total = 0, 0, 0
        for name in sorted(names):
            if time.monotonic() - started > 10:
                raise ValueError('time_limit')
            if not safe_path(root, name):
                excluded += 1
                continue
            file = root / name
            digest.update(json.dumps(name, ensure_ascii=True).encode() + b'\0')
            try:
                before = file.lstat()
            except FileNotFoundError:
                digest.update(b'missing\0')
                continue
            if not stat.S_ISREG(before.st_mode):
                # Submodules/directories need their own task; do not silently certify them.
                raise ValueError('non_regular_path')
            total += before.st_size
            if total > MAX_BYTES:
                raise ValueError('byte_limit')
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('path_changed')
                content = hashlib.sha256()
                read_bytes = 0
                while chunk := stream.read(1024 * 1024):
                    read_bytes += len(chunk)
                    if read_bytes > MAX_BYTES or time.monotonic() - started > 10:
                        raise ValueError('read_limit')
                    content.update(chunk)
                after = os.fstat(stream.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('changed_during_snapshot')
            digest.update(str(before.st_mode & 0o777).encode() + b':' + content.digest())
            count += 1
        if git(root, 'ls-files', '--stage', '-z') != index:
            raise ValueError('index_changed_during_snapshot')
        return {'available': True, 'sha256': digest.hexdigest(), 'files': count,
                'excluded_paths': excluded, 'root': str(root),
                'coverage': 'Git index, HEAD, tracked and non-ignored untracked regular files; sensitive paths and symlinks excluded. Not an atomic snapshot or OS lock.'}
    except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        return {'available': False, 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                'coverage': 'Automatic freshness unavailable; parent must verify the actual files.'}


def compare(before, after):
    if not before or not after or not before.get('available') or not after.get('available'):
        return 'unknown'
    return 'unchanged' if before['sha256'] == after['sha256'] else 'changed'
