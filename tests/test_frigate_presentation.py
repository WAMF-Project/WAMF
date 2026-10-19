"""Display-only camera rows and configured local observation times."""

from copy import deepcopy

import pytest

from app.frigate_presentation import build_camera_presentation


def camera(camera_id='Garden', **values):
    return {'id': camera_id, 'display_name': 'Garden birds', 'status': 'healthy',
            'inventory_observed_at': '2026-10-19T19:45:14.552162+00:00',
            'runtime_observed_at': '2026-10-19T19:45:30Z', **values}


def test_warnings_render_once_without_changing_health_data():
    warning = camera('test', status='disabled', safe_reason='Camera is disabled in Frigate.')
    healthy = camera()
    neutral = camera('Quiet', status='unknown')
    source = {'rows': [warning, healthy, neutral], 'warnings': [warning],
              'summary': '1 healthy · 1 disabled · 1 unknown', 'degraded': True}
    before = deepcopy(source)
    view = build_camera_presentation(source, {})
    assert [row['id'] for row in view['warnings']] == ['test']
    assert [row['id'] for row in view['additional_rows']] == ['Garden', 'Quiet']
    assert view['summary'] == source['summary']
    assert view['warnings'][0]['safe_reason'] == warning['safe_reason']
    assert source == before


def test_only_warnings_have_no_additional_rows():
    warning = camera(status='disabled')
    view = build_camera_presentation({'rows': [warning], 'warnings': [warning]}, {})
    assert view['additional_rows'] == []


@pytest.mark.parametrize('timestamp,expected', [
    ('2026-10-19T19:45:14.552162+00:00', '20:45 BST'),
    ('2026-12-19T19:45:14+00:00', '19:45 GMT'),
    ('2026-10-25T00:30:00Z', '01:30 BST'),
    ('2026-10-25T01:30:00Z', '01:30 GMT'),
])
def test_configured_london_zone_handles_bst_gmt(timestamp, expected):
    row = camera(inventory_observed_at=timestamp, runtime_observed_at=None)
    view = build_camera_presentation({'rows': [row]}, {'retention': {'schedule': {'timezone': 'Europe/London'}}})
    assert view['additional_rows'][0]['observation_summary'] == f'Inventory checked {expected}'


@pytest.mark.parametrize('inventory,runtime,expected', [
    (None, '2026-10-19T19:45:00Z', 'Runtime checked 19:45 UTC'),
    ('2026-10-19T19:45:00Z', None, 'Inventory checked 19:45 UTC'),
    (None, None, ''),
    ('invalid', 123, ''),
    ('2026-10-19T19:45:00', None, ''),
    ('2026-10-19T19:45:00Z', '2026-10-19T19:46:00Z',
     'Inventory checked 19:45 UTC · Runtime checked 19:46 UTC'),
])
def test_absent_invalid_and_naive_times_are_omitted(inventory, runtime, expected):
    row = camera(inventory_observed_at=inventory, runtime_observed_at=runtime)
    view = build_camera_presentation({'rows': [row]}, {})
    assert view['additional_rows'][0]['observation_summary'] == expected


def test_unavailable_timezone_uses_existing_utc_fallback_policy():
    view = build_camera_presentation({'rows': [camera()]}, None)
    assert view['additional_rows'][0]['observation_summary'] == 'Inventory checked 19:45 UTC · Runtime checked 19:45 UTC'
