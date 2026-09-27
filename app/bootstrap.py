"""Native first-run configuration and credentials, before worker creation."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import string

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from app.config_editor import get_config_path
from app.config_persistence import ConfigPersistenceError, ConfigPersistenceTransaction
from app.config_validation import is_placeholder, validate_config

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 7767


def valid_password_hash(value):
    if is_placeholder(value):
        return False
    try:
        method, salt, digest = value.split('$', 2)
        if not (method.startswith(('pbkdf2:', 'scrypt:')) or method == 'scrypt' or method in hashlib.algorithms_available):
            return False
        if not salt or not re.fullmatch(r'[0-9a-f]+', digest):
            return False
        # Exercise the installed Werkzeug implementation, including method parameters.
        check_password_hash(value, secrets.token_urlsafe(16))
        sample = generate_password_hash('validation', method=method, salt_length=len(salt))
        return len(digest) == len(sample.rsplit('$', 1)[1])
    except (ValueError, TypeError, OverflowError):
        return False


def _replace_admin_values(content, config, updates):
    """Replace only bootstrap fields, retaining unrelated YAML and comments."""
    root = yaml.compose(content)
    admin_node = next((v for k, v in root.value if k.value == 'admin'), None) if root else None
    if admin_node is None:
        return content.rstrip() + '\n\n' + yaml.safe_dump({'admin': updates}, sort_keys=False)
    if not isinstance(admin_node, yaml.MappingNode):
        raise ValueError('admin must be a YAML mapping')
    replacements = []
    remaining = dict(updates)
    for key, value in admin_node.value:
        if key.value in remaining:
            replacement = '"' + remaining.pop(key.value) + '"'
            replacements.append((value.start_mark.index, value.end_mark.index, replacement))
    if remaining:
        # Re-render just admin when keys are absent (also supports flow mappings).
        admin = dict(config.get('admin') or {})
        admin.update(updates)
        rendered = yaml.safe_dump(admin, sort_keys=False, default_flow_style=True).strip()
        return content[:admin_node.start_mark.index] + rendered + '\n' + content[admin_node.end_mark.index:]
    for start, end, replacement in sorted(replacements, reverse=True):
        content = content[:start] + replacement + content[end:]
    return content


def bootstrap_config():
    path = Path(get_config_path())
    if not path.exists() and 'WHOSATMYFEEDER_CONFIG' in os.environ:
        raise ValueError(f'WHOSATMYFEEDER_CONFIG file does not exist: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    with ConfigPersistenceTransaction(path) as transaction:
        return _bootstrap_locked(path, transaction)


def _bootstrap_locked(path, transaction):
    if not path.exists():
        example = REPO_ROOT / 'config/config.yml.example'
        example_content = example.read_text(encoding='utf-8')
        example_config = yaml.safe_load(example_content) or {}
        transaction.write(
            example_config,
            create_backup=False,
            rendered_content=example_content,
        )
        print(f'Created {path} from the example. Review external settings before starting WAMF.', flush=True)

    # Never print credentials before persistence.
    content = transaction.read_text()
    config = yaml.safe_load(content) or {}
    if not isinstance(config, dict):
        raise ValueError('Configuration must be a YAML mapping')
    admin = config.get('admin') or {}
    if not isinstance(admin, dict):
        raise ValueError('admin must be a YAML mapping')
    updates = {}
    password = None
    if admin.get('auth_enabled', False):
        env_secret = os.environ.get('WAMF_SECRET_KEY')
        if env_secret and is_placeholder(env_secret):
            raise ValueError('WAMF_SECRET_KEY must not be blank or a placeholder')
        if not env_secret and is_placeholder(admin.get('session_secret')):
            updates['session_secret'] = secrets.token_hex(32)
        if not valid_password_hash(admin.get('password_hash')):
            alphabet = string.ascii_letters + string.digits
            password = '-'.join(''.join(secrets.choice(alphabet) for _ in range(6)) for _ in range(4))
            updates['password_hash'] = generate_password_hash(password)
    if updates:
        updated = _replace_admin_values(content, config, updates)
        config = yaml.safe_load(updated)
        # Preserve the established bootstrap contract: no credential backup.
        transaction.write(
            config,
            create_backup=False,
            rendered_content=updated,
        )
    if password:
        print(f'WAMF temporary admin password: {password}\nSign in and change this password. It will not be displayed again.', flush=True)
    return config


def preflight(config):
    """Return setup findings while preserving the historical preflight API."""

    result = validate_config(config)
    if result.errors:
        raise ValueError("; ".join(str(issue) for issue in result.errors))
    return [issue.field for issue in result.readiness_issues]


def prepare_native_startup():
    try:
        config = bootstrap_config()
        return preflight(config)
    except (ConfigPersistenceError, OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f'WAMF startup configuration error: {exc}') from None
