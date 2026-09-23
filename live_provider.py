"""RailRadar adapter. Live snapshots never enter the synthetic replay predictor."""
import hashlib
import json
import math
import os
import re
import socket
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

IST = timezone(timedelta(hours=5, minutes=30))
MAX_BYTES = 2_000_000
STALE_SECONDS = 300
_CACHE = OrderedDict()
_LOCK = Lock()
_COOLDOWNS = {}


class ProviderError(Exception):
    """Only fixed, safe messages leave this boundary; upstream bodies never do."""
    def __init__(self, code, message, status=502, retry_after=None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('Provider redirects are disabled')


_OPENER = build_opener(NoRedirect())


def poll_seconds():
    try:
        return max(30, min(900, int(os.environ.get('LIVE_POLL_SECONDS', '60'))))
    except ValueError:
        return 60


def validate_selection(number, journey_date):
    if not re.fullmatch(r'[0-9]{5}', number):
        raise ProviderError('INVALID_TRAIN', 'Enter a five-digit train number.', 400)
    if journey_date:
        try:
            if date.fromisoformat(journey_date).isoformat() != journey_date:
                raise ValueError
        except (ValueError, TypeError):
            raise ProviderError('INVALID_DATE', 'Journey start date must be YYYY-MM-DD.', 400) from None


def retry_seconds(value):
    try:
        return max(1, min(86400, math.ceil(float(value))))
    except (ValueError, TypeError, OverflowError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            return max(1, min(86400, math.ceil(seconds)))
        except (ValueError, TypeError, OverflowError):
            return 60


def _fetch(number, journey_date, key):
    base = os.environ.get('RAILRADAR_BASE_URL', 'https://api.railradar.in/v1').rstrip('/')
    parts = urlsplit(base)
    if parts.scheme != 'https' or parts.netloc != 'api.railradar.in' or parts.path != '/v1' or parts.query or parts.fragment:
        raise ProviderError('PROVIDER_CONFIG', 'RAILRADAR_BASE_URL must be https://api.railradar.in/v1.', 503)
    parameters = {'includeCoordinates': 'true'}
    if journey_date:
        parameters['date'] = journey_date
    request = Request(f'{base}/trains/{number}/live?{urlencode(parameters)}',
                      headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    try:
        with _OPENER.open(request, timeout=8) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        status = error.code
        retry = retry_seconds(error.headers.get('Retry-After')) if status in (429, 503) else None
        error.close()
        if status in (401, 403):
            raise ProviderError('PROVIDER_AUTH_FAILED', 'RailRadar rejected the API key or its access permissions. Check the Vercel configuration.', 503) from None
        if status == 429:
            raise ProviderError('PROVIDER_RATE_LIMIT', 'RailRadar request limit reached. Please wait before refreshing.', 429, retry) from None
        if status == 404:
            raise ProviderError('LIVE_NOT_FOUND', 'RailRadar has no live record for this train and journey date.', 404) from None
        if status == 400:
            raise ProviderError('PROVIDER_REJECTED_SELECTION', 'RailRadar could not accept this train/date selection.', 400) from None
        raise ProviderError('PROVIDER_UNAVAILABLE', 'RailRadar is temporarily unavailable.', 503, retry or 60) from None
    except (TimeoutError, socket.timeout):
        raise ProviderError('PROVIDER_TIMEOUT', 'RailRadar took too long to respond. Please retry later.', 504, 60) from None
    except (URLError, OSError, ValueError):
        raise ProviderError('PROVIDER_CONNECTION', 'Unable to connect to RailRadar.', 503, 60) from None
    if len(raw) > MAX_BYTES:
        raise ProviderError('PROVIDER_RESPONSE_TOO_LARGE', 'RailRadar returned an oversized response.')
    try:
        # Defense in depth if an upstream response unexpectedly echoes credentials.
        body = json.loads(raw.decode('utf-8').replace(key, '[REDACTED]'))
    except (UnicodeError, ValueError):
        raise ProviderError('PROVIDER_BAD_RESPONSE', 'RailRadar returned an unreadable response.') from None
    if not isinstance(body, dict) or body.get('success') is not True or not isinstance(body.get('data'), dict):
        raise ProviderError('PROVIDER_BAD_RESPONSE', 'RailRadar did not return a valid live snapshot.')
    return body['data']


def number_value(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if low is not None and value < low or high is not None and value > high:
        return None
    return value


def text(value, default=''):
    return value[:200] if isinstance(value, str) else default


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(IST) if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError, OverflowError):
        return None


def iso(value):
    return value.isoformat(timespec='seconds') if value else None


def coordinates(row):
    lat, lon = number_value(row.get('lat'), -90, 90), number_value(row.get('lng'), -180, 180)
    return (lat, lon) if lat is not None and lon is not None else (None, None)


def normalize(raw, number, requested_date, fetched_at, now=None):
    """Map only observed/documented fields. 'actualArrival' at UPCOMING stops
    is a provider projection in the authenticated payload, never an event.
    Missing data remains null; response-generation time is not observation time.
    """
    now = now or datetime.now(IST)
    train = raw.get('train') if isinstance(raw.get('train'), dict) else {}
    returned_number = str(raw.get('trainNumber') or train.get('number') or '')
    if returned_number != number:
        raise ProviderError('PROVIDER_TRAIN_MISMATCH', 'RailRadar returned a different train than requested.')
    start = raw.get('startDate')
    try:
        if date.fromisoformat(start).isoformat() != start:
            raise ValueError
    except (ValueError, TypeError):
        raise ProviderError('PROVIDER_BAD_DATE', 'RailRadar did not identify the journey start date.') from None
    if requested_date and start != requested_date:
        raise ProviderError('PROVIDER_DATE_MISMATCH', 'RailRadar returned a different journey date. Choose that departure date explicitly.')
    rows = raw.get('route')
    if not isinstance(rows, list) or not rows or len(rows) > 2000 or not all(isinstance(r, dict) for r in rows):
        raise ProviderError('PROVIDER_BAD_ROUTE', 'RailRadar did not supply a usable timetable.')
    observed = timestamp(raw.get('lastUpdatedAt'))
    warnings = []
    if not observed:
        warnings.append('Observation time unavailable')
    elif observed > now + timedelta(seconds=60):
        warnings.append('Provider observation timestamp is in the future')
        observed = None
    elif observed.date() < date.fromisoformat(start):
        warnings.append('Observation predates this journey')
        observed = None
    age = max(0, (now - observed).total_seconds()) if observed else None
    status = text(raw.get('status')).lower().replace('-', '_').replace(' ', '_')
    journey_status = {'running': 'RUNNING', 'not_started': 'NOT_STARTED', 'scheduled': 'NOT_STARTED',
                      'completed': 'COMPLETED', 'arrived': 'COMPLETED', 'cancelled': 'CANCELLED',
                      'canceled': 'CANCELLED', 'partially_cancelled': 'PARTIALLY_CANCELLED',
                      'diverted': 'DIVERTED', 'rescheduled': 'RESCHEDULED'}.get(status, 'UNAVAILABLE')
    if raw.get('isLive') is not True and journey_status == 'RUNNING':
        warnings.append('Provider has not confirmed live tracking')
    stale = observed is None or age > STALE_SECONDS or (raw.get('isLive') is not True and journey_status == 'RUNNING')
    freshness = 'UNAVAILABLE' if observed is None else 'STALE' if stale else 'FRESH'
    delay = number_value(raw.get('delayMinutes'), -1440, 10080)
    stops, route = [], []
    last_distance = -1
    for row in rows:
        code = text(row.get('stationCode'))
        km = number_value(row.get('distance'), 0, 20000)
        if not code:
            raise ProviderError('PROVIDER_BAD_ROUTE', 'RailRadar returned a station without its code.')
        if km is not None and km < last_distance:
            # Do not snap to a route with reversed/malformed distance values.
            warnings.append('Route distances are inconsistent; map progress unavailable')
            km = None
        elif km is not None:
            last_distance = km
        lat, lon = coordinates(row)
        point = {'code': code, 'name': text(row.get('stationName'), code), 'type': 'STOP' if row.get('isHalt') is True else 'PASS',
                 'km': km, 'lat': lat, 'lon': lon}
        route.append(point)
        if point['type'] != 'STOP':
            continue
        sa, sd = timestamp(row.get('scheduledArrival')), timestamp(row.get('scheduledDeparture'))
        arr, dep = timestamp(row.get('actualArrival')), timestamp(row.get('actualDeparture'))
        row_status = text(row.get('status')).lower()
        normalized_status = {'departed': 'DEPARTED', 'upcoming': 'UPCOMING', 'arrived': 'AT_STATION',
                             'at_station': 'AT_STATION', 'halted': 'AT_STATION', 'cancelled': 'CANCELLED',
                             'skipped': 'SKIPPED'}.get(row_status, 'UNKNOWN')
        # Reported statuses never create missing arrival/departure timestamps.
        actual_arr = arr if normalized_status in ('DEPARTED', 'AT_STATION') and observed and arr and arr <= observed else None
        actual_dep = dep if normalized_status == 'DEPARTED' and observed and dep and dep <= observed else None
        eta = None
        eta_basis = None
        if normalized_status == 'UPCOMING' and journey_status not in ('CANCELLED', 'COMPLETED', 'UNAVAILABLE'):
            # Verified RailRadar behavior: future times in 'actualArrival' on
            # upcoming rows represent its projected arrival, not a past event.
            if arr and observed and arr > observed:
                eta, eta_basis = arr, 'RailRadar projection for an upcoming stop'
        if journey_status == 'COMPLETED' and row is rows[-1] and actual_arr:
            normalized_status = 'ARRIVED'
        baseline = sa + timedelta(minutes=delay) if sa and delay is not None and normalized_status == 'UPCOMING' and journey_status not in ('CANCELLED', 'COMPLETED', 'UNAVAILABLE') else None
        stops.append({'seq': len(stops) + 1, **point, 'status': normalized_status,
                      'event_source': 'PROVIDER_REPORTED', 'km_from_origin': km, 'km_to_go': None,
                      'sched_arr': iso(sa), 'sched_dep': iso(sd), 'actual_arr': iso(actual_arr), 'actual_dep': iso(actual_dep),
                      'baseline_eta': iso(baseline), 'predicted_eta': iso(eta), 'eta_source': 'RailRadar' if eta else None,
                      'eta_basis': eta_basis, 'predicted_delay_min': round((eta - sa).total_seconds() / 60, 1) if eta and sa else None,
                      'delay_min': round((actual_arr - sa).total_seconds() / 60, 1) if actual_arr and sa else None,
                      'eta_low': None, 'eta_high': None, 'confidence': None,
                      'reasons': [eta_basis] if eta_basis else ['Provider ETA unavailable']})
    if not stops:
        raise ProviderError('PROVIDER_BAD_ROUTE', 'RailRadar did not return scheduled halts.')
    loc = raw.get('currentLocation') if isinstance(raw.get('currentLocation'), dict) else {}
    coords = loc.get('coordinates') if isinstance(loc.get('coordinates'), dict) else {}
    lat, lon = coordinates(coords)
    source, basis = 'UNAVAILABLE', 'Provider location unavailable'
    km = number_value(loc.get('distanceFromOriginKm'), 0, 20000)
    if lat is not None:
        source, basis = 'PROVIDER_REPORTED', 'Coordinates reported by RailRadar; GPS provenance unverified'
    else:
        # No fabricated interpolation or fake GPS pings: at most mark its named
        # station as an explicitly approximate location, with no progress claim.
        point = next((p for p in route if p['code'] == loc.get('stationCode') and p['lat'] is not None), None)
        if point:
            lat, lon = point['lat'], point['lon']
            source, basis = 'ESTIMATED', 'Approximate location at the provider-reported station'
    total = number_value(train.get('distance'), 0, 20000)
    if total is None:
        total = route[-1]['km']
    if km is not None and (total is None or km > total or any('distances' in w for w in warnings)):
        km = None
    for stop in stops:
        if km is not None and stop['km'] is not None:
            stop['km_to_go'] = round(max(0, stop['km'] - km), 1)
    previous = raw.get('previousHalt') if isinstance(raw.get('previousHalt'), dict) else {}
    following = raw.get('nextHalt') if isinstance(raw.get('nextHalt'), dict) else {}
    position = {'latitude': lat, 'longitude': lon, 'km_from_origin': km,
                'speed_kmh': number_value(loc.get('speedKmh'), 0, 250), 'accuracy_m': None,
                'source': source, 'basis': basis, 'warnings': warnings, 'last_update': iso(observed),
                'seconds_since_update': round(age, 1) if age is not None else None,
                'rejected_outliers': None, 'unscheduled_halt': False,
                'between': [text(previous.get('stationCode')), text(following.get('stationCode'))],
                'station_name': text(loc.get('stationName') or loc.get('stationCode'), 'Location unavailable'),
                'progress_pct': round(km / total * 100, 1) if km is not None and total else None}
    return {'train_number': number, 'train_name': text(raw.get('trainName') or train.get('name'), number),
            'journey_start_date': start, 'data_mode': 'LIVE', 'provider': 'RailRadar',
            'journey_status': journey_status, 'freshness': freshness, 'stale': stale,
            'stale_after_seconds': STALE_SECONDS, 'observed_at': iso(observed), 'fetched_at': iso(fetched_at),
            'generated_at': iso(now), 'sim_time': iso(now), 'sim_speed': 1, 'poll_seconds': poll_seconds(),
            'current_delay_min': delay, 'delay_reference_station': text(loc.get('stationCode')),
            'total_distance_km': total, 'position': position, 'stops': stops, 'route': route,
            'active_conditions': [], 'at_station': text(loc.get('status')) in ('arrived', 'halted', 'at_station'),
            'prediction_model': 'PROVIDER_ONLY', 'warnings': list(dict.fromkeys(warnings)), 'passing_times': {}}


def get_live(number, journey_date=None):
    validate_selection(number, journey_date)
    key = os.environ.get('RAILRADAR_API_KEY', '').strip()
    if not key:
        raise ProviderError('LIVE_NOT_CONFIGURED', 'Live data is not configured. Set RAILRADAR_API_KEY in Vercel and redeploy.', 503)
    credential_id = hashlib.sha256(key.encode()).hexdigest()
    cache_key = (credential_id, number, journey_date)
    # This lock coalesces calls only within one Python worker. Vercel workers
    # do not share this cache; production global quotas need shared controls.
    with _LOCK:
        now_mono = time.monotonic()
        cooldown = _COOLDOWNS.get(credential_id)
        remaining = cooldown[0] - now_mono if cooldown else 0
        if remaining > 0:
            previous = cooldown[1]
            raise ProviderError(previous.code, str(previous), previous.status, math.ceil(remaining))
        entry = _CACHE.get(cache_key)
        cached = bool(entry and now_mono - entry[0] < poll_seconds())
        if cached:
            _, raw, fetched_at = entry
        else:
            try:
                raw = _fetch(number, journey_date, key)
            except ProviderError as error:
                if error.retry_after:
                    if len(_COOLDOWNS) > 128:
                        _COOLDOWNS.clear()
                    _COOLDOWNS[credential_id] = (time.monotonic() + error.retry_after, error)
                raise
            fetched_at = datetime.now(IST)
            # Validate before admitting a response to the cache.
            normalize(raw, number, journey_date, fetched_at)
            _CACHE[cache_key] = (time.monotonic(), raw, fetched_at)
            _CACHE.move_to_end(cache_key)
            while len(_CACHE) > 128:
                _CACHE.popitem(last=False)
        result = normalize(raw, number, journey_date, fetched_at)
        result['cache_hit'] = cached
        return result
