"""Small, dependency-free geometry helpers; route km always means rail km."""
import math


def haversine_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def route_polyline(train, stations):
    return [{**point, **stations[point['code']]} for point in train['route']]


def snap_to_route(lat, lon, route):
    best = None
    for a, b in zip(route, route[1:]):
        # Local tangent plane in kilometres, centred on the segment's first point.
        scale = math.cos(math.radians((a['lat'] + b['lat']) / 2))
        bx, by = (b['lon'] - a['lon']) * 111.32 * scale, (b['lat'] - a['lat']) * 111.32
        px, py = (lon - a['lon']) * 111.32 * scale, (lat - a['lat']) * 111.32
        denominator = bx * bx + by * by
        f = max(0, min(1, (px * bx + py * by) / denominator)) if denominator else 0
        off = math.hypot(px - f * bx, py - f * by)
        candidate = {'km': a['km'] + f * (b['km'] - a['km']), 'off_route_km': off}
        if best is None or off < best['off_route_km']:
            best = candidate
    return best


def km_to_latlon(km, route):
    km = max(route[0]['km'], min(route[-1]['km'], km))
    for a, b in zip(route, route[1:]):
        if km <= b['km']:
            fraction = (km - a['km']) / (b['km'] - a['km'])
            return a['lat'] + fraction * (b['lat'] - a['lat']), a['lon'] + fraction * (b['lon'] - a['lon'])
    return route[-1]['lat'], route[-1]['lon']
