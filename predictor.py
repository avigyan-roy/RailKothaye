"""Explainable conditional medians, shared by live service and held-out backtest."""
import csv
import math
from datetime import datetime, timedelta
from statistics import median
from replay import DATA, schedule


def minutes(delta):
    return delta.total_seconds() / 60


def bucket(delay):
    return 'ON_TIME' if delay < 10 else 'LATE' if delay <= 60 else 'VERY_LATE'


def quantile(values, probability):
    values = sorted(values)
    index = (len(values) - 1) * probability
    low = math.floor(index)
    fraction = index - low
    return values[low] * (1 - fraction) + values[min(low + 1, len(values) - 1)] * fraction


def summary(values):
    return median(values), quantile(values, .1), quantile(values, .9)


def load_history():
    with (DATA / 'history.csv').open(encoding='utf-8', newline='') as handle:
        return list(csv.DictReader(handle))


def build_stats(rows, trains):
    stats = {'sections': {}, 'dwells': {}}
    journeys = {}
    for row in rows:
        if row['train_number'] not in trains:
            continue
        journeys.setdefault((row['train_number'], row['journey_date']), []).append(row)
    for (number, _), journey in journeys.items():
        route = {p['code']: p for p in trains[number]['route']}
        journey.sort(key=lambda r: int(r['seq']))
        for i, row in enumerate(journey):
            if row['actual_arr'] and row['actual_dep']:
                dwell = minutes(datetime.fromisoformat(row['actual_dep']) - datetime.fromisoformat(row['actual_arr']))
                stats['dwells'].setdefault((number, row['station_code']), []).append(dwell)
            if not i:
                continue
            previous = journey[i - 1]
            runtime = minutes(datetime.fromisoformat(row['actual_arr']) - datetime.fromisoformat(previous['actual_dep']))
            length = route[row['station_code']]['km'] - route[previous['station_code']]['km']
            sr_km, sr_speed = float(row['sec_sr_km'] or 0), float(row['sec_sr_speed'] or 0)
            # Invert observed = base + sr_km * (60/sr_speed - base/length).
            if sr_km and sr_speed:
                runtime = (runtime - sr_km * 60 / sr_speed) / (1 - sr_km / length)
            sample = {'run': max(1, runtime), 'congestion': int(row['sec_congestion'] or 0),
                      'fog': int(row['sec_fog'] or 0), 'sr_km': sr_km, 'sr_speed': sr_speed,
                      'bucket': bucket(float(previous['delay_at_dep_min']))}
            stats['sections'].setdefault((number, previous['station_code'], row['station_code']), []).append(sample)
    return stats


def section_conditions(train, a, b, conditions):
    positions = {p['code']: p['km'] for p in train['route']}
    return [c for c in conditions if train['train_number'] in c.get('train_numbers', [])
            and positions.get(c['from'], math.inf) <= a['km'] and positions.get(c['to'], -1) >= b['km']]


def expected_run(train, a, b, delay, conditions, stats):
    active = section_conditions(train, a, b, conditions)
    congestion = next((c for c in active if c['type'] == 'CONGESTION'), None)
    fog = next((c for c in active if c['type'] == 'WEATHER_FOG'), None)
    flags = {'congestion': int(congestion is not None), 'fog': int(fog is not None), 'bucket': bucket(delay)}
    samples = stats['sections'].get((train['train_number'], a['code'], b['code']), [])
    kept = ['congestion', 'fog', 'bucket']
    chosen = []
    for keys in (kept, ['congestion', 'fog'], ['congestion'], []):
        chosen = [s['run'] for s in samples if all(s[key] == flags[key] for key in keys)]
        kept = keys
        if len(chosen) >= 5 or not keys:
            break
    if not chosen:
        # A JSON-only train addition remains usable before it has history.
        chosen = [minutes(b['sched_arr'] - a['sched_dep'])]
    base, low, high = summary(chosen)
    reasons = []
    length = b['km'] - a['km']
    label = a['code'] + '–' + b['code']
    for condition in active:
        if condition['type'] == 'SPEED_RESTRICTION':
            restricted = min(length, condition.get('restricted_km', 20))
            penalty = max(0, restricted * (60 / condition.get('speed_kmh', 30) - base / length))
            base, low, high = base + penalty, low + penalty, high + penalty
            reasons.append('Speed restriction ' + label)
    if congestion:
        if 'congestion' not in kept:
            extra = congestion.get('extra_min', 10)
            base, low, high = base + extra, low + extra, high + extra
        reasons.append('Congestion ' + label)
    if fog:
        if 'fog' not in kept:
            factor = fog.get('factor', 1.15)
            base, low, high = base * factor, low * factor, high * factor
        reasons.append('Fog ' + label)
    return base, low, high, reasons


