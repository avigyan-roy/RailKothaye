"""Reproducible synthetic journeys. Run: python generate_data.py [--date YYYY-MM-DD]."""
import argparse
import bisect
import csv
import json
import math
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import mean

DATA = Path(__file__).resolve().parent / "data"
IST = timezone(timedelta(hours=5, minutes=30))
HISTORY_FIELDS = "journey_date train_number seq station_code sched_arr sched_dep actual_arr actual_dep delay_at_dep_min sec_congestion sec_sr_km sec_sr_speed sec_fog".split()
PING_FIELDS = "t_offset_sec latitude longitude speed_kmh accuracy_m".split()

# These are illustrative rail distances and times, NOT an official timetable.
POINTS = [
    ("HWH", "Howrah Jn", 22.5835, 88.3426, 0, 1, None, "16:50"),
    ("BWN", "Barddhaman Jn", 23.2497, 87.8697, 95),
    ("DGR", "Durgapur", 23.4940, 87.3180, 158),
    ("ASN", "Asansol Jn", 23.6904, 86.9750, 200, 1, "18:48", "18:50"),
    ("DHN", "Dhanbad Jn", 23.7916, 86.4297, 259, 1, "19:43", "19:48"),
    ("KQR", "Koderma Jn", 24.4670, 85.5950, 382),
    ("GAYA", "Gaya Jn", 24.8043, 84.9993, 458, 1, "22:03", "22:06"),
    ("SSM", "Sasaram Jn", 24.9520, 84.0120, 558),
    ("DDU", "Pt. Deen Dayal Upadhyaya Jn", 25.2795, 83.1180, 661, 2, "00:35", "00:45"),
    ("MZP", "Mirzapur", 25.1440, 82.5690, 724),
    ("PRYJ", "Prayagraj Jn", 25.4467, 81.8260, 814, 2, "02:28", "02:30"),
    ("FTP", "Fatehpur", 25.9300, 80.8130, 931),
    ("CNB", "Kanpur Central", 26.4540, 80.3510, 1008, 2, "04:45", "04:50"),
    ("ETW", "Etawah Jn", 26.7780, 79.0230, 1146),
    ("TDL", "Tundla Jn", 27.2070, 78.2350, 1238),
    ("ALJN", "Aligarh Jn", 27.8870, 78.0800, 1316),
    ("GZB", "Ghaziabad", 28.6490, 77.4290, 1422),
    ("NDLS", "New Delhi", 28.6430, 77.2195, 1447, 2, "10:00", None),
]


def iso(value):
    return value.isoformat(timespec="seconds") if value else ""


def scheduled(stop, field, journey_date):
    if not stop.get(field):
        return None
    return datetime.combine(journey_date + timedelta(days=stop["day"] - 1),
                            time.fromisoformat(stop[field]), IST)


def write_csv(filename, fields, rows):
    with (DATA / filename).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {filename}: {len(rows):,} rows")


def seed_files():
    stations = [dict(zip(["code", "name", "lat", "lon"], p[:4])) for p in POINTS]
    route = []
    for p in POINTS:
        point = {"code": p[0], "type": "STOP" if len(p) == 8 else "PASS", "km": p[4]}
        if len(p) == 8:
            point.update(day=p[5], arr=p[6], dep=p[7])
        route.append(point)
    reverse = [{"code": p["code"], "type": p["type"], "km": 1447 - p["km"]} for p in reversed(route)]
    reverse_times = {
        "NDLS": (1, None, "16:55"), "CNB": (1, "22:00", "22:05"),
        "PRYJ": (2, "00:15", "00:17"), "DDU": (2, "02:00", "02:10"),
        "GAYA": (2, "04:39", "04:42"), "DHN": (2, "06:57", "07:02"),
        "ASN": (2, "07:55", "07:57"), "HWH": (2, "09:55", None),
    }
    for point in reverse:
        if point["type"] == "STOP":
            point.update(zip(["day", "arr", "dep"], reverse_times[point["code"]]))
    trains = {
        "12301": dict(train_number="12301", name="Howrah – New Delhi Rajdhani (DEMO)", origin="HWH", destination="NDLS", route=route),
        "12302": dict(train_number="12302", name="New Delhi – Howrah Rajdhani (DEMO)", origin="NDLS", destination="HWH", route=reverse),
    }
    conditions = [
        dict(id="C1", train_numbers=["12301"], type="SPEED_RESTRICTION", **{"from": "GAYA", "to": "DDU"}, restricted_km=25, speed_kmh=30, reported_at_min=0, cleared_at_min=None, note="Track maintenance near Sasaram (DEMO)"),
        dict(id="C2", train_numbers=["12301"], type="CONGESTION", **{"from": "DDU", "to": "PRYJ"}, extra_min=15, reported_at_min=330, cleared_at_min=420, note="Heavy traffic ahead (DEMO)"),
        dict(id="C3", train_numbers=["12301"], type="WEATHER_FOG", **{"from": "CNB", "to": "NDLS"}, factor=1.15, reported_at_min=600, cleared_at_min=None, note="Dense fog reported (DEMO)"),
        dict(id="C4", train_numbers=["12302"], type="CONGESTION", **{"from": "NDLS", "to": "CNB"}, extra_min=12, reported_at_min=0, cleared_at_min=320, note="Traffic approaching Kanpur (DEMO)"),
        dict(id="C5", train_numbers=["12302"], type="SPEED_RESTRICTION", **{"from": "DDU", "to": "GAYA"}, restricted_km=15, speed_kmh=45, reported_at_min=200, cleared_at_min=None, note="Temporary maintenance restriction (DEMO)"),
    ]
    for filename, content in [("stations.json", stations), ("trains.json", trains), ("conditions.json", conditions)]:
        (DATA / filename).write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {filename}: {len(content)} records")
    return trains, {s["code"]: s for s in stations}


