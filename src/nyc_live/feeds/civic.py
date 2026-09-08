"""Civic feeds: 311 service requests, DOHMH restaurant inspections, weather.gov.

The registry (``nyc_live.feeds.ADAPTER_SPECS``) imports the three adapter
classes from this module; the implementations live in ``socrata.py`` and
``weather.py``.
"""

from __future__ import annotations

from nyc_live.feeds.socrata import InspectionsAdapter, Nyc311Adapter
from nyc_live.feeds.weather import WeatherAdapter

__all__ = ["InspectionsAdapter", "Nyc311Adapter", "WeatherAdapter"]
