"""HTTP boundary coverage with fake bounded responses only."""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from app.frigate_client import FrigateClient, FrigateReviewError
from tests.test_review_ingestion import review_row


def response(value=None, status=200):
    reply = Mock()
    reply.status_code = status
    reply.iter_content.return_value = [json.dumps(value).encode()]
    return reply


@pytest.mark.parametrize('method,args,resource', [
    ('get_review', ('r1',), 'review/r1'),
    ('get_tracked_event', ('e1',), 'events/e1'),
])
def test_direct_lookup_bounded_closed(method, args, resource):
    reply = response(review_row() if method == 'get_review' else {'id': 'e1'})
    with patch('app.frigate_client.requests.get', return_value=reply) as get:
        getattr(FrigateClient('http://frigate/prefix/'), method)(*args)
    get.assert_called_once_with(f'http://frigate/prefix/api/{resource}', params=None,
                               timeout=(2, 2), stream=True, allow_redirects=False)
    reply.close.assert_called_once()


def test_older_review_ids_fallback_and_empty_missing():
    direct, fallback = response(status=404), response([review_row()])
    with patch('app.frigate_client.requests.get', side_effect=[direct, fallback]):
        assert FrigateClient('http://frigate').get_review('r1')['id'] == 'r1'
    direct.close.assert_called_once()
    fallback.close.assert_called_once()
    with (patch('app.frigate_client.requests.get', side_effect=[response(status=404), response([])]),
          pytest.raises(FrigateReviewError, match='reconciliation') as caught):
        FrigateClient('http://frigate').get_review('r1')
    assert caught.value.code == 'review_missing'


def test_event_membership_endpoint_and_interval_fallback_require_explicit_ids():
    with patch('app.frigate_client.requests.get', return_value=response(review_row())) as get:
        client = FrigateClient('http://frigate')
        assert client.get_review_for_event('e1', camera='feeder', start_time=101)['id'] == 'r1'
        assert get.call_args.args[0].endswith('/review/event/e1')
    with patch('app.frigate_client.requests.get', side_effect=[response(status=404), response([review_row()])]):
        assert client.get_review_for_event('e1', camera='feeder', start_time=101, end_time=170)['id'] == 'r1'
    with (patch('app.frigate_client.requests.get', side_effect=[response(status=404), response([review_row(members=())])]),
          pytest.raises(FrigateReviewError) as caught):
        client.get_review_for_event('e1', camera='feeder', start_time=101, end_time=170)
    assert caught.value.code == 'event_missing'


def test_unsupported_only_from_endpoint_evidence_not_empty_review_results():
    with patch('app.frigate_client.requests.get', return_value=response([])):
        assert FrigateClient('http://frigate').list_reviews('feeder') == []
    with (patch('app.frigate_client.requests.get', return_value=response(status=404)),
          pytest.raises(FrigateReviewError) as caught):
        FrigateClient('http://frigate').list_reviews('feeder')
    assert caught.value.code == 'unsupported'


def test_malformed_interval_membership_is_a_safe_retryable_response_error():
    row = {**review_row(), 'data': {'detections': None}}
    with (patch('app.frigate_client.requests.get', side_effect=[response(status=404), response([row])]),
          pytest.raises(FrigateReviewError) as caught):
        FrigateClient('http://frigate').get_review_for_event('e1', camera='feeder', start_time=101, end_time=170)
    assert caught.value.code == 'unexpected_response'


@pytest.mark.parametrize('failure', [requests.Timeout('PRIVATE'), requests.ConnectionError('PRIVATE')])
def test_safe_transport_failures(failure):
    with (patch('app.frigate_client.requests.get', side_effect=failure),
          pytest.raises(FrigateReviewError) as caught):
        FrigateClient('http://PRIVATE:PRIVATE@host').get_review('r1')
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('body', [b'invalid PRIVATE', b'x' * (1024 * 1024 + 1)])
def test_malformed_oversized_responses_are_safe_and_closed(body):
    reply = response()
    reply.iter_content.return_value = [body]
    with (patch('app.frigate_client.requests.get', return_value=reply),
          pytest.raises(FrigateReviewError) as caught):
        FrigateClient('http://frigate').list_reviews('feeder')
    assert caught.value.code == 'unexpected_response'
    reply.close.assert_called_once()