def make_history(trains, today):
    rows, averages = [], {}
    for days_ago in range(60, 0, -1):
        journey_date = today - timedelta(days=days_ago)
        for number, train in trains.items():
            stops = [s for s in train["route"] if s["type"] == "STOP"]
            draw = random.random()
            origin_delay = random.uniform(0, 5) if draw < .70 else random.uniform(5, 30) if draw < .95 else random.uniform(30, 90)
            restricted_section = random.randrange(1, len(stops)) if random.random() < .20 else -1
            foggy = random.random() < .15
            congestion = random.random() < .25
            previous_dep = scheduled(stops[0], "dep", journey_date) + timedelta(minutes=origin_delay)
            for i, stop in enumerate(stops):
                sa, sd = scheduled(stop, "arr", journey_date), scheduled(stop, "dep", journey_date)
                flags = dict(sec_congestion="", sec_sr_km="", sec_sr_speed="", sec_fog="")
                arrival, departure = None, previous_dep
                if i:
                    previous = stops[i - 1]
                    length = stop["km"] - previous["km"]
                    sched_run = (sa - scheduled(previous, "dep", journey_date)).total_seconds() / 60
                    runtime = sched_run * random.uniform(.90, .99) + random.gauss(0, 2 + length / 100)
                    congested = congestion and stop["code"] in ("DDU", "CNB")
                    fog = foggy and i > next(j for j, s in enumerate(stops) if s["code"] == "DDU")
                    if congested:
                        runtime += random.uniform(8, 25)
                    start_delay = (previous_dep - scheduled(previous, "dep", journey_date)).total_seconds() / 60
                    if start_delay > 60:
                        runtime += random.uniform(2, 8)
                    if fog:
                        runtime *= random.uniform(1.08, 1.25)
                    sr_km = random.uniform(10, 30) if i == restricted_section else 0
                    sr_speed = random.choice([30, 45]) if sr_km else 0
                    if sr_km:
                        runtime += sr_km * (60 / sr_speed - runtime / length)
                    flags = dict(sec_congestion=int(congested), sec_sr_km=round(sr_km, 5), sec_sr_speed=sr_speed, sec_fog=int(fog))
                    arrival = previous_dep + timedelta(minutes=runtime)
                    if sd:
                        dwell = (sd - sa).total_seconds() / 60 + random.uniform(-1, 4)
                        if random.random() < .10:
                            dwell += random.uniform(5, 12)
                        departure = max(arrival + timedelta(minutes=dwell), sd)
                    else:
                        departure = None
                    averages.setdefault((number, previous["code"], stop["code"], sched_run), []).append(runtime)
                delay = (departure - sd).total_seconds() / 60 if sd else ""
                rows.append(dict(journey_date=str(journey_date), train_number=number, seq=i + 1, station_code=stop["code"], sched_arr=iso(sa), sched_dep=iso(sd), actual_arr=iso(arrival), actual_dep=iso(departure), delay_at_dep_min=round(delay, 5) if delay != "" else "", **flags))
                previous_dep = departure
    write_csv("history.csv", HISTORY_FIELDS, rows)
    print("\n  Train  Section       Scheduled  Mean actual (minutes)")
    for (number, a, b, scheduled_run), samples in averages.items():
        print(f"  {number}  {a:4}–{b:4}     {scheduled_run:6.1f}       {mean(samples):6.1f}")


def coordinates(km, route, stations):
    for a, b in zip(route, route[1:]):
        if km <= b["km"]:
            f = max(0, (km - a["km"]) / (b["km"] - a["km"]))
            x, y = stations[a["code"]], stations[b["code"]]
            return x["lat"] + f * (y["lat"] - x["lat"]), x["lon"] + f * (y["lon"] - x["lon"])
    last = stations[route[-1]["code"]]
    return last["lat"], last["lon"]


