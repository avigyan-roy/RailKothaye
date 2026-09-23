"""Acceptance checks using only unittest and Flask's built-in test client."""
import json
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import app
import replay
from geo import km_to_latlon, snap_to_route
from predictor import predict_journey
from state import build_state


class RailETATests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def at(self, offset, number='12301'):
        with self.client.session_transaction() as session:
            session['clock'] = {**replay.new_clock(), 'speed':1, 'start_offset_min':offset}
        response = self.client.get(f'/api/train/{number}/live')
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_geometry_in_both_directions(self):
        for train in app.TRAINS.values():
            for km in [0, 95, 300, 850, 1447]:
                lat, lon = km_to_latlon(km, train['route'])
                snap = snap_to_route(lat,lon,train['route'])
                self.assertAlmostEqual(snap['km'],km,places=5)
                self.assertLess(snap['off_route_km'],.001)

    def test_replay_progress_and_termination(self):
        for number in app.TRAINS:
            positions = [self.at(offset,number)['position']['km_from_origin'] for offset in [20,180,300,550,840,1100]]
            self.assertEqual(positions,sorted(positions))
            self.assertAlmostEqual(positions[-1],1447,places=2)
            self.assertEqual(self.at(1100,number)['stops'][-1]['status'],'ARRIVED')

    def test_replay_files_and_final_ping(self):
        for train in app.TRAINS.values():
            rows = replay.read_replay(train['train_number'])
            self.assertTrue(all(b['t_offset_sec'] > a['t_offset_sec'] for a,b in zip(rows,rows[1:])))
            self.assertEqual(rows[-1]['latitude'],train['route'][-1]['lat'])
            self.assertEqual(rows[-1]['longitude'],train['route'][-1]['lon'])

    def test_uncertainty_and_no_early_departure(self):
        for number in app.TRAINS:
            for offset in [0,20,142,195,328,520,620,900]:
                payload = self.at(offset,number)
                for stop in payload['stops']:
                    if 'predicted_eta' not in stop:
                        continue
                    self.assertLessEqual(stop['eta_low'],stop['predicted_eta'])
                    self.assertLessEqual(stop['predicted_eta'],stop['eta_high'])
                    self.assertGreaterEqual(stop['predicted_eta'],payload['sim_time'])
                    if stop.get('predicted_dep'):
                        self.assertGreaterEqual(stop['predicted_dep'],stop['sched_dep'])
                    trend = [r for r in stop['reasons'] if r in ('Losing time','Recovering delay (timetable slack)','In line with current delay')]
                    self.assertEqual(len(trend),1)
                    self.assertTrue(stop['predicted_eta'].endswith('+05:30'))

    def test_condition_report_and_clear(self):
        def arrival(offset):
            return datetime.fromisoformat(self.at(offset)['stops'][-1]['predicted_eta'])
        increase = (arrival(330.1)-arrival(329.9)).total_seconds()/60
        decrease = (arrival(420.1)-arrival(419.9)).total_seconds()/60
        self.assertGreater(increase,10)
        self.assertLess(decrease,-10)

    def test_gps_gap_and_outlier_rejection(self):
        gap = self.at(668)
        self.assertEqual(gap['position']['source'],'ESTIMATED')
        self.assertLessEqual(gap['position']['km_from_origin'],1007.5)
        self.assertEqual(gap['position']['rejected_outliers'],3)
        self.assertEqual(self.at(675)['position']['source'],'MEASURED')

    def test_signal_lost_and_no_confirmed_stop_crossing(self):
        train=app.TRAINS['12301']
        now=replay.start_time(train)+timedelta(minutes=680)
        old=replay.pings_until(train,now-timedelta(minutes=20))
        state=build_state(train,old,now,app.STATS,replay.start_time(train).date())
        self.assertIn('SIGNAL_LOST',state['warnings'])
        self.assertLessEqual(state['km_from_origin'],1007.5)

    def test_unscheduled_halt(self):
        train=app.TRAINS['12301']
        candidates=[r for r in replay.read_replay('12301') if r['speed_kmh']==0 and 270 < snap_to_route(r['latitude'],r['longitude'],train['route'])['km'] < 450]
        self.assertGreater(len(candidates),2)
        payload=self.at(candidates[2]['t_offset_sec']/60)
        self.assertTrue(payload['position']['unscheduled_halt'])
        self.assertIn('Unscheduled halt detected',payload['stops'][3]['reasons'])

    def test_manual_reports_change_eta_and_are_reversible(self):
        self.at(20)
        before=self.client.get('/api/train/12301/live').json['stops'][-1]['predicted_eta']
        response=self.client.post('/api/conditions',json={'type':'CONGESTION','train_number':'12301'})
        self.assertEqual(response.status_code,200)
        after=self.client.get('/api/train/12301/live').json['stops'][-1]['predicted_eta']
        # Require a visible destination change, not the full section penalty.
        self.assertGreater((datetime.fromisoformat(after)-datetime.fromisoformat(before)).total_seconds(),120)
        self.client.delete('/api/conditions')
        cleared=self.client.get('/api/train/12301/live').json['stops'][-1]['predicted_eta']
        self.assertLess(abs((datetime.fromisoformat(cleared)-datetime.fromisoformat(before)).total_seconds()),2)

    def test_session_survives_worker_state_reset_and_is_isolated(self):
        self.at(400)
        self.client.post('/api/sim',json={'speed':10})
        replay.CLOCK=replay.new_clock()
        state=self.client.get('/api/sim').json
        self.assertEqual(state['speed'],10)
        self.assertGreaterEqual(state['offset_min'],400)
        other=app.app.test_client().get('/api/sim').json
        self.assertEqual(other['speed'],60)
        self.assertLess(other['offset_min'],21)
        self.assertNotIn('Set-Cookie',self.client.get('/api/sim').headers)

    def test_control_validation_and_errors_are_json(self):
        for path in ['/api/train/999/live','/api/train/999/eta','/api/station/NOPE/arrivals','/missing']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,404)
            self.assertIsNotNone(response.json)
        for body in [{'speed':0},{'speed':True},{'restart':'yes'},{'jump_before':'BWN'},[]]:
            response=self.client.post('/api/sim',json=body)
            self.assertEqual(response.status_code,400,response.json)
        for body in [{'type':'BAD'},{'type':'SPEED_RESTRICTION','speed_kmh':0},{'type':'WEATHER_FOG','factor':float('nan')}]:
            self.assertEqual(self.client.post('/api/conditions',json=body).status_code,400)

    def test_speed_jump_restart_and_endpoint_contracts(self):
        self.at(20)
        result=self.client.post('/api/sim',json={'jump_before':'GAYA','speed':120})
        self.assertEqual(result.status_code,200)
        self.assertTrue(310 < result.json['offset_min'] < 330)
        self.assertTrue(self.client.get('/api/train/12301/eta').json)
        arrivals=self.client.get('/api/station/CNB/arrivals').json
        self.assertEqual(arrivals,sorted(arrivals,key=lambda a:a['predicted_eta']))
        self.assertEqual(self.client.post('/api/sim',json={'restart':True}).json['start_offset_min'],20)
        for path in ('/', '/static/app.js'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code,200)

    def test_demo_event_shortcuts(self):
        gap=self.client.post('/api/sim',json={'jump_event':'gps_gap','speed':1})
        self.assertEqual(gap.status_code,200)
        self.assertEqual(self.client.get('/api/train/12301/live').json['position']['source'],'ESTIMATED')
        report=self.client.post('/api/sim',json={'jump_event':'congestion'})
        self.assertAlmostEqual(report.json['offset_min'],328,places=1)
        self.assertEqual(self.client.post('/api/sim',json={'jump_event':'bad'}).status_code,400)

    def test_json_only_train_fallback(self):
        from copy import deepcopy
        train = deepcopy(app.TRAINS['12301'])
        train.update(train_number='54321', name='Additional route (DEMO)')
        app.TRAINS['54321'] = train
        try:
            payload = self.at(100, '54321')
            self.assertEqual(payload['train_number'],'54321')
            self.assertGreater(payload['position']['km_from_origin'],0)
            self.assertIn('predicted_eta',payload['stops'][-1])
        finally:
            app.TRAINS.pop('54321')
            replay.REPLAYS.pop('54321',None)

    def test_backtest_completeness_and_metrics(self):
        result=self.client.get('/api/evaluation').json
        self.assertEqual(result['overall']['count'],840)
        self.assertLess(result['train_date_range'][1],result['test_date_range'][0])
        self.assertEqual(sum(r['count'] for r in result['horizons']),840)
        self.assertEqual(sum(r['count'] for r in result['distances']),840)
        self.assertLess(result['overall']['model']['mae'],result['overall']['baseline']['mae'])
        for row in result['horizons']+result['distances']:
            for model in ['baseline','model']:
                self.assertGreaterEqual(row[model]['within_10_pct'],row[model]['within_5_pct'])


if __name__=='__main__':
    unittest.main(verbosity=2)