def expected_dwell(train, stop, stats):
    values = stats['dwells'].get((train['train_number'], stop['code']), [])
    if not values:
        values = [max(1, minutes(stop['sched_dep'] - stop['sched_arr'])) if stop['sched_arr'] and stop['sched_dep'] else 1]
    return summary(values)


def predict_journey(train, state, sim_now, active_conditions, stats):
    stops = schedule(train, state['journey_start_date'])
    current = state['current_index']
    if current >= len(stops) - 1:
        return []
    predicted, reasons = [], []
    t, delay = sim_now, state['current_delay_min']
    variance_low = variance_high = 0.0
    if state.get('unscheduled_halt'):
        reasons.append('Unscheduled halt detected')
    if state.get('source') == 'ESTIMATED':
        reasons.append('Position estimated from last GPS')
    if state['at_station']:
        stop = stops[current]
        dwell, lo, hi = expected_dwell(train, stop, stats)
        arrival = state['events'].get(stop['code'], {}).get('actual_arr', sim_now)
        remaining = max(1, dwell - max(0, minutes(sim_now - arrival)))
        t = max(sim_now + timedelta(minutes=remaining), stop['sched_dep'])
        # Origin departure is governed by its timetable; observed late waiting still advances t.
        variance_low += (dwell - lo) ** 2
        variance_high += (hi - dwell) ** 2
        delay = minutes(t - stop['sched_dep'])
    for i in range(current, len(stops) - 1):
        a, b = stops[i], stops[i + 1]
        run, low, high, adjustments = expected_run(train, a, b, delay, active_conditions, stats)
        reasons = list(dict.fromkeys(reasons + adjustments))
        fraction = 1.0
        if i == current and not state['at_station']:
            left = max(0, b['km'] - state['km_from_origin'])
            fraction = min(1, left / (b['km'] - a['km']))
            remaining = fraction * run
            if left < 30 and state['speed_kmh'] > 20:
                remaining = .5 * remaining + .5 * left / state['speed_kmh'] * 60
        else:
            remaining = run
        t += timedelta(minutes=remaining)
        variance_low += ((run - low) * fraction) ** 2
        variance_high += ((high - run) * fraction) ** 2
        width = 1.25 if state.get('source') == 'ESTIMATED' else 1
        eta_low = t - timedelta(minutes=math.sqrt(variance_low) * width)
        eta_high = t + timedelta(minutes=math.sqrt(variance_high) * width)
        span = minutes(eta_high - eta_low)
        predicted_delay = minutes(t - b['sched_arr'])
        trend = ('Recovering delay (timetable slack)' if predicted_delay < state['current_delay_min'] - 2
                 else 'Losing time' if predicted_delay > state['current_delay_min'] + 2 else 'In line with current delay')
        item = {'code': b['code'], 'name': b.get('name', b['code']), 'sched_arr': b['sched_arr'],
                'baseline_eta': b['sched_arr'] + timedelta(minutes=state['current_delay_min']),
                'predicted_eta': t, 'predicted_delay_min': round(predicted_delay, 1),
                'eta_low': eta_low, 'eta_high': eta_high,
                'confidence': 'HIGH' if span <= 10 else 'MEDIUM' if span <= 25 else 'LOW',
                'reasons': reasons + [trend]}
        predicted.append(item)
        if b['sched_dep']:
            dwell, lo, hi = expected_dwell(train, b, stats)
            departure = max(t + timedelta(minutes=dwell), b['sched_dep'])
            item['predicted_dep'] = departure
            # Accumulate independent section/dwell spreads, not whole-journey quantiles.
            variance_low += (dwell - lo) ** 2
            variance_high += (hi - dwell) ** 2
            t = departure
        delay = predicted_delay
    return predicted
