"""Chronological 45/15 backtest. No future actual arrival enters the predictor."""
import json
from collections import defaultdict
from datetime import datetime
from statistics import mean, median
from predictor import build_stats, load_history, predict_journey, minutes
from replay import DATA, schedule


def metrics(errors):
    absolute = [abs(e) for e in errors]
    return {'mae': round(mean(absolute), 3), 'median_absolute_error': round(median(absolute), 3),
            'bias': round(mean(errors), 3), 'within_5_pct': round(100 * sum(e <= 5 for e in absolute) / len(errors), 2),
            'within_10_pct': round(100 * sum(e <= 10 for e in absolute) / len(errors), 2)}


def conditions_from_rows(train, rows):
    conditions = []
    # This is an explicit oracle for CONDITION FLAGS only, not outcomes or hidden noise.
    for previous, row in zip(rows, rows[1:]):
        common = {'train_numbers': [train['train_number']], 'from': previous['station_code'], 'to': row['station_code']}
        if int(row['sec_congestion'] or 0):
            conditions.append({**common, 'type': 'CONGESTION', 'extra_min': 16.5})
        if int(row['sec_fog'] or 0):
            conditions.append({**common, 'type': 'WEATHER_FOG', 'factor': 1.165})
        if float(row['sec_sr_km'] or 0):
            conditions.append({**common, 'type': 'SPEED_RESTRICTION', 'restricted_km': float(row['sec_sr_km']), 'speed_kmh': float(row['sec_sr_speed'])})
    return conditions


def evaluate():
    trains = json.loads((DATA / 'trains.json').read_text(encoding='utf-8'))
    rows = load_history()
    dates = sorted({r['journey_date'] for r in rows})
    if len(dates) != 60:
        raise ValueError('Expected 60 history dates; run generate_data.py first')
    training = [r for r in rows if r['journey_date'] in dates[:45]]
    test = [r for r in rows if r['journey_date'] in dates[45:]]
    stats = build_stats(training, trains)
    journeys = defaultdict(list)
    for row in test:
        journeys[(row['train_number'], row['journey_date'])].append(row)
    errors = []
    for (number, day), journey in journeys.items():
        journey.sort(key=lambda r: int(r['seq']))
        train = trains[number]
        start_date = datetime.fromisoformat(day).date()
        stops = schedule(train, start_date)
        conditions = conditions_from_rows(train, journey)
        for i, origin in enumerate(journey[:-1]):
            now = datetime.fromisoformat(origin['actual_dep'])
            state = {'journey_start_date': start_date, 'current_index': i, 'at_station': False,
                     'current_delay_min': float(origin['delay_at_dep_min']), 'km_from_origin': stops[i]['km'],
                     'speed_kmh': 0, 'source': 'MEASURED', 'unscheduled_halt': False,
                     'events': {r['station_code']: {key: datetime.fromisoformat(r[key]) for key in ('actual_arr','actual_dep') if r[key]} for r in journey[:i+1]}}
            predictions = predict_journey(train, state, now, conditions, stats)
            for j, prediction in enumerate(predictions, i + 1):
                actual = datetime.fromisoformat(journey[j]['actual_arr'])
                errors.append({'horizon': j-i, 'distance': stops[j]['km'] - stops[i]['km'],
                               'baseline': minutes(prediction['baseline_eta'] - actual),
                               'model': minutes(prediction['predicted_eta'] - actual)})
    def group(label, samples):
        baseline = metrics([s['baseline'] for s in samples])
        model = metrics([s['model'] for s in samples])
        return {'label': label, 'count':len(samples), 'baseline': baseline, 'model': model,
                'improvement_pct': round((baseline['mae'] - model['mae']) / baseline['mae'] * 100, 2)}
    result = {'data_mode': 'SIMULATED', 'training_days_per_train':45, 'test_days_per_train':15,
              'train_numbers':list(trains), 'train_date_range':[dates[0],dates[44]], 'test_date_range':[dates[45],dates[-1]],
              'assumption':'Section conditions are reported before the train reaches them; hidden noise and future actual times are never model inputs.',
              'overall':group('Overall',errors),
              'horizons':[group('1 stop ahead',[s for s in errors if s['horizon']==1]),
                          group('2–3 stops ahead',[s for s in errors if 2<=s['horizon']<=3]),
                          group('4+ stops ahead',[s for s in errors if s['horizon']>=4])],
              'distances':[group('<150 km',[s for s in errors if s['distance']<150]),
                           group('150–500 km',[s for s in errors if 150<=s['distance']<=500]),
                           group('>500 km',[s for s in errors if s['distance']>500])]}
    (DATA / 'evaluation.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('DEMO backtest · 45 training / 15 unseen journeys per train · 840 predictions')
    print(f"{'Group':19} {'Method':9} {'MAE':>7} {'Median':>7} {'Bias':>7} {'±5 %':>7} {'±10 %':>7}")
    print('-'*72)
    for row in result['horizons']+result['distances']+[result['overall']]:
        for method in ('baseline','model'):
            m=row[method]
            print(f"{row['label']:19} {method:9} {m['mae']:7.2f} {m['median_absolute_error']:7.2f} {m['bias']:7.2f} {m['within_5_pct']:7.1f} {m['within_10_pct']:7.1f}")
        print(f"  MAE improvement: {row['improvement_pct']:+.1f}%")
    return result


if __name__ == '__main__':
    evaluate()
