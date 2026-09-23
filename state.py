"""Derive railway events and a trustworthy position using only observed pings."""
from datetime import datetime
from geo import km_to_latlon, snap_to_route
from predictor import expected_run, minutes
from replay import schedule


def build_state(train, pings, now, stats, journey_date):
    stops = schedule(train, journey_date)
    events = {s['code']: {} for s in stops}
    passing, valid = {}, []
    best_km, rejected, backwards = 0.0, 0, 0
    for ping in sorted(pings, key=lambda p: p['observed_at']):
        observed = datetime.fromisoformat(ping['observed_at'])
        if observed > now:
            continue
        snapped = snap_to_route(ping['latitude'], ping['longitude'], train['route'])
        if snapped['off_route_km'] > 2:
            rejected += 1
            continue
        if snapped['km'] < best_km - .3:
            backwards += 1
            continue
        best_km = max(best_km, snapped['km'])
        item = {**ping, 'km': best_km, 'time': observed}
        valid.append(item)
        for s in stops:
            event = events[s['code']]
            if best_km >= s['km'] - .5 and s['sched_arr']:
                event.setdefault('actual_arr', observed)
            if best_km >= s['km'] + .5 and s['sched_dep']:
                event.setdefault('actual_dep', observed)
        for point in train['route']:
            if point['type'] == 'PASS' and best_km >= point['km']:
                passing.setdefault(point['code'], observed)
    current = 0
    at_station = 'actual_dep' not in events[stops[0]['code']]
    for i, s in enumerate(stops[1:], 1):
        if 'actual_arr' in events[s['code']]:
            current = i
            at_station = 'actual_dep' not in events[s['code']]
    reference = stops[current]
    event = events[reference['code']]
    if at_station and reference['sched_arr'] and event.get('actual_arr'):
        delay = minutes(event['actual_arr'] - reference['sched_arr'])
    elif event.get('actual_dep'):
        delay = minutes(event['actual_dep'] - reference['sched_dep'])
    else:
        delay = max(0, minutes(now - reference['sched_dep']))
    last = valid[-1] if valid else None
    age = max(0, (now - last['time']).total_seconds()) if last else max(0, (now - stops[0]['sched_dep']).total_seconds())
    source = 'MEASURED' if last and age <= 120 else 'ESTIMATED'
    km = best_km
    speed = last['speed_kmh'] if last else 0
    warnings = ['SIGNAL_LOST'] if age > 600 else []
    if not last:
        warnings.append('NO_GPS_FIX')
    if source == 'ESTIMATED' and last and not at_station and current < len(stops) - 1:
        a, b = stops[current:current + 2]
        run, _, _, _ = expected_run(train, a, b, delay, [], stats)
        km = max(best_km, min(b['km'] - .5, best_km + age / 60 * (b['km'] - a['km']) / run))
    lat, lon = km_to_latlon(km, train['route'])
    halt = not at_station and len(valid) >= 2 and all(p['speed_kmh'] < 3 for p in valid[-2:])
    return {'journey_start_date': journey_date, 'events': events, 'passing_times': passing,
            'current_index': current, 'at_station': at_station, 'current_delay_min': delay,
            'delay_reference_station': reference['code'], 'latitude': lat, 'longitude': lon,
            'km_from_origin': km, 'speed_kmh': speed, 'accuracy_m': last['accuracy_m'] if last else None,
            'source': source, 'warnings': warnings, 'last_update': last['time'] if last else None,
            'seconds_since_update': age, 'rejected_outliers': rejected, 'ignored_backwards': backwards,
            'unscheduled_halt': halt, 'between': [reference['code'], stops[min(current + 1, len(stops) - 1)]['code']],
            'progress_pct': km / stops[-1]['km'] * 100}
