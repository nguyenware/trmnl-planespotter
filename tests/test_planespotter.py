import json
from unittest.mock import MagicMock, patch

import pytest

import planespotter as ps

HOME = (32.90, -97.04)  # DFW

DFW = {"name": "Dallas Fort Worth International Airport", "iata": "DFW", "lat": 32.8968, "lon": -97.038}
MSP = {"name": "Minneapolis-St Paul International Airport", "iata": "MSP", "lat": 44.882, "lon": -93.2218}

ROUTE = {
    "callsign": "AAL2104",
    "airline_code": "AAL",
    "airport_codes": "KDFW-KMSP-KDFW",
    "_airport_codes_iata": "DFW-MSP-DFW",
    "_airports": [DFW, MSP, DFW],
}


@pytest.fixture
def config(monkeypatch, tmp_path):
    for key in ("RECEIVER_LAT", "RECEIVER_LON", "RADIUS", "PREFER_AIRLINERS", "DISTANCE_UNIT"):
        monkeypatch.delenv(key, raising=False)
    receiver = tmp_path / "receiver.json"
    receiver.write_text(json.dumps({"lat": HOME[0], "lon": HOME[1]}))
    monkeypatch.setenv("RECEIVER_JSON", str(receiver))
    monkeypatch.setenv("AIRCRAFT_JSON", str(tmp_path / "aircraft.json"))
    return ps.Config()


def write_aircraft(config, aircraft):
    with open(config.aircraft_url, "w", encoding="utf-8") as f:
        json.dump({"now": 1700000000.0, "aircraft": aircraft}, f)


def test_haversine_known_distance():
    # DFW -> MSP is roughly 853 statute miles.
    km = ps.haversine_km(DFW["lat"], DFW["lon"], MSP["lat"], MSP["lon"])
    assert 845 < km * ps.UNIT_FACTORS["mi"] < 860


def test_bearing_cardinal():
    assert round(ps.bearing(0, 0, 1, 0)) == 0
    assert round(ps.bearing(0, 0, 0, 1)) == 90
    assert round(ps.bearing(0, 0, -1, 0)) == 180


def test_compass_arrow():
    assert ps.compass_arrow(0) == "↑"
    assert ps.compass_arrow(359) == "↑"
    assert ps.compass_arrow(90) == "→"
    assert ps.compass_arrow(225) == "↙"
    assert ps.compass_arrow(None) == ""


def test_rate_arrow():
    assert ps.rate_arrow(64) == "▲"
    assert ps.rate_arrow(-64) == "▼"
    assert ps.rate_arrow(0) == ""
    assert ps.rate_arrow(None) == ""


def test_closest_prefers_airliners(config):
    aircraft = [
        {"hex": "a1", "lat": 32.91, "lon": -97.04, "category": "A1"},   # closest, light
        {"hex": "b2", "lat": 32.95, "lon": -97.04, "category": "A3"},   # airliner
        {"hex": "c3", "lat": 32.92, "lon": -97.04, "category": "A3", "seen_pos": 600},  # stale
        {"hex": "d4", "category": "A3"},                                # no position
    ]
    ac, _ = ps.closest_aircraft(aircraft, HOME, config)
    assert ac["hex"] == "b2"

    config.prefer_airliners = False
    ac, _ = ps.closest_aircraft(aircraft, HOME, config)
    assert ac["hex"] == "a1"


def test_closest_falls_back_when_no_airliners(config):
    ac, _ = ps.closest_aircraft([{"hex": "a1", "lat": 32.91, "lon": -97.04, "category": "A7"}], HOME, config)
    assert ac["hex"] == "a1"


def test_closest_respects_radius(config):
    ac, dist = ps.closest_aircraft([{"hex": "far", "lat": 40.0, "lon": -97.04}], HOME, config)
    assert ac is None and dist is None


