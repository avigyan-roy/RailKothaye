"""RailETA Flask server. Run generate_data.py, evaluate.py, then python app.py."""
import json
import os
import math
import uuid
from datetime import date, datetime
from pathlib import Path
from flask import Flask, jsonify, request, session, g
from werkzeug.exceptions import HTTPException
import replay
import live_provider
from geo import route_polyline
from predictor import build_stats, load_history, predict_journey
from state import build_state

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / 'static'))
app.json.ensure_ascii = False
# The fallback signs only unprivileged demo preferences, never identity or access.
app.secret_key = os.environ.get('RAILETA_SESSION_SECRET', 'raileta-public-demo-preferences-v1')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('VERCEL')), MAX_CONTENT_LENGTH=16384)


def read_json(filename):
    return json.loads((ROOT / 'data' / filename).read_text(encoding='utf-8'))


STATIONS = {s['code']: s for s in read_json('stations.json')}
TRAINS = read_json('trains.json')
for train in TRAINS.values():
    train['route'] = route_polyline(train, STATIONS)
SCRIPTED = read_json('conditions.json')
STATS = build_stats(load_history(), TRAINS)



@app.before_request
def restore_replay_session():
    if data_mode() == 'LIVE':
        return
    # Each browser sends its complete demo state in Flask's signed cookie. Any
    # serverless instance can reconstruct the same clock without disk writes.
    saved = session.get('clock')
    if not isinstance(saved, dict) or not all(k in saved for k in ('speed','start_offset_min','real_start','journey_date')):
        saved = replay.new_clock()
    g.clock_token = replay.REQUEST_CLOCK.set(dict(saved))
    g.manual = list(session.get('manual_conditions', []))
    g.original_clock = dict(session['clock']) if 'clock' in session else None
    g.original_manual = list(g.manual)


@app.after_request
def save_replay_session(response):
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    if hasattr(g, 'manual') and request.path.startswith('/api/'):
        session['clock'] = replay.clock()
        session['manual_conditions'] = g.manual
        # Flask writes the cookie only on changes. Reads must not overwrite a
        # simultaneous control request's newer cookie with an old snapshot.
        session.modified = session.get('clock') != getattr(g, 'original_clock', None) or session.get('manual_conditions') != getattr(g, 'original_manual', None)
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.teardown_request
def release_replay_session(error):
    if hasattr(g, 'clock_token'):
        replay.REQUEST_CLOCK.reset(g.clock_token)


