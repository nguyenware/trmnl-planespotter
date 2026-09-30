#!/usr/bin/env python3
"""Planespotter: show the closest aircraft seen by a local tar1090 feeder on a TRMNL.

A personal version of https://github.com/ervansetiawan/itsaplane-trmnl that reads
aircraft from your own receiver (tar1090 / readsb on a Raspberry Pi) instead of
the adsb.lol API. The closest aircraft is enriched with route information from
adsb.lol's VRS standing data and then either:

  * pushed to a TRMNL private plugin webhook (``push``, the default), or
  * served as JSON for a TRMNL polling plugin / BYOS server (``serve``), or
  * printed once (``once``) for testing.

Configuration comes from environment variables (optionally loaded from a
``.env`` file next to this script). See ``.env.example``.
"""

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import asin, atan2, cos, degrees, radians, sin, sqrt

import requests

HERE = os.path.dirname(os.path.abspath(__file__))

EARTH_RADIUS_KM = 6371.0088
UNIT_FACTORS = {"mi": 0.621371, "nm": 0.539957, "km": 1.0}
AIRLINER_CATEGORIES = ("A3", "A4", "A5")
COMPASS_ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
ROUTE_URL = "https://vrs-standing-data.adsb.lol/routes/{prefix}/{callsign}.json"
DEFAULT_LOGO_BASE_URL = "https://raw.githubusercontent.com/ervansetiawan/itsaplane-trmnl/main/logos"


def load_dotenv(path):
    """Minimal .env loader so the script only depends on requests."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_float(name, default=None):
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


class Config:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """All settings, read from the environment."""

    def __init__(self):
        base = os.environ.get("TAR1090_URL", "http://localhost/tar1090").rstrip("/")
        self.aircraft_url = os.environ.get("AIRCRAFT_JSON", f"{base}/data/aircraft.json")
        self.receiver_url = os.environ.get("RECEIVER_JSON", f"{base}/data/receiver.json")
        self.lat = env_float("RECEIVER_LAT")
        self.lon = env_float("RECEIVER_LON")
        self.unit = os.environ.get("DISTANCE_UNIT", "mi").lower()
        if self.unit not in UNIT_FACTORS:
            raise SystemExit(f"DISTANCE_UNIT must be one of {', '.join(UNIT_FACTORS)}")
        self.radius = env_float("RADIUS", 25.0)
        self.prefer_airliners = os.environ.get("PREFER_AIRLINERS", "1") not in ("0", "false", "no")
        self.max_age = env_float("MAX_POSITION_AGE", 60.0)
        self.webhook_url = os.environ.get("TRMNL_WEBHOOK_URL", "").strip()
        self.interval = int(env_float("PUSH_INTERVAL", 300))
        self.logo_base_url = os.environ.get("LOGO_BASE_URL", DEFAULT_LOGO_BASE_URL).rstrip("/")
        self.host = os.environ.get("HOST", "0.0.0.0")
        self.port = int(env_float("PORT", 5000))


def read_json(location):
    """Reads JSON from an http(s) URL or a local file path (e.g. /run/readsb/aircraft.json)."""
    if location.startswith(("http://", "https://")):
        response = requests.get(location, timeout=10)
        response.raise_for_status()
        return response.json()
    with open(location, encoding="utf-8") as json_file:
        return json.load(json_file)


def load_aircraft_models(path=os.path.join(HERE, "aircrafts.csv")):
    """Maps ICAO type designators (B38M) to model names (Boeing 737 MAX 8)."""
    models = {}
    if not os.path.exists(path):
        return models
    with open(path, encoding="utf-8") as csv_file:
        for line in csv_file:
            code, _, model = line.strip().partition(",")
            if code and model:
                models[code.strip()] = model.strip()
    return models


def load_logo_codes(path=os.path.join(HERE, "logos.txt")):
    """Airline ICAO codes that have a logo at LOGO_BASE_URL."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as logo_file:
        return {line.strip().upper() for line in logo_file if line.strip()}