def test_current_leg_picks_return_leg():
    # Both legs of DFW-MSP-DFW are geometrically identical, so any point
    # between the airports must resolve to a DFW<->MSP leg.
    origin, dest = ps.current_leg([DFW, MSP, DFW], 40.0, -95.0)
    assert {origin["iata"], dest["iata"]} == {"DFW", "MSP"}
    assert ps.current_leg([DFW], 40.0, -95.0) == (None, None)


def test_current_leg_uses_track_for_out_and_back():
    # Just north of DFW: heading south-west means inbound on MSP->DFW,
    # heading north-east means outbound on DFW->MSP.
    origin, dest = ps.current_leg([DFW, MSP, DFW], 33.0, -97.0, track=200)
    assert (origin["iata"], dest["iata"]) == ("MSP", "DFW")
    origin, dest = ps.current_leg([DFW, MSP, DFW], 33.0, -97.0, track=20)
    assert (origin["iata"], dest["iata"]) == ("DFW", "MSP")


def test_load_aircraft_models_has_no_header_row():
    models = ps.load_aircraft_models()
    assert models["A124"] == "Antonov An-124 Ruslan"


def test_build_flight_with_route(config):
    write_aircraft(config, [{
        "hex": "a1b2c3", "flight": "AAL2104 ", "r": "N123AA", "t": "B38M", "category": "A3",
        "lat": 33.0, "lon": -97.1, "alt_baro": 6375, "baro_rate": 2368, "gs": 272.2,
        "track": 219.19, "squawk": "2476", "nav_heading": 220.08, "nav_altitude_mcp": 16992,
        "nav_qnh": 1016.0, "seen_pos": 1.2,
    }])
    response = MagicMock(status_code=200)
    response.json.return_value = ROUTE
    with patch.object(ps.requests, "get", return_value=response) as get:
        flight = ps.build_flight(config)
    get.assert_called_once_with("https://vrs-standing-data.adsb.lol/routes/AA/AAL2104.json", timeout=10)

    assert flight["status"] == "ok"
    assert flight["flight"] == "AAL2104"
    assert flight["model"] == "Boeing 737 MAX 8"
    assert flight["route"] == "DFW-MSP-DFW"
    assert flight["alt"] == 6375 and flight["rate_arrow"] == "▲"
    assert flight["track_arrow"] == "↙"
    assert flight["logo"].endswith("/AAL.png")
    assert flight["flown"] + flight["to_go"] > 0
    assert 0 <= flight["progress"] <= 100
    # A webhook payload must fit in TRMNL's 2 KB limit.
    assert len(json.dumps({"merge_variables": flight}, separators=(",", ":")).encode()) < 2048


def test_build_flight_without_route(config):
    write_aircraft(config, [{"hex": "abcdef", "lat": 32.91, "lon": -97.04, "alt_baro": "ground"}])
    with patch.object(ps.requests, "get") as get:
        flight = ps.build_flight(config)
    get.assert_not_called()
    assert flight["flight"] == "ABCDEF"
    assert flight["alt"] == "ground"
    assert flight["route"] == "" and flight["logo"] is None


def test_build_flight_empty_sky(config):
    write_aircraft(config, [])
    flight = ps.build_flight(config)
    assert flight["status"] == "empty"


def test_receiver_location_override(config):
    config.lat, config.lon = 1.0, 2.0
    assert ps.receiver_location(config) == (1.0, 2.0)


def test_receiver_location_missing(config, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    config.receiver_url = str(empty)
    with pytest.raises(RuntimeError):
        ps.receiver_location(config)


def test_push_posts_merge_variables(config):
    config.webhook_url = "https://trmnl.com/api/custom_plugins/test"
    with patch.object(ps.requests, "post", return_value=MagicMock(status_code=200)) as post:
        ps.push(config, {"status": "empty"})
    post.assert_called_once_with(config.webhook_url, json={"merge_variables": {"status": "empty"}}, timeout=15)
