from datetime import datetime
import glob
import os
import re
import shutil

import yaml


SENSITIVE_CONFIG_BLOCKS = {'admin', 'api'}


def get_config_path():
    return os.environ.get(
        'WHOSATMYFEEDER_CONFIG',
        './config/config.yml'
    )


def strip_sensitive_config_blocks(config_content):
    """Remove config sections that should not be shown in the browser editor."""
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


def strip_admin_config_block(config_content):
    return strip_sensitive_config_blocks(config_content)


def load_config_file_content():
    with open(get_config_path(), 'r') as config_file:
        return config_file.read()


def load_config_from_content(config_content):
    return yaml.safe_load(config_content) or {}


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


def write_config_preserving_admin(config_content, admin_config=None, api_config=None, reload_callback=None):
    if admin_config is None:
        admin_config = get_existing_admin_config()

    if api_config is None:
        api_config = get_existing_api_config()

    sanitized_content = strip_sensitive_config_blocks(config_content)
    load_config_from_content(sanitized_content)
    final_content = append_sensitive_config_blocks(
        sanitized_content,
        admin_config,
        api_config
    )
    load_config_from_content(final_content)

    backup_path = (
        f"{get_config_path()}."
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
    )

    shutil.copy2(
        get_config_path(),
        backup_path
    )

    with open(
        get_config_path(),
        'w'
    ) as config_file:
        config_file.write(
            final_content
        )

    if reload_callback:
        reload_callback()


def update_admin_password_hash(password_hash, reload_callback=None):
    current_content = load_config_file_content()
    current_config = load_config_from_content(current_content)
    admin_config = current_config.get('admin', {})
    admin_config['password_hash'] = password_hash
    write_config_preserving_admin(
        current_content,
        admin_config,
        reload_callback=reload_callback
    )


def update_api_token_hash(token_hash, reload_callback=None):
    current_content = load_config_file_content()
    current_config = load_config_from_content(current_content)
    api_config = current_config.get('api', {})
    api_config.setdefault('token_auth_enabled', True)
    api_config['token_hash'] = token_hash
    write_config_preserving_admin(
        current_content,
        current_config.get('admin'),
        api_config,
        reload_callback=reload_callback
    )


def get_config_file_metadata():
    config_path = get_config_path()

    return {
        'config_path': config_path,
        'file_size': os.path.getsize(config_path),
        'last_modified': datetime.fromtimestamp(
            os.path.getmtime(config_path)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        'backup_count': len(glob.glob(f"{config_path}.*.bak")),
    }
