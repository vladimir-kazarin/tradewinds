from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs, urlparse

from helpers import logistics
from links import build_links, flights_url, stay_url

CR = "Costa Rica"


def _q(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_round_trip_flight_url(brief):
    url = flights_url(brief, logistics([("Nosara", CR, 14)]).flights)
    assert urlparse(url).netloc == "www.google.com"
    q = _q(url)["q"][0]
    for s in ("BOS", "LIR", "2026-12-01", "2026-12-15"):
        assert s in q


def test_open_jaw_flight_url(brief):
    url = flights_url(brief, logistics([("x", CR, 14)], arr="LIR", ret="PTY").flights)
    assert "from PTY to BOS on 2026-12-15" in _q(url)["q"][0]


def test_stay_dates_are_cumulative(brief):
    links = build_links(brief, logistics([("Nosara", CR, 4), ("Santa Teresa", CR, 6), ("Sámara", CR, 4)]))
    outs = [_q(s.url)["checkout"][0] for s in links.stays]
    ins = [_q(s.url)["checkin"][0] for s in links.stays]
    assert ins == ["2026-12-01", "2026-12-05", "2026-12-11"]
    assert outs == ["2026-12-05", "2026-12-11", "2026-12-15"]
    assert [s.base for s in links.stays] == ["Nosara", "Santa Teresa", "Sámara"]


def test_stay_url_encodes_special_characters():
    name = "Sámara & D'Arenal"
    url = stay_url(name, CR, date(2026, 12, 1), date(2026, 12, 5))
    query = urlparse(url).query
    assert " " not in url and "'" not in url and "& " not in url
    assert query.count("&") == 2  # only the separators between three params
    assert _q(url)["ss"] == [f"{name}, {CR}"]


def test_all_links_https(brief):
    links = build_links(brief, logistics([("Nosara", CR, 14)]))
    assert links.flights.startswith("https://")
    assert all(s.url.startswith("https://www.booking.com/") for s in links.stays)
