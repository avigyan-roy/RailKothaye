"""Live-only RailETA Flask server. Set RAILRADAR_API_KEY, then run app.py."""
import os
from pathlib import Path
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
import live_provider

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / 'static'))
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 16384


@app.before_request
def require_live_mode():
    # Old clients may still send mode=LIVE; no setting can enable simulation.
    if request.path.startswith('/api/') and request.args.get('mode', 'LIVE').upper() != 'LIVE':
        return jsonify(error='Only live train data is supported.', code='LIVE_ONLY', data_mode='LIVE'), 400


@app.after_request
def no_api_cache(response):
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
@app.get('/index.html')
def index():
    return app.send_static_file('index.html')


@app.get('/api/config')
def configuration():
    return jsonify(data_mode='LIVE', live_configured=bool(os.environ.get('RAILRADAR_API_KEY', '').strip()),
                   live_poll_seconds=live_provider.poll_seconds(), provider='RailRadar')


@app.get('/api/trains')
def trains():
    return jsonify([])  # Type a five-digit train number; no bulk provider requests.


@app.get('/api/train/<number>/live')
def live(number):
    return jsonify(live_provider.get_live(number, request.args.get('date') or None))


@app.get('/api/train/<number>/eta')
def eta(number):
    payload = live_provider.get_live(number, request.args.get('date') or None)
    keys = ('code', 'name', 'sched_arr', 'baseline_eta', 'predicted_eta', 'predicted_delay_min', 'eta_source')
    return jsonify([{**{key: stop[key] for key in keys}, 'data_mode': 'LIVE',
                     'observed_at': payload['observed_at'], 'stale': payload['stale']}
                    for stop in payload['stops'] if stop['status'] == 'UPCOMING'])


@app.get('/api/station/<code>/arrivals')
def station_arrivals(code):
    number = request.args.get('train_number')
    if not number:
        return jsonify(error='Choose train_number for a live station lookup; bulk polling is disabled.', code='TRAIN_REQUIRED'), 400
    payload = live_provider.get_live(number, request.args.get('date') or None)
    return jsonify([{**stop, 'train_number': number, 'data_mode': 'LIVE', 'stale': payload['stale']}
                    for stop in payload['stops'] if stop['code'] == code.upper() and stop['status'] == 'UPCOMING'])


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
