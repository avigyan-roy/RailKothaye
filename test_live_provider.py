"""Offline contract tests; synthetic fixtures use the verified RailRadar shape."""
import copy
import io
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import live_provider as provider
from app import app

NOW = datetime.fromisoformat('2026-09-23T22:10:00+05:30')
FIXTURE = {
    'trainNumber': '12301', 'trainName': 'Test Rajdhani',
    'startDate': '2026-09-23', 'lastUpdatedAt': NOW.isoformat(),
    'status': 'running', 'isLive': True, 'delayMinutes': 4,
    'train': {'number': '12301', 'distance': 1450.1},
    'currentLocation': {'stationCode': 'PRP', 'stationName': 'Paharpur',
                        'coordinates': {'lat': 24.66, 'lng': 85.16},
                        'distanceFromOriginKm': 430.9, 'status': 'departed'},
    'previousHalt': {'stationCode': 'PNME'}, 'nextHalt': {'stationCode': 'GAYA'},
    'route': [
        {'stationCode': 'HWH', 'stationName': 'Howrah', 'isHalt': True,
         'distance': 0, 'status': 'departed', 'lat': 22.58, 'lng': 88.34,
         'scheduledDeparture': '2026-09-23T16:50:00+05:30'},
        {'stationCode': 'PNME', 'stationName': 'Parasnath', 'isHalt': True,
         'distance': 325, 'status': 'departed',
         'scheduledArrival': '2026-09-23T21:10:00+05:30',
         'actualArrival': '2026-09-23T21:14:00+05:30',
         'actualDeparture': '2026-09-23T21:16:00+05:30'},
        {'stationCode': 'PRP', 'stationName': 'Paharpur', 'isHalt': False,
         'distance': 430, 'lat': 24.66, 'lng': 85.16, 'status': 'departed'},
        {'stationCode': 'GAYA', 'stationName': 'Gaya', 'isHalt': True,
         'distance': 470, 'status': 'upcoming',
         'scheduledArrival': '2026-09-23T22:32:00+05:30',
         'actualArrival': '2026-09-23T22:36:00+05:30'},
        {'stationCode': 'NDLS', 'stationName': 'New Delhi', 'isHalt': True,
         'distance': 1450.1, 'status': 'upcoming', 'lat': 28.64, 'lng': 77.22,
         'scheduledArrival': '2026-09-24T10:05:00+05:30',
         'actualArrival': '2026-09-24T10:09:00+05:30'},
    ],
}


class LiveProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'RAILRADAR_API_KEY': 'test-private-token',
                             'RAILRADAR_BASE_URL': 'https://api.railradar.in/v1',
                             'RAILETA_DATA_MODE': 'LIVE', 'LIVE_POLL_SECONDS': '60'})
        self.env.start()
        self.addCleanup(self.env.stop)
        provider._CACHE.clear()
        provider._COOLDOWNS.clear()
        self.raw = copy.deepcopy(FIXTURE)
        self.client = app.test_client()

    def normalize(self, **kwargs):
        return provider.normalize(self.raw, '12301', '2026-09-23', NOW, now=kwargs.get('now', NOW))

    def test_reported_events_and_overnight_projections_are_separate(self):
        result = self.normalize()
        self.assertEqual(result['freshness'], 'FRESH')
        self.assertEqual(result['journey_status'], 'RUNNING')
        self.assertEqual(result['prediction_model'], 'PROVIDER_ONLY')
        self.assertIsNone(result['stops'][0]['actual_dep'])
        self.assertEqual(result['stops'][1]['actual_arr'], FIXTURE['route'][1]['actualArrival'])
        destination = result['stops'][-1]
        self.assertEqual(destination['predicted_eta'], '2026-09-24T10:09:00+05:30')
        self.assertIsNone(destination['actual_arr'])
        self.assertEqual(destination['baseline_eta'], destination['predicted_eta'])
        self.assertIsNone(destination['confidence'])
        self.assertEqual(result['position']['source'], 'PROVIDER_REPORTED')
        self.assertIsNone(result['position']['accuracy_m'])
        self.assertIsNone(result['position']['speed_kmh'])

    def test_missing_position_speed_delay_and_eta_stay_unknown(self):
        self.raw.pop('currentLocation')
        self.raw.pop('delayMinutes')
        self.raw['route'][-1].pop('actualArrival')
        result = self.normalize()
        self.assertEqual(result['position']['source'], 'UNAVAILABLE')
        self.assertIsNone(result['position']['latitude'])
        self.assertIsNone(result['position']['speed_kmh'])
        self.assertIsNone(result['current_delay_min'])
        self.assertIsNone(result['stops'][-1]['baseline_eta'])
        self.assertIsNone(result['stops'][-1]['predicted_eta'])

    def test_station_position_is_estimated(self):
        self.raw['currentLocation'].pop('coordinates')
        result = self.normalize()
        self.assertEqual(result['position']['source'], 'ESTIMATED')
        self.assertIn('Approximate', result['position']['basis'])

    def test_stale_and_invalid_observation_times(self):
        self.assertEqual(self.normalize(now=NOW + timedelta(minutes=6))['freshness'], 'STALE')
        for value in (None, 'broken', '2026-09-23T22:10:00',
                      (NOW + timedelta(minutes=10)).isoformat(),
                      '2026-09-22T22:10:00+05:30'):
            with self.subTest(value=value):
                self.raw['lastUpdatedAt'] = value
                result = self.normalize()
                self.assertEqual(result['freshness'], 'UNAVAILABLE')
                self.assertIsNone(result['observed_at'])
                self.assertIsNone(result['stops'][-1]['predicted_eta'])
                self.assertEqual(result['fetched_at'], NOW.isoformat())

    def test_not_live_flag_is_not_fresh(self):
        self.raw['isLive'] = False
        self.assertTrue(self.normalize()['stale'])

    def test_journey_states_never_invent_arrivals(self):
        for status, expected in [('cancelled', 'CANCELLED'), ('completed', 'COMPLETED'),
                                 ('unknown', 'UNAVAILABLE'), ('not_started', 'NOT_STARTED')]:
            with self.subTest(status=status):
                self.raw['status'] = status
                result = self.normalize()
                self.assertEqual(result['journey_status'], expected)
                self.assertIsNone(result['stops'][-1]['actual_arr'])
                if status != 'not_started':
                    self.assertIsNone(result['stops'][-1]['predicted_eta'])
        self.raw['status'] = 'completed'
        self.raw['lastUpdatedAt'] = '2026-09-24T10:10:00+05:30'
        self.raw['route'][-1]['status'] = 'arrived'
        result = self.normalize(now=NOW + timedelta(days=1))
        self.assertEqual(result['stops'][-1]['status'], 'ARRIVED')
        self.assertEqual(result['stops'][-1]['actual_arr'], '2026-09-24T10:09:00+05:30')

    def test_malformed_or_wrong_journey_is_rejected(self):
        for field, value in [('trainNumber', '12302'), ('startDate', '2026-09-22'),
                             ('startDate', None), ('route', []), ('route', [None])]:
            with self.subTest(field=field, value=value):
                raw = copy.deepcopy(FIXTURE)
                raw[field] = value
                with self.assertRaises(provider.ProviderError):
                    provider.normalize(raw, '12301', '2026-09-23', NOW, now=NOW)

    def test_selected_request_auth_cache_and_safe_fields(self):
        raw = copy.deepcopy(FIXTURE)
        raw['trainName'] = 'test-private-token'
        body = json.dumps({'success': True, 'data': raw}).encode()
        with patch.object(provider._OPENER, 'open', return_value=io.BytesIO(body)) as opened:
            first = self.client.get('/api/train/12301/live?date=2026-09-23')
            second = self.client.get('/api/train/12301/live?date=2026-09-23')
        self.assertEqual(first.status_code, 200)
        self.assertNotIn(b'test-private-token', first.data)
        self.assertTrue(second.json['cache_hit'])
        self.assertEqual(opened.call_count, 1)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-private-token')
        self.assertEqual(request.full_url, 'https://api.railradar.in/v1/trains/12301/live?includeCoordinates=true&date=2026-09-23')
        self.assertEqual(opened.call_args.kwargs['timeout'], 8)
        self.assertEqual(first.headers['Cache-Control'], 'no-store')

    def test_http_failures_are_safe_and_retry_after_is_respected(self):
        for upstream, expected, code in [(401, 503, 'PROVIDER_AUTH_FAILED'),
                                        (429, 429, 'PROVIDER_RATE_LIMIT'),
                                        (503, 503, 'PROVIDER_UNAVAILABLE')]:
            with self.subTest(upstream=upstream):
                provider._COOLDOWNS.clear()
                error = HTTPError('https://api.railradar.in/v1', upstream,
                                  'test-private-token', {'Retry-After': '120'}, io.BytesIO(b'test-private-token'))
                with patch.object(provider._OPENER, 'open', side_effect=error) as opened:
                    response = self.client.get('/api/train/12301/live')
                    self.assertEqual(response.status_code, expected)
                    self.assertEqual(response.json['code'], code)
                    self.assertNotIn(b'test-private-token', response.data)
                    if upstream in (429, 503):
                        self.assertEqual(response.headers['Retry-After'], '120')
                        again = self.client.get('/api/train/12302/live')
                        self.assertEqual(again.status_code, expected)
                        self.assertEqual(opened.call_count, 1)

    def test_timeout_network_malformed_and_oversized_responses(self):
        for error, status in [(TimeoutError('test-private-token'), 504),
                              (URLError('test-private-token'), 503)]:
            provider._COOLDOWNS.clear()
            with patch.object(provider._OPENER, 'open', side_effect=error):
                response = self.client.get('/api/train/12301/live')
                self.assertEqual(response.status_code, status)
                self.assertNotIn(b'test-private-token', response.data)
        provider._COOLDOWNS.clear()
        for body in (b'<html>test-private-token', b'{}', b'x' * (provider.MAX_BYTES + 1)):
            with patch.object(provider._OPENER, 'open', return_value=io.BytesIO(body)):
                response = self.client.get('/api/train/12301/live')
                self.assertEqual(response.status_code, 502)
                self.assertNotIn(b'test-private-token', response.data)

    def test_validation_and_missing_key_never_contact_provider(self):
        with patch.object(provider._OPENER, 'open') as opened:
            for url in ('/api/train/abc/live', '/api/train/12301/live?date=2026-02-30'):
                self.assertEqual(self.client.get(url).status_code, 400)
            with patch.dict(os.environ, {'RAILRADAR_API_KEY': ''}):
                response = self.client.get('/api/train/12301/live')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json['code'], 'LIVE_NOT_CONFIGURED')
            opened.assert_not_called()

    def test_modes_controls_and_homepage_routes(self):
        with patch('app.live_payload', side_effect=AssertionError('Replay called')):
            with patch('live_provider._fetch', return_value=copy.deepcopy(FIXTURE)):
                self.assertEqual(self.client.get('/api/train/12301/live').json['data_mode'], 'LIVE')
        with patch('live_provider.get_live', side_effect=AssertionError('Live called')):
            self.assertEqual(self.client.get('/api/train/12301/live?mode=DEMO').json['data_mode'], 'REPLAY')
        for url in ('/api/sim', '/api/conditions'):
            self.assertEqual(self.client.post(url, json={}).status_code, 409)
        self.assertEqual(self.client.get('/api/evaluation').status_code, 409)
        for url in ('/', '/index.html', '/static/app.js', '/static/style.css'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'test-private-token', response.data)
            response.close()
        self.assertNotIn(b'test-private-token', self.client.get('/api/config').data)

    def test_no_redirects_or_alternate_credential_destinations(self):
        with self.assertRaises(URLError):
            provider.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')
        with patch.dict(os.environ, {'RAILRADAR_BASE_URL': 'https://example.com/v1'}):
            with patch.object(provider._OPENER, 'open') as opened:
                self.assertEqual(self.client.get('/api/train/12301/live').status_code, 503)
                opened.assert_not_called()


if __name__ == '__main__':
    unittest.main()
