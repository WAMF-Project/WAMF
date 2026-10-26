"""Native first-run configuration and credentials, before worker creation."""
import fcntl
import os
from pathlib import Path
import secrets
import string
from urllib.parse import urlsplit

import yaml
from werkzeug.security import generate_password_hash

from app.config_editor import get_config_path
from app.credentials import valid_credential_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 7767


def is_placeholder(value):
    if not isinstance(value, str) or not value.strip():
        return True
    value = value.strip().lower()
    return '<' in value or value.startswith(('your-', 'your_', 'change-me', 'changeme'))


def valid_password_hash(value):
    return valid_credential_hash(value)


def _unconfigured(value, example):
    # Only known example text counts as unconfigured, not arbitrary bad hashes.
    return value is None or isinstance(value, str) and (not value.strip() or value.strip() == example)


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
    # Lock the directory so concurrent first starts cannot read a half-copied config.
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        return _bootstrap_locked(path)
    finally:
        os.close(directory_fd)


def _bootstrap_locked(path):
    if not path.exists():
        example = REPO_ROOT / 'config/config.yml.example'
        example_content = example.read_text(encoding='utf-8')
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
                target.write(example_content)
            print(f'Created {path} from the example. Review external settings before starting WAMF.', flush=True)
        except FileExistsError:
            pass

    # Never print credentials before persistence.
    with path.open('r', encoding='utf-8') as target:
        content = target.read()
        config = yaml.safe_load(content) or {}
        if not isinstance(config, dict):
            raise ValueError('Configuration must be a YAML mapping')
        admin = config.get('admin') or {}
        if not isinstance(admin, dict):
            raise ValueError('admin must be a YAML mapping')
        api = config.get('api') or {}
        if not isinstance(api, dict):
            raise ValueError('api must be a YAML mapping')
        password_missing = _unconfigured(admin.get('password_hash'), '<generated werkzeug password hash>')
        token_missing = _unconfigured(api.get('token_hash'), '<generated werkzeug token hash>')
        secret_missing = _unconfigured(admin.get('session_secret'), '<long random session secret>')
        fresh = password_missing and token_missing and secret_missing
        # Validate everything before provisioning or writing anything. Existing
        # invalid credentials are configuration errors, never a reset request.
        if not password_missing and not valid_password_hash(admin.get('password_hash')):
            raise ValueError('admin.password_hash has a malformed or unsupported credential format')
        if not token_missing and not valid_credential_hash(api.get('token_hash'), for_api=True):
            raise ValueError('api.token_hash has a malformed or unsupported credential format')
        updates = {}
        password = None
        if admin.get('auth_enabled', False):
            env_secret = os.environ.get('WAMF_SECRET_KEY')
            if env_secret and _unconfigured(env_secret, '<long random session secret>'):
                raise ValueError('WAMF_SECRET_KEY must not be blank or a placeholder')
            if not secret_missing and not isinstance(admin.get('session_secret'), str):
                raise ValueError('admin.session_secret must be a string')
            if not fresh and password_missing:
                raise ValueError('Existing installation requires admin.password_hash; credentials were not changed')
            if not fresh and secret_missing and not env_secret:
                raise ValueError('Existing installation requires admin.session_secret or WAMF_SECRET_KEY; credentials were not changed')
            if fresh and not env_secret:
                updates['session_secret'] = secrets.token_hex(32)
            if fresh:
                alphabet = string.ascii_letters + string.digits
                password = '-'.join(''.join(secrets.choice(alphabet) for _ in range(6)) for _ in range(4))
                updates['password_hash'] = generate_password_hash(password)
        if updates:
            updated = _replace_admin_values(content, config, updates)
            # No backup containing credentials; preserve unrelated configuration.
            with path.open('r+', encoding='utf-8') as writable:
                os.fchmod(writable.fileno(), 0o600)
                writable.write(updated)
                writable.truncate()
                writable.flush()
                os.fsync(writable.fileno())
            config = yaml.safe_load(updated)
        if password:
            print(f'WAMF temporary admin password: {password}\nSign in and change this password. It will not be displayed again.', flush=True)
        return config


def preflight(config):
    """Return settings needing setup; reject structures unsafe for the web UI."""
    if not isinstance(config, dict):
        raise ValueError('Configuration must be a YAML mapping')
    for section in ('frigate', 'classification', 'webui', 'storage', 'media',
                    'admin', 'api', 'retention', 'bridge', 'camera', 'live_view'):
        if section in config and not isinstance(config[section], dict):
            raise ValueError(f'{section} must be a YAML mapping')
    webui = config.get('webui', {})
    port = webui.get('port', DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('webui.port must be an integer from 1 to 65535')
    if is_placeholder(webui.get('host', '0.0.0.0')):
        raise ValueError('webui.host must be a valid bind address')
    errors = []
    frigate = config.get('frigate') or {}
    if not isinstance(frigate, dict):
        raise ValueError('frigate must be a YAML mapping')
    for key in ('frigate_url', 'mqtt_server', 'main_topic'):
        if is_placeholder(frigate.get(key)):
            errors.append(f'frigate.{key}')
    try:
        url = urlsplit(str(frigate.get('frigate_url', '')))
        valid_url = url.scheme in ('http', 'https') and url.hostname
    except ValueError:
        valid_url = False
    if not valid_url:
        errors.append('frigate.frigate_url (HTTP/HTTPS URL required)')
    cameras = frigate.get('camera')
    if not isinstance(cameras, list) or not cameras or any(is_placeholder(c) for c in cameras):
        errors.append('frigate.camera')
    if frigate.get('mqtt_auth'):
        for key in ('mqtt_username', 'mqtt_password'):
            if is_placeholder(frigate.get(key)):
                errors.append(f'frigate.{key}')
    classification = config.get('classification') or {}
    if not isinstance(classification, dict):
        errors.append('classification (YAML mapping required)')
    else:
        if is_placeholder(classification.get('model')):
            errors.append('classification.model')
        threshold = classification.get('threshold')
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            errors.append('classification.threshold (number from 0 to 1 required)')
    port = frigate.get('mqtt_port', 1883)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append('frigate.mqtt_port (integer from 1 to 65535 required)')
    return errors


def prepare_native_startup():
    try:
        config = bootstrap_config()
        return preflight(config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SystemExit(f'WAMF startup configuration error: {exc}') from None
