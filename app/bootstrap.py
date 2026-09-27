"""Native first-run configuration and credentials, before worker creation."""
import hashlib
import os
from pathlib import Path
import re
import secrets
import string

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from app.config_editor import get_config_backups_max_files, get_config_path
from app.config_migration import (
    ConfigMigrationError,
    migrate_config,
    require_structurally_valid_migration,
)
from app.config_persistence import (
    ConfigPersistenceError,
    ConfigPersistenceTransaction,
    ensure_private_file,
)
from app.config_secrets import (
    apply_secrets,
    empty_secrets,
    get_secrets_path,
    merge_embedded_secrets,
    parse_secrets_content,
    validate_secrets,
)
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

    # Never print credentials before durable persistence.
    content = transaction.read_text()
    config = yaml.safe_load(content) or {}
    if not isinstance(config, dict):
        raise ValueError('Configuration must be a YAML mapping')
    migration = migrate_config(config)
    validation = validate_config(migration.config)
    require_structurally_valid_migration(migration, validation)
    if validation.errors:
        fields = ", ".join(issue.field for issue in validation.errors)
        raise ValueError(f"Configuration is structurally invalid: {fields}")

    ensure_private_file(path)
    ensure_private_file(transaction.lock_path)

    secrets_path = get_secrets_path(path)
    password = None
    with ConfigPersistenceTransaction(secrets_path) as secrets_transaction:
        secrets_existed = secrets_path.exists()
        if secrets_existed:
            existing_secrets = parse_secrets_content(
                secrets_transaction.read_text()
            )
            ensure_private_file(secrets_path)
        else:
            existing_secrets = empty_secrets()

        extraction = merge_embedded_secrets(
            migration.config,
            existing_secrets,
        )
        secrets_candidate = extraction.secrets
        admin = extraction.config.get('admin') or {}
        if not isinstance(admin, dict):
            raise ValueError('admin must be a YAML mapping')
        runtime_admin = dict(admin)
        stored_admin = secrets_candidate.get('admin') or {}
        if isinstance(stored_admin, dict):
            for field in ('session_secret', 'password_hash'):
                if field in stored_admin:
                    runtime_admin[field] = stored_admin[field]
        if admin.get('auth_enabled', False):
            env_secret = os.environ.get('WAMF_SECRET_KEY')
            if env_secret and is_placeholder(env_secret):
                raise ValueError('WAMF_SECRET_KEY must not be blank or a placeholder')
            secret_admin = secrets_candidate.setdefault('admin', {})
            if not env_secret and is_placeholder(runtime_admin.get('session_secret')):
                secret_admin['session_secret'] = secrets.token_hex(32)
            if not valid_password_hash(runtime_admin.get('password_hash')):
                alphabet = string.ascii_letters + string.digits
                password = '-'.join(
                    ''.join(secrets.choice(alphabet) for _ in range(6))
                    for _ in range(4)
                )
                secret_admin['password_hash'] = generate_password_hash(password)

        secrets_candidate = validate_secrets(secrets_candidate)
        # Credentials are always made durable before config sanitization.
        if not secrets_existed or secrets_candidate != existing_secrets:
            secrets_transaction.write(
                secrets_candidate,
                create_backup=secrets_existed,
                backup_limit=get_config_backups_max_files(extraction.config),
            )

    sanitized = extraction.config
    sanitized_validation = validate_config(sanitized)
    if sanitized_validation.errors:
        fields = ", ".join(issue.field for issue in sanitized_validation.errors)
        raise ValueError(f"Configuration is structurally invalid: {fields}")

    if migration.migrated or extraction.embedded_paths:
        transaction.write(
            sanitized,
            backup_limit=get_config_backups_max_files(sanitized),
        )

    ensure_private_file(secrets_path)
    ensure_private_file(f"{secrets_path}.lock")

    config = apply_secrets(sanitized, secrets_candidate)
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
    except (
        ConfigMigrationError,
        ConfigPersistenceError,
        OSError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        raise SystemExit(f'WAMF startup configuration error: {exc}') from None