def serial(value):
    """Flask's default dates use HTTP-date; our API always uses ISO IST."""
    if isinstance(value, datetime):
        return value.isoformat(timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


def get_train(number):
    if number not in TRAINS:
        return None
    return TRAINS[number]


def unknown_train():
    return jsonify(error='Unknown train number', available=list(TRAINS)), 404


def active_conditions(train, now):
    offset = (now - replay.start_time(train)).total_seconds() / 60
    return [c for c in SCRIPTED + g.manual if train['train_number'] in c['train_numbers']
            and c['reported_at_min'] <= offset
            and (c.get('cleared_at_min') is None or offset < c['cleared_at_min'])]


def live_payload(train):
    now = replay.sim_now(train)
    journey_date = replay.start_time(train).date()
    state = build_state(train, replay.pings_until(train, now), now, STATS, journey_date)
    conditions = active_conditions(train, now)
    predictions = {p['code']: p for p in predict_journey(train, state, now, conditions, STATS)}
    stops = []
    for i, s in enumerate(replay.schedule(train, journey_date)):
        event = state['events'][s['code']]
        status = ('DEPARTED' if event.get('actual_dep') else
                  'ARRIVED' if event.get('actual_arr') and not s['sched_dep'] else
                  'AT_STATION' if (event.get('actual_arr') or i == 0 and state['at_station'] and state['current_index'] == 0) else 'UPCOMING')
        item = {'seq': i + 1, 'code': s['code'], 'name': s['name'], 'status': status,
                'km_from_origin': s['km'], 'km_to_go': round(max(0, s['km'] - state['km_from_origin']), 1),
                'sched_arr': s['sched_arr'], 'sched_dep': s['sched_dep'], **event, **predictions.get(s['code'], {})}
        if event.get('actual_arr') and s['sched_arr']:
            item['delay_min'] = round((event['actual_arr'] - s['sched_arr']).total_seconds() / 60, 1)
        elif event.get('actual_dep'):
            item['delay_min'] = round((event['actual_dep'] - s['sched_dep']).total_seconds() / 60, 1)
        stops.append(item)
    position_keys = ['latitude', 'longitude', 'km_from_origin', 'speed_kmh', 'accuracy_m', 'source', 'warnings',
                     'last_update', 'seconds_since_update', 'rejected_outliers', 'ignored_backwards', 'unscheduled_halt', 'between', 'progress_pct']
    return serial({'train_number': train['train_number'], 'train_name': train['name'],
                   'journey_start_date': journey_date, 'data_mode': 'REPLAY', 'sim_time': now, 'sim_speed': replay.clock()['speed'],
                   'position': {key: state[key] for key in position_keys},
                   'current_delay_min': round(state['current_delay_min'], 1), 'delay_reference_station': state['delay_reference_station'],
                   'stops': stops, 'route': train['route'], 'active_conditions': conditions, 'generated_at': now,
                   'at_station': state['at_station'], 'passing_times': state['passing_times']})


@app.get('/')
@app.get('/index.html')
def index():
    # Let Flask serve the homepage directly; Vercel must not rewrite it to an
    # asset path that its generated function routing may not expose.
    return app.send_static_file('index.html')



def data_mode():
    mode = request.args.get('mode', os.environ.get('RAILETA_DATA_MODE', 'DEMO')).upper()
    if mode not in ('LIVE', 'DEMO'):
        raise ValueError('mode must be LIVE or DEMO')
    return mode


@app.get('/api/config')
def configuration():
    return jsonify(data_mode=data_mode(), live_configured=bool(os.environ.get('RAILRADAR_API_KEY', '').strip()),
                   live_poll_seconds=live_provider.poll_seconds(), provider='RailRadar')


def live_only_error(message='This control is available only in DEMO mode.'):
    return jsonify(error=message, code='DEMO_ONLY', data_mode='LIVE'), 409


@app.get('/api/trains')
def trains():
    if data_mode() == 'LIVE':
        return jsonify([])  # Type any five-digit train; do not fetch a bulk list.
    return jsonify([{key: t[key] for key in ('train_number', 'name', 'origin', 'destination')} for t in TRAINS.values()])


@app.get('/api/train/<number>/live')
def live(number):
    if data_mode() == 'LIVE':
        return jsonify(live_provider.get_live(number, request.args.get('date') or None))
    train = get_train(number)
    return jsonify(live_payload(train)) if train else unknown_train()


@app.get('/api/train/<number>/eta')
def eta(number):
    if data_mode() == 'LIVE':
        payload = live_provider.get_live(number, request.args.get('date') or None)
        keys = ('code', 'name', 'sched_arr', 'baseline_eta', 'predicted_eta', 'predicted_delay_min', 'eta_source')
        return jsonify([{**{key: stop[key] for key in keys}, 'data_mode': 'LIVE',
                         'observed_at': payload['observed_at'], 'stale': payload['stale']}
                        for stop in payload['stops'] if stop['status'] == 'UPCOMING'])
    train = get_train(number)
    if not train:
        return unknown_train()
    keys = ('code', 'name', 'sched_arr', 'predicted_eta', 'predicted_delay_min', 'eta_low', 'eta_high')
    return jsonify([{key: stop[key] for key in keys} for stop in live_payload(train)['stops'] if stop['status'] == 'UPCOMING' and 'predicted_eta' in stop])


def json_body():
    body = request.get_json()
    if not isinstance(body, dict):
        raise ValueError('Request body must be a JSON object')
    return body


@app.route('/api/sim', methods=['GET', 'POST'])
def simulator():
    if data_mode() == 'LIVE':
        return live_only_error()
    body = json_body() if request.method == 'POST' else {}
    number = str(body.get('train_number', request.args.get('train_number', next(iter(TRAINS)))))
    train = get_train(number)
    if not train:
        return unknown_train()
    if request.method == 'POST':
        if 'restart' in body and not isinstance(body['restart'], bool):
            raise ValueError('restart must be true or false')
        result = replay.configure(body, train)
        if body.get('restart'):
            g.manual.clear()
    else:
        result = replay.clock_payload(train)
    return jsonify(result)



@app.route('/api/conditions', methods=['GET', 'POST', 'DELETE'])
def conditions_api():
    if data_mode() == 'LIVE':
        return jsonify([]) if request.method == 'GET' else live_only_error()
    body = json_body() if request.method == 'POST' else {}
    number = str(body.get('train_number', request.args.get('train_number', next(iter(TRAINS)))))
    train = get_train(number)
    if not train:
        return unknown_train()
    now = replay.sim_now(train)
    if request.method == 'DELETE':
        g.manual.clear()
    elif request.method == 'POST':
        kind = body.get('type')
        if kind not in ('CONGESTION', 'SPEED_RESTRICTION', 'WEATHER_FOG'):
            raise ValueError('type must be CONGESTION, SPEED_RESTRICTION or WEATHER_FOG')
        state = build_state(train, replay.pings_until(train, now), now, STATS, replay.start_time(train).date())
        stops = replay.schedule(train)
        index = state['current_index']
        if index >= len(stops)-1:
            raise ValueError('Journey complete. Restart or jump back to add a condition.')
        # "Ahead" means the current section when between stops, or the next
        # departure section while at a station. Fog spans the remaining route.
        from_code = body.get('from', stops[index]['code'])
        to_code = body.get('to', stops[-1]['code'] if kind == 'WEATHER_FOG' else stops[index+1]['code'])
        indices = {s['code']: i for i,s in enumerate(stops)}
        if from_code not in indices or to_code not in indices or indices[from_code] >= indices[to_code]:
            raise ValueError('Condition endpoints must be STOPs in travel order')
        def numeric(key, default, low, high):
            value = body.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{key} must be a number between {low} and {high}')
            return value
        condition = {'id':'M-'+uuid.uuid4().hex[:8], 'train_numbers':[number], 'type':kind,
                     'from':from_code, 'to':to_code, 'reported_at_min':(now-replay.start_time(train)).total_seconds()/60,
                     'cleared_at_min':None, 'manual':True, 'note':'Manual what-if report (DEMO)'}
        if kind == 'CONGESTION':
            condition['extra_min'] = numeric('extra_min', 15, 1, 120)
        elif kind == 'SPEED_RESTRICTION':
            condition['restricted_km'] = numeric('restricted_km', 25, 1, 100)
            condition['speed_kmh'] = numeric('speed_kmh', 30, 5, 120)
        else:
            condition['factor'] = numeric('factor', 1.15, 1.01, 2)
        # Repeating a report updates that condition instead of compounding it.
        g.manual = [c for c in g.manual if not (c['train_numbers']==[number] and c['type']==kind and c['from']==from_code and c['to']==to_code)]
        if len(g.manual) >= 8:
            raise ValueError('Up to 8 manual reports are supported. Clear your reports first.')
        g.manual.append(condition)
    return jsonify(active_conditions(train, now))


@app.get('/api/station/<code>/arrivals')
def station_arrivals(code):
    if data_mode() == 'LIVE':
        number = request.args.get('train_number')
        if not number:
            return jsonify(error='Choose train_number for a live station lookup; bulk polling is disabled.', code='TRAIN_REQUIRED'), 400
        payload = live_provider.get_live(number, request.args.get('date') or None)
        return jsonify([{**stop, 'train_number': number, 'data_mode': 'LIVE', 'stale': payload['stale']}
                        for stop in payload['stops'] if stop['code'] == code.upper() and stop['status'] == 'UPCOMING'])
    code = code.upper()
    if code not in STATIONS:
        return jsonify(error='Unknown station code', available=list(STATIONS)), 404
    arrivals = []
    for train in TRAINS.values():
        for stop in live_payload(train)['stops']:
            if stop['code'] == code and stop['status'] == 'UPCOMING' and 'predicted_eta' in stop:
                arrivals.append({'train_number':train['train_number'], 'train_name':train['name'], **stop})
    return jsonify(sorted(arrivals, key=lambda a:a['predicted_eta']))


@app.get('/api/evaluation')
def evaluation():
    if data_mode() == 'LIVE':
        return live_only_error('The backtest uses simulated data and is available only in DEMO mode.')
    if not (ROOT / 'data' / 'evaluation.json').exists():
        return jsonify(error='Run python evaluate.py to generate the backtest results'), 503
    return jsonify(read_json('evaluation.json'))


@app.errorhandler(Exception)
def handle_error(error):
    if isinstance(error, live_provider.ProviderError):
        response = jsonify(error=str(error), code=error.code, data_mode='LIVE', retry_after=error.retry_after)
        if error.retry_after:
            response.headers['Retry-After'] = str(error.retry_after)
        return response, error.status
    if isinstance(error, HTTPException):
        return jsonify(error=error.description), error.code
    if isinstance(error, (ValueError, TypeError)):
        return jsonify(error=str(error)), 400
    # Exception messages may contain third-party data: log no raw exception.
    app.logger.error('Request failed (%s)', type(error).__name__)
    return jsonify(error='Unable to complete request. Check the local server log.'), 500


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
