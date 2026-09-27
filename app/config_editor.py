from datetime import datetime
import os
import re
from pathlib import Path

import yaml

from app.config_migration import migrate_config, require_structurally_valid_migration
from app.config_persistence import (
    ConfigPersistenceTransaction,
    get_config_backup_paths as _get_config_backup_paths,
    prune_config_backups as _prune_config_backups,
)
from app.config_secrets import (
    empty_secrets,
    get_secrets_path,
    merge_embedded_secrets,
    parse_secrets_content,
    sanitize_config,
    secret_updates,
    validate_secrets,
)
from app.config_validation import validate_config


SENSITIVE_CONFIG_BLOCKS = {'admin', 'api'}
DEFAULT_CONFIG_BACKUPS_MAX_FILES = 10


def get_config_path():
    override = os.environ.get('WHOSATMYFEEDER_CONFIG')
    if override is not None:
        return str(Path(override).expanduser().resolve())
    return str(Path(__file__).resolve().parent.parent / 'config/config.yml')


def _without_sensitive_blocks(config_content):
    lines = config_content.splitlines()
    kept = []
    skipping_sensitive = False

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if skipping_sensitive:
            if not stripped or line.lstrip().startswith('#') or indent > 0:
                continue
            skipping_sensitive = False

        if indent == 0 and re.match(r'^(admin|api)\s*:', line):
            skipping_sensitive = True
            continue

        kept.append(line)

    return '\n'.join(kept).strip() + '\n'


def strip_sensitive_config_blocks(config_content):
    """Remove secret-bearing sections/leaves from browser-visible YAML."""

    visible = yaml.safe_load(_without_sensitive_blocks(config_content)) or {}
    return yaml.safe_dump(sanitize_config(visible), sort_keys=False)


def strip_admin_config_block(config_content):
    return strip_sensitive_config_blocks(config_content)


def load_config_file_content():
    with open(get_config_path(), 'r') as config_file:
        return config_file.read()


def load_config_from_content(config_content):
    return yaml.safe_load(config_content) or {}


def get_config_backups_max_files(config):
    retention_config = (config or {}).get('retention', {})
    return max(
        int(
            retention_config.get(
                'config_backups_max_files',
                DEFAULT_CONFIG_BACKUPS_MAX_FILES,
            )
        ),
        0,
    )


def get_config_backup_paths(config_path=None):
    config_path = config_path or get_config_path()
    return _get_config_backup_paths(config_path)


def prune_config_backups(config_path=None, max_files=DEFAULT_CONFIG_BACKUPS_MAX_FILES):
    return _prune_config_backups(config_path or get_config_path(), max_files)


def get_existing_admin_config():
    current_config = load_config_from_content(load_config_file_content())
    return current_config.get('admin')


def get_existing_api_config():
    current_config = load_config_from_content(load_config_file_content())
    return current_config.get('api')


def append_sensitive_config_blocks(config_content, admin_config, api_config):
    sanitized_content = strip_sensitive_config_blocks(config_content).rstrip()
    sensitive_config = {}

    if admin_config:
        sensitive_config['admin'] = admin_config

    if api_config:
        sensitive_config['api'] = api_config

    if not sensitive_config:
        return sanitized_content + '\n'

    sensitive_content = yaml.safe_dump(
        sensitive_config,
        sort_keys=False
    ).strip()

    return f"{sanitized_content}\n\n{sensitive_content}\n"


def _persist_composed_config(transaction, config_content, admin_config, api_config):
    sanitized_content = strip_sensitive_config_blocks(config_content)
    load_config_from_content(sanitized_content)
    final_content = append_sensitive_config_blocks(
        sanitized_content,
        admin_config,
        api_config,
    )
    final_config = load_config_from_content(final_content)
    migration = migrate_config(final_config)
    validation = validate_config(migration.config)
    require_structurally_valid_migration(migration, validation)
    sanitized = sanitize_config(migration.config)
    transaction.write(
        sanitized,
        backup_limit=get_config_backups_max_files(migration.config),
    )


def write_config_preserving_admin(config_content, admin_config=None, api_config=None, reload_callback=None):
    config_path = Path(get_config_path())
    with ConfigPersistenceTransaction(config_path) as transaction:
        current_config = load_config_from_content(
            transaction.read_text()
        )
        if admin_config is None:
            admin_config = current_config.get('admin')
        if api_config is None:
            api_config = current_config.get('api')
        # The editor does not expose admin/API blocks. MQTT secret leaves are
        # accepted for compatibility, with blank or omitted values preserving
        # the current stored credentials.
        submitted_content = _without_sensitive_blocks(config_content)
        submitted = load_config_from_content(submitted_content)
        editable_content = yaml.safe_dump(sanitize_config(submitted), sort_keys=False)
        secrets_path = get_secrets_path(config_path)
        with ConfigPersistenceTransaction(secrets_path) as secrets_transaction:
            secrets_existed = secrets_path.exists()
            existing_secrets = (
                parse_secrets_content(secrets_transaction.read_text())
                if secrets_existed
                else empty_secrets()
            )
            extracted = merge_embedded_secrets(current_config, existing_secrets)
            updated_secrets = secret_updates(submitted, extracted.secrets)
            if not secrets_existed or updated_secrets != existing_secrets:
                secrets_transaction.write(
                    updated_secrets,
                    create_backup=secrets_existed,
                    backup_limit=get_config_backups_max_files(current_config),
                )
        _persist_composed_config(
            transaction,
            editable_content,
            admin_config,
            api_config,
        )

    if reload_callback:
        reload_callback()


def update_admin_password_hash(password_hash, reload_callback=None):
    _update_secret("admin", "password_hash", password_hash)
    if reload_callback:
        reload_callback()


def update_api_token_hash(token_hash, reload_callback=None):
    _update_secret("api", "token_hash", token_hash)
    if reload_callback:
        reload_callback()


def _update_secret(section, field, value):
    """Persist one already-prepared secret through the shared transaction."""

    config_path = Path(get_config_path())
    current_config = load_config_from_content(config_path.read_text(encoding="utf-8"))
    secrets_path = get_secrets_path(config_path)
    with ConfigPersistenceTransaction(secrets_path) as transaction:
        existed = secrets_path.exists()
        existing = (
            parse_secrets_content(transaction.read_text())
            if existed
            else empty_secrets()
        )
        updated = dict(existing)
        updated[section] = dict(updated.get(section) or {})
        updated[section][field] = value
        transaction.write(
            validate_secrets(updated),
            create_backup=existed,
            backup_limit=get_config_backups_max_files(current_config),
        )


def get_config_file_metadata():
    config_path = get_config_path()

    return {
        'config_path': config_path,
        'file_size': os.path.getsize(config_path),
        'last_modified': datetime.fromtimestamp(
            os.path.getmtime(config_path)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        'backup_count': len(get_config_backup_paths(config_path)),
    }