def make_replay(number, train, stations, today):
    stops = [s for s in train["route"] if s["type"] == "STOP"]
    # Forward totals: 12 origin + 1011 running + 27 dwell = 1050 min; +20 at NDLS.
    story = {"12301": dict(delay=12, runs=[128, 50, 129, 180, 98, 124, 302]),
             "12302": dict(delay=8, runs=[301, 124, 98, 150, 129, 50, 113])}[number]
    # A piecewise distance/time trace preserves ramps, signal halts and restrictions.
    knots = [(0.0, 0.0), (story["delay"] * 60.0, 0.0)]
    elapsed = story["delay"] * 60.0
    gap_start = None
    for i, (a, b) in enumerate(zip(stops, stops[1:])):
        section_start = elapsed
        length = b["km"] - a["km"]
        count = math.ceil(length / .25)
        dx = length / count
        pieces = []
        halt = 7 * 60 if number == "12301" and a["code"] == "DHN" else 0
        for j in range(count):
            distance = (j + .5) * dx
            rail_km = a["km"] + distance
            ramp = max(.12, min(1, math.sqrt(distance / 3), math.sqrt((length - distance) / 3)))
            fixed = None
            if number == "12301" and 545.5 <= rail_km < 570.5:
                fixed = dx / 30 * 3600
            if number == "12302" and a["code"] == "DDU" and 880 <= rail_km < 895:
                fixed = dx / 45 * 3600
            weight = dx / ramp
            if number == "12301" and 85 <= rail_km < 105:
                weight *= 2  # traffic near Barddhaman
            pieces.append((a["km"] + (j + 1) * dx, weight, fixed))
        free_seconds = story["runs"][i] * 60 - halt - sum(p[2] or 0 for p in pieces)
        weight_sum = sum(p[1] for p in pieces if p[2] is None)
        halted = False
        for km, weight, fixed in pieces:
            elapsed += fixed if fixed is not None else free_seconds * weight / weight_sum
            knots.append((elapsed, km))
            if halt and km >= 382 and not halted:
                elapsed += halt
                knots.append((elapsed, km))
                halted = True
        if number == "12301" and a["code"] == "PRYJ":
            gap_start = section_start + 45 * 60
        if b.get("dep"):
            sa, sd = scheduled(b, "arr", today), scheduled(b, "dep", today)
            elapsed += (sd - sa).total_seconds()
            elapsed = max(elapsed, (sd - scheduled(stops[0], "dep", today)).total_seconds())
            knots.append((elapsed, b["km"]))
    times = [k[0] for k in knots]
    offsets = list(range(0, math.ceil(elapsed), 30)) + [math.ceil(elapsed)]
    rows = []
    for offset in offsets:
        if gap_start is not None and gap_start <= offset < gap_start + 8 * 60:
            continue
        index = min(len(knots) - 2, max(0, bisect.bisect_right(times, offset) - 1))
        ta, ka = knots[index]
        tb, kb = knots[index + 1]
        f = min(1, max(0, (offset - ta) / (tb - ta)))
        km = ka + f * (kb - ka)
        speed = (kb - ka) / (tb - ta) * 3600 if offset < elapsed else 0
        lat, lon = coordinates(km, train["route"], stations)
        if offset < elapsed:
            lat += random.uniform(-15, 15) / 111320
            lon += random.uniform(-15, 15) / (111320 * math.cos(math.radians(lat)))
        if number == "12301" and offset in (15000, 15030, 15060):
            lat += .05  # about 5 km perpendicular to this section
        rows.append(dict(t_offset_sec=offset, latitude=round(lat, 7), longitude=round(lon, 7), speed_kmh=round(speed, 2), accuracy_m=random.randint(8, 25)))
    assert all(int(b["t_offset_sec"]) > int(a["t_offset_sec"]) for a, b in zip(rows, rows[1:]))
    final = stations[train["destination"]]
    assert (rows[-1]["latitude"], rows[-1]["longitude"]) == (final["lat"], final["lon"])
    write_csv(f"replay_{number}.csv", PING_FIELDS, rows)
    scheduled_minutes = (scheduled(stops[-1], "arr", today) - scheduled(stops[0], "dep", today)).total_seconds() / 60
    print(f"    Destination delay: {elapsed / 60 - scheduled_minutes:+.1f} min; timestamps and final position checked")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(IST).date())
    args = parser.parse_args()
    random.seed(42)
    DATA.mkdir(exist_ok=True)
    print(f"Generating DEMO data for {args.date} (seed 42)")
    trains, stations = seed_files()
    make_history(trains, args.date)
    print()
    for number, train in trains.items():
        make_replay(number, train, stations, args.date)


if __name__ == "__main__":
    main()
