"""Shared path policy for file tools, command sandbox and prompt attachments."""
import fnmatch
from pathlib import Path

SECRET_NAMES = ('.env', '.env.*', '*.pem', '*.key', 'id_rsa', 'id_ed25519',
                '.npmrc', '.pypirc', '.netrc', '.git-credentials', '.mcp.json',
                '.claude.json', 'application_default_credentials.json', 'env.zsh')
SECRET_DIRECTORIES = (('.aws',), ('.ssh',), ('.config', 'gcloud'), ('.config', 'secrets'))
SECRET_PATTERNS = SECRET_NAMES + tuple('/'.join(p) + '/**' for p in SECRET_DIRECTORIES)


def sensitive(path):
    parts = Path(path).parts
    return (any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in SECRET_NAMES)
            or any(tuple(parts[i:i+len(prefix)]) == prefix
                   for prefix in SECRET_DIRECTORIES for i in range(len(parts))))