AIRCRAFT_MODELS = load_aircraft_models()
LOGO_CODES = load_logo_codes()


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    """Initial bearing in degrees from point 1 to point 2."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    x = sin(lon2 - lon1) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(lon2 - lon1)
    return (degrees(atan2(x, y)) + 360) % 360


def compass_arrow(heading):
    try:
        return COMPASS_ARROWS[round(float(heading) / 45) % 8]
    except (TypeError, ValueError):
        return ""


def rate_arrow(rate):
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return ""
    if rate > 0:
        return "▲"
    if rate < 0:
        return "▼"
    return ""


def receiver_location(config):
    """Receiver lat/lon from config, falling back to tar1090's receiver.json."""
    if config.lat is not None and config.lon is not None:
        return config.lat, config.lon
    receiver = read_json(config.receiver_url)
    if "lat" not in receiver or "lon" not in receiver:
        raise RuntimeError(
            "receiver.json has no location; set RECEIVER_LAT and RECEIVER_LON"
        )
    return float(receiver["lat"]), float(receiver["lon"])


def closest_aircraft(aircraft_list, home, config):
    """Returns (aircraft, distance_km) for the closest aircraft with a fresh position.

    With prefer_airliners, large aircraft (ADS-B categories A3-A5) win if any
    are in range; otherwise the closest aircraft of any kind is used.
    """
    radius_km = config.radius / UNIT_FACTORS[config.unit]
    candidates = []
    for ac in aircraft_list:
        if "lat" not in ac or "lon" not in ac:
            continue
        if ac.get("seen_pos", 0) > config.max_age:
            continue
        dist = haversine_km(home[0], home[1], ac["lat"], ac["lon"])
        if dist <= radius_km:
            candidates.append((dist, ac))
    if not candidates:
        return None, None

    if config.prefer_airliners:
        airliners = [c for c in candidates if c[1].get("category") in AIRLINER_CATEGORIES]
        candidates = airliners or candidates
    dist, ac = min(candidates, key=lambda c: c[0])
    return ac, dist


def fetch_route(callsign):
    """Route for a callsign from adsb.lol's VRS standing data, or None."""
    callsign = (callsign or "").strip().upper()
    if len(callsign) < 3:
        return None
    try:
        response = requests.get(
            ROUTE_URL.format(prefix=callsign[:2], callsign=callsign), timeout=10
        )
        if response.status_code == 200:
            return response.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Route lookup failed for {callsign}: {error}", file=sys.stderr)
    return None


def current_leg(airports, lat, lon, track=None):
    """Picks the route leg the aircraft is most likely flying.

    Multi-stop routes (DFW-MSP-DFW) list every airport; the current leg is the
    one where origin->aircraft->destination adds the least detour. Legs whose
    destination is behind the aircraft's track are penalised, which separates
    out-and-back legs that are otherwise identical.
    Returns (origin, destination) airport dicts, or (None, None).
    """
    best, best_score = (None, None), None
    for origin, dest in zip(airports, airports[1:]):
        try:
            leg = haversine_km(origin["lat"], origin["lon"], dest["lat"], dest["lon"])
            via = (haversine_km(origin["lat"], origin["lon"], lat, lon)
                   + haversine_km(lat, lon, dest["lat"], dest["lon"]))
            score = via - leg
            if track is not None:
                off = abs((bearing(lat, lon, dest["lat"], dest["lon"]) - float(track) + 180) % 360 - 180)
                if off > 90:
                    score += leg
        except (KeyError, TypeError, ValueError):
            continue
        if best_score is None or score < best_score:
            best, best_score = (origin, dest), score
    return best


