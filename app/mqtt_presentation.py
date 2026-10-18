"""Safe, read-only presentation of the production MQTT consumer snapshot."""

from datetime import datetime, timezone
import re

from app.config_normalization import normalize_config
from app.config_validation import validate_config
from app.mqtt_runtime import MqttRuntimeSnapshot
from app.mqtt_settings import mqtt_settings_from_config
from app.retention_schedule import parse_retention_schedule


def build_mqtt_presentation(snapshot, config):
    zone = timezone.utc
    timezone_note = 'Times shown in UTC (fallback)'
    try:
        schedule = parse_retention_schedule(config)
        zone = schedule.timezone
        timezone_note = f'Times shown in {schedule.timezone_name}'
    except (AttributeError, TypeError, ValueError, OSError):
        pass

    def timestamp(value, missing='Unknown'):
        if value is None:
            return missing
        if not isinstance(value, str):
            return 'Unknown'
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                return 'Unknown'
            return parsed.astimezone(zone).strftime('%d %b %Y · %H:%M %Z')
        except (ValueError, OverflowError):
            return 'Unknown'

    broker = topic = 'Unavailable'
    ready = None
    if config is not None:
        try:
            mqtt_config = {'mqtt': config.get('mqtt', {})}
            validation = validate_config(mqtt_config)
            ready = not any(issue.field == 'mqtt' or issue.field.startswith('mqtt.')
                            for issue in (*validation.errors, *validation.readiness_issues))
            settings = mqtt_settings_from_config(normalize_config(mqtt_config).config)
            host = settings.host
            secrets = [value for value in (settings.username, settings.password)
                       if isinstance(value, str) and value]

            def safe_value(value):
                return (isinstance(value, str) and len(value) <= 512
                        and not any(ord(c) < 32 for c in value)
                        and not any(secret in value for secret in secrets)
                        and '://' not in value and '@' not in value)

            if (safe_value(host) and re.fullmatch(r'[A-Za-z0-9_.:\[\]-]+', host)
                    and type(settings.port) is int and 1 <= settings.port <= 65535):
                broker = f'{host if ":" not in host or host.startswith("[") else "[" + host + "]"}:{settings.port}'
            else:
                ready = False
            if safe_value(settings.topic_prefix):
                topic = f'{settings.topic_prefix}/events'
        except Exception:
            ready = False

    valid = (
        isinstance(snapshot, MqttRuntimeSnapshot)
        and type(snapshot.is_current) is bool
        and (snapshot.worker_generation is None or isinstance(snapshot.worker_generation, str))
        and isinstance(snapshot.connection_state, str)
        and isinstance(snapshot.subscription_state, str)
        and snapshot.connection_state in {
            'unknown', 'starting', 'subscribing', 'connected', 'reconnecting', 'disconnected', 'stopped',
        }
        and snapshot.subscription_state in {'unknown', 'pending', 'subscribed', 'failed'}
    )
    state = snapshot if valid else MqttRuntimeSnapshot()
    status, tone, online = 'Unknown', 'neutral', None
    freshness = 'No trustworthy current runtime status'
    if ready is False:
        status, tone = 'Setup required', 'warning'
        freshness = 'Saved MQTT connection settings require attention'
    elif ready is None:
        freshness = 'Saved configuration is unavailable'
    elif state.worker_generation and (state.connection_state == 'stopped' or not state.is_current):
        status, tone = 'Stale', 'warning'
        freshness = 'Worker stopped; previous observations are historical'
    elif state.is_current and state.worker_generation:
        freshness = 'Current worker generation; quiet topics are normal'
        if state.subscription_state == 'failed' and state.connection_state == 'subscribing':
            status, tone, online = 'Subscription failed', 'danger', False
        elif state.connection_state == 'connected' and state.subscription_state == 'subscribed':
            status, tone, online = 'Connected', 'success', True
        elif state.connection_state in ('starting', 'subscribing', 'reconnecting', 'disconnected'):
            status = state.connection_state.capitalize()
            tone = 'warning' if state.connection_state in ('reconnecting', 'disconnected') else 'neutral'
            online = False

    reasons = {
        'expected_disconnect': 'Expected disconnection',
        'unexpected_disconnect': 'Unexpected disconnection',
    }
    details = [
        ('Runtime status', status), ('Configured broker', broker), ('Configured event topic', topic),
        ('Worker started', timestamp(state.worker_started_at)),
        ('Last connected', timestamp(state.last_connected_at)),
        ('Last disconnected', timestamp(state.last_disconnected_at)),
        ('Disconnect reason', reasons.get(state.last_disconnect_reason, 'Not recorded')
         if isinstance(state.last_disconnect_reason, str) else 'Not recorded'),
        ('Last event message received', timestamp(state.last_event_message_at, 'No events received yet')),
        ('Runtime freshness', freshness), ('Last runtime update', timestamp(state.updated_at)),
    ]
    return {'status': status, 'tone': tone, 'online': online, 'details': details,
            'timezone_note': timezone_note}
