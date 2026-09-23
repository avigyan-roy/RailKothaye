"""A lazy simulation clock: no worker threads or background polling."""
import csv
from contextvars import ContextVar
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DATA = Path(__file__).resolve().parent / 'data'
def new_clock():
    return {'speed': 60, 'start_offset_min': 20.0, 'real_start': time.time(),
            'journey_date': str(datetime.now(IST).date())}


CLOCK = new_clock()
REQUEST_CLOCK = ContextVar('replay_clock', default=None)


def clock():
    return REQUEST_CLOCK.get() or CLOCK
REPLAYS = {}


def schedule(train, journey_date=None):
    journey_date = journey_date or datetime.fromisoformat(clock()['journey_date']).date()
    result = []
    for point in train['route']:
        if point['type'] != 'STOP':
            continue
        stop = dict(point)
        for field in ('arr', 'dep'):
            value = point.get(field)
            stop['sched_' + field] = datetime.fromisoformat(f'{journey_date}T{value}:00+05:30') + timedelta(days=point['day'] - 1) if value else None
        result.append(stop)
    return result


def start_time(train, journey_date=None):
    return schedule(train, journey_date)[0]['sched_dep']


def offset_minutes():
    return clock()['start_offset_min'] + (time.time() - clock()['real_start']) * clock()['speed'] / 60


def sim_now(train):
    return start_time(train) + timedelta(minutes=offset_minutes())


def read_replay(number, train=None):
    if number not in REPLAYS:
        path = DATA / f'replay_{number}.csv'
        if not path.exists() and train is not None:
            REPLAYS[number] = timetable_replay(train)
        else:
            with path.open(encoding='utf-8', newline='') as handle:
                REPLAYS[number] = [{key: float(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    return REPLAYS[number]


def pings_until(train, now):
    # Anchor to the session's journey date, including after simulated midnight.
    start = start_time(train)
    offset = (now - start).total_seconds()
    return [{'train_number': train['train_number'], 'journey_start_date': str(start.date()),
             'latitude': row['latitude'], 'longitude': row['longitude'], 'speed_kmh': row['speed_kmh'],
             'accuracy_m': row['accuracy_m'], 'observed_at': (start + timedelta(seconds=row['t_offset_sec'])).isoformat()}
            for row in read_replay(train['train_number'], train) if row['t_offset_sec'] <= offset]


def configure(settings, train):
    speed = settings.get('speed', clock()['speed'])
    if isinstance(speed, bool) or speed not in (1, 10, 60, 120):
        raise ValueError('Speed must be 1, 10, 60 or 120')
    offset = 20.0 if settings.get('restart') else offset_minutes()
    if settings.get('jump_before'):
        stops = schedule(train)
        target = next((s for s in stops[1:] if s['code'] == settings['jump_before']), None)
        if not target:
            raise ValueError('Choose a non-origin STOP on this train route')
        # Use the recorded trace for useful demo navigation, never for prediction.
        from geo import snap_to_route
        hit = next((r for r in read_replay(train['train_number'], train)
                    if (lambda p: p['off_route_km'] <= 2 and p['km'] >= target['km'] - .5)(snap_to_route(r['latitude'], r['longitude'], train['route']))), None)
        offset = max(0, hit['t_offset_sec'] / 60 - 10) if hit else max(0, (target['sched_arr'] - stops[0]['sched_dep']).total_seconds() / 60 - 10)
    event = settings.get('jump_event')
    if event:
        rows = read_replay(train['train_number'], train)
        if event == 'gps_gap':
            gaps = [(a,b) for a,b in zip(rows,rows[1:]) if b['t_offset_sec']-a['t_offset_sec'] > 120]
            if not gaps:
                raise ValueError('This train has no scripted GPS gap; use the forward demo train')
            # Land just as the last fix becomes stale; the next poll sees ESTIMATED.
            offset = (gaps[0][0]['t_offset_sec'] + 121) / 60
        elif event == 'congestion':
            import json
            conditions = json.loads((DATA / 'conditions.json').read_text(encoding='utf-8'))
            events = [c for c in conditions if train['train_number'] in c['train_numbers']
                      and c['type'] == 'CONGESTION' and c['reported_at_min'] > 0]
            if not events:
                raise ValueError('This train has no later congestion report; use the forward demo train')
            offset = max(0, min(c['reported_at_min'] for c in events)-2)
        else:
            raise ValueError('jump_event must be congestion or gps_gap')
    if settings.get('restart'):
        clock()['journey_date'] = str(datetime.now(IST).date())
    clock().update(speed=speed, start_offset_min=offset, real_start=time.time())
    return clock_payload(train)


def clock_payload(train):
    return {'speed': clock()['speed'], 'start_offset_min': clock()['start_offset_min'],
            'offset_min': round(offset_minutes(), 3), 'sim_time': sim_now(train).isoformat(),
            'train_number': train['train_number']}


def timetable_replay(train):
    """Neutral on-time fallback for additional JSON-defined demo trains."""
    from bisect import bisect_right
    from geo import km_to_latlon
    stops = schedule(train)
    start = stops[0]['sched_dep']
    knots = [(0.0, 0.0)]
    for stop in stops[1:]:
        knots.append(((stop['sched_arr']-start).total_seconds(), stop['km']))
        if stop['sched_dep']:
            knots.append(((stop['sched_dep']-start).total_seconds(), stop['km']))
    rows = []
    times = [t for t, km in knots]
    final = int(times[-1])
    for offset in list(range(0, final, 30)) + [final]:
        index = min(len(knots)-2, max(0, bisect_right(times,offset)-1))
        ta, ka = knots[index]
        tb, kb = knots[index+1]
        fraction = (offset-ta)/(tb-ta)
        lat, lon = km_to_latlon(ka+fraction*(kb-ka), train['route'])
        rows.append({'t_offset_sec':offset,'latitude':lat,'longitude':lon,
                     'speed_kmh':(kb-ka)/(tb-ta)*3600 if offset<final else 0,'accuracy_m':15})
    return rows
