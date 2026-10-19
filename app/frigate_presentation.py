"""Camera-health display only; observation and classification remain untouched."""

from datetime import datetime, timezone

from app.retention_schedule import parse_retention_schedule


def build_camera_presentation(cameras, config):
    # Match the existing MQTT/retention configured-zone and UTC fallback policy.
    zone = timezone.utc
    try:
        zone = parse_retention_schedule(config).timezone
    except (AttributeError, TypeError, ValueError, OSError):
        pass

    def checked_time(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return parsed.astimezone(zone).strftime('%H:%M %Z')
        except (ValueError, OverflowError):
            pass
        return None

    def row(camera):
        observations = []
        for label, field in (('Inventory', 'inventory_observed_at'),
                             ('Runtime', 'runtime_observed_at')):
            checked = checked_time(camera.get(field))
            if checked:
                observations.append(f'{label} checked {checked}')
        return {**camera, 'observation_summary': ' · '.join(observations)}

    warnings = cameras.get('warnings', [])
    warning_ids = {camera['id'] for camera in warnings}
    return {
        **cameras,
        'warnings': [row(camera) for camera in warnings],
        'additional_rows': [row(camera) for camera in cameras.get('rows', [])
                            if camera['id'] not in warning_ids],
    }
