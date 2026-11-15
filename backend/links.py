"""Booking links built from validated plan fields.

The model never writes URLs: links are prefilled searches assembled here, so
they can't be invented and always use the itinerary's own towns and dates.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote_plus, urlencode

from schemas import FlightPlan, Logistics, StayLink, TripBrief, TripLinks

GOOGLE_FLIGHTS = "https://www.google.com/travel/flights?q="
BOOKING_SEARCH = "https://www.booking.com/searchresults.html?"


def flights_url(brief: TripBrief, flights: FlightPlan) -> str:
    dep, arr, ret = flights.depart_airport, flights.arrive_airport, flights.return_from_airport
    if ret == arr:
        text = f"Flights from {dep} to {arr} on {brief.start_date} returning {brief.end_date}"
    else:
        text = f"Flights from {dep} to {arr} on {brief.start_date} and from {ret} to {dep} on {brief.end_date}"
    return GOOGLE_FLIGHTS + quote_plus(text)


def stay_url(base: str, country: str, checkin: date, checkout: date) -> str:
    return BOOKING_SEARCH + urlencode(
        {"ss": f"{base}, {country}", "checkin": checkin.isoformat(), "checkout": checkout.isoformat()})


def build_links(brief: TripBrief, logistics: Logistics) -> TripLinks:
    stays: list[StayLink] = []
    checkin = brief.start_date
    for s in logistics.stays:
        checkout = checkin + timedelta(days=s.nights)
        stays.append(StayLink(base=s.base, country=s.country, url=stay_url(s.base, s.country, checkin, checkout)))
        checkin = checkout
    return TripLinks(flights=flights_url(brief, logistics.flights), stays=stays)