def number(value, digits=0):
    """Rounds numeric values and passes through strings like 'ground'."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if digits and not float(value).is_integer():
            return round(value, digits)
        return int(round(value))
    return value


def build_flight(config):  # pylint: disable=too-many-locals
    """Builds the display payload for the closest aircraft."""
    home = receiver_location(config)
    data = read_json(config.aircraft_url)
    ac, dist_km = closest_aircraft(data.get("aircraft", []), home, config)
    factor = UNIT_FACTORS[config.unit]
    if ac is None:
        return {"status": "empty", "unit": config.unit, "radius": number(config.radius, 1),
                "updated": int(data.get("now", time.time()))}

    callsign = (ac.get("flight") or "").strip()
    type_code = ac.get("t", "")
    direction = bearing(home[0], home[1], ac["lat"], ac["lon"])
    track = ac.get("track", ac.get("true_heading"))

    flight = {
        "status": "ok",
        "unit": config.unit,
        "updated": int(data.get("now", time.time())),
        "flight": callsign or ac.get("r") or ac.get("hex", "").upper(),
        "hex": ac.get("hex", "").upper(),
        "reg": ac.get("r", ""),
        "type": type_code,
        "model": AIRCRAFT_MODELS.get(type_code) or ac.get("desc", "") or type_code,
        "alt": number(ac.get("alt_baro", ac.get("alt_geom"))),
        "rate": number(ac.get("baro_rate", ac.get("geom_rate"))),
        "rate_arrow": rate_arrow(ac.get("baro_rate", ac.get("geom_rate"))),
        "gs": number(ac.get("gs"), 1),
        "track": number(track, 1),
        "track_arrow": compass_arrow(track),
        "squawk": ac.get("squawk", ""),
        "emergency": ac.get("emergency", "none") not in ("none", ""),
        "nav_heading": number(ac.get("nav_heading"), 1),
        "nav_heading_arrow": compass_arrow(ac.get("nav_heading")),
        "nav_alt": number(ac.get("nav_altitude_mcp")),
        "qnh": number(ac.get("nav_qnh"), 1),
        "dist": round(dist_km * factor, 1),
        "dir": round(direction),
        "dir_arrow": compass_arrow(direction),
        "route": "",
        "airports": [],
        "flown": None,
        "to_go": None,
        "progress": None,
        "logo": None,
    }

    route = fetch_route(callsign)
    if route:
        airports = route.get("_airports") or []
        flight["route"] = route.get("_airport_codes_iata") or route.get("airport_codes", "")
        flight["airports"] = [a.get("name", "") for a in airports][:4]
        origin, dest = current_leg(airports, ac["lat"], ac["lon"], track)
        if origin:
            flown = haversine_km(origin["lat"], origin["lon"], ac["lat"], ac["lon"])
            to_go = haversine_km(ac["lat"], ac["lon"], dest["lat"], dest["lon"])
            flight["flown"] = round(flown * factor)
            flight["to_go"] = round(to_go * factor)
            flight["progress"] = round(100 * flown / (flown + to_go)) if flown + to_go else 0
        airline = (route.get("airline_code") or "").upper()
        if airline in LOGO_CODES:
            flight["logo"] = f"{config.logo_base_url}/{airline}.png"
    return flight


def push(config, flight):
    """Sends the payload to the TRMNL private plugin webhook."""
    body = {"merge_variables": flight}
    size = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    if size > 2048:
        # Webhooks accept 2 KB (5 KB for TRMNL+); airport names are the only unbounded field.
        flight["airports"] = [name[:40] for name in flight.get("airports", [])][:3]
    response = requests.post(config.webhook_url, json=body, timeout=15)
    if response.status_code == 429:
        print("TRMNL rate limit hit (429); consider raising PUSH_INTERVAL", file=sys.stderr)
    response.raise_for_status()


def run_push(config):
    if not config.webhook_url:
        raise SystemExit("TRMNL_WEBHOOK_URL is required for push mode")
    while True:
        try:
            flight = build_flight(config)
            push(config, flight)
            print(f"Pushed {flight.get('flight', 'no aircraft')}"
                  f" {flight.get('route', '')} {flight.get('dist', '')}", flush=True)
        except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
            print(f"Update failed: {error}", file=sys.stderr, flush=True)
        time.sleep(config.interval)


def run_server(config):
    class Handler(BaseHTTPRequestHandler):
        """Serves the closest flight as JSON."""

        def do_GET(self):  # pylint: disable=invalid-name
            if self.path.split("?")[0] not in ("/", "/closest_flight"):
                self.send_error(404)
                return
            try:
                status, body = 200, build_flight(config)
            except (requests.RequestException, OSError, ValueError, RuntimeError) as error:
                status, body = 502, {"status": "error", "error": str(error)}
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    print(f"Serving on http://{config.host}:{config.port}/closest_flight", flush=True)
    ThreadingHTTPServer((config.host, config.port), Handler).serve_forever()


def main():
    load_dotenv(os.path.join(HERE, ".env"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("mode", nargs="?", default="push", choices=("push", "serve", "once"),
                        help="push to a TRMNL webhook (default), serve JSON, or print once")
    args = parser.parse_args()
    config = Config()
    if args.mode == "once":
        print(json.dumps(build_flight(config), indent=2, ensure_ascii=False))
    elif args.mode == "serve":
        run_server(config)
    else:
        run_push(config)


if __name__ == "__main__":
    main()
