"""Live-only application contracts, independent of provider availability."""
import copy
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from live_provider import normalize
from test_live_provider import FIXTURE, NOW

ROOT = Path(__file__).resolve().parent


class RailETATests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.payload = normalize(copy.deepcopy(FIXTURE), '12301', None, NOW, now=NOW)

    def test_live_is_unconditional_even_with_old_demo_environment(self):
        with patch.dict(os.environ, {'RAILETA_DATA_MODE': 'DEMO', 'RAILRADAR_API_KEY': ''}):
            self.assertEqual(self.client.get('/api/config').json['data_mode'], 'LIVE')
            missing = self.client.get('/api/train/12301/live')
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.json['code'], 'LIVE_NOT_CONFIGURED')
            self.assertNotIn('stops', missing.json)

    def test_old_mode_query_cannot_enable_demo(self):
        with patch('live_provider.get_live') as provider:
            for route in ('/api/config', '/api/trains', '/api/train/12301/live',
                          '/api/train/12301/eta', '/api/station/NDLS/arrivals'):
                result = self.client.get(route + '?mode=DEMO')
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json['code'], 'LIVE_ONLY')
            provider.assert_not_called()

    def test_removed_simulation_endpoints(self):
        for path in ('/api/sim', '/api/conditions', '/api/evaluation'):
            for method in ('GET', 'POST', 'DELETE'):
                result = self.client.open(path, method=method)
                self.assertEqual(result.status_code, 404)
                self.assertIsNotNone(result.json)

    def test_selected_train_and_date_are_forwarded(self):
        with patch('live_provider.get_live', return_value=self.payload) as provider:
            response = self.client.get('/api/train/12301/live?date=2026-09-23')
            provider.assert_called_once_with('12301', '2026-09-23')
        self.assertEqual(response.json['data_mode'], 'LIVE')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Set-Cookie', response.headers)

    def test_eta_and_station_routes_use_provider_data(self):
        with patch('live_provider.get_live', return_value=self.payload) as provider:
            eta = self.client.get('/api/train/12301/eta').json
            self.assertEqual([row['code'] for row in eta], ['GAYA', 'NDLS'])
            self.assertTrue(all(row['eta_source'] == 'RailRadar' for row in eta))
            station = self.client.get('/api/station/ndls/arrivals?train_number=12301').json
            self.assertEqual(station[0]['predicted_eta'], '2026-09-24T10:09:00+05:30')
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(self.client.get('/api/station/NDLS/arrivals').status_code, 400)
            self.assertEqual(provider.call_count, 2)

    def test_homepage_and_frontend_have_only_live_controls(self):
        for path in ('/', '/index.html', '/static/index.html'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'LIVE DATA', response.data)
                for removed in (b'id="data-mode"', b'Replay studio', b'Simulation controls',
                                b'id="speed"', b'id="accuracy"', b'DEMO', b'required title='):
                    self.assertNotIn(removed, response.data)
        for path in ('/static/app.js', '/static/style.css'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_train_list_does_not_query_provider_or_offer_seed_trains(self):
        with patch('live_provider.get_live') as provider:
            self.assertEqual(self.client.get('/api/trains').json, [])
            provider.assert_not_called()

    def test_runtime_imports_no_replay_or_predictor(self):
        # A clean process checks imports without contamination from other tests.
        result = subprocess.run([sys.executable, '-c',
            "import app, sys; assert not {'replay','predictor','state','generate_data','evaluate'} & sys.modules.keys()"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
