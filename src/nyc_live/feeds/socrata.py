"""Socrata (SoDA 2.1) adapters: NYC 311 service requests and DOHMH restaurant inspections.

Both datasets live on ``settings.socrata_base`` (``https://data.cityofnewyork.us``).
An app token is optional; when ``SOCRATA_APP_TOKEN`` is set it is sent as
``X-App-Token`` (raises the anonymous throttle). Socrata "floating timestamps"
carry no zone; NYC datasets publish them in America/New_York, so every timestamp
is localised there and converted to UTC before it reaches a record.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ValidationError

from nyc_live.config import Settings
from nyc_live.contracts import (
    DEFAULT_TTL,
    ErrorKind,
    FeedName,
    FeedUnavailable,
    RestaurantInspection,
    ServiceRequest,
    Snapshot,
    now_utc,
)
from nyc_live.geo import in_nyc_bbox
from nyc_live.http import RateLimiter, get_with_retry

log = logging.getLogger(__name__)

NYC_TZ = ZoneInfo("America/New_York")

DATASET_311 = "erm2-nwe9"
DATASET_INSPECTIONS = "43nn-pn8j"

SODA_PAGE_SIZE = 20_000
"""``$limit`` per request. Socrata 2.1 has no hard cap, but 20k keeps a page well under a minute."""

SODA_MAX_PAGES = 10
"""Safety cap on ``$offset`` paging (200k rows). Hitting it logs a warning; it never fabricates."""

NYC_311_WINDOW = timedelta(hours=24)
INSPECTIONS_WINDOW = timedelta(days=90)

Row = dict[str, Any]


# ---------------------------------------------------------------------------
# Shared SoDA helpers
# ---------------------------------------------------------------------------


def parse_floating_timestamp(raw: object) -> datetime | None:
    """Parse a Socrata floating timestamp (``2026-09-08T09:15:00.000``) as NYC local time -> UTC.

    Returns None for null / empty. A value that already carries an offset is
    honoured as-is. Anything that is not a string is a parse error.
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"expected a floating timestamp string, got {type(raw).__name__}")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=NYC_TZ).astimezone(UTC)


def format_floating_timestamp(dt: datetime) -> str:
    """Render an aware datetime as the NYC-local literal Socrata compares floating columns against."""
    return dt.astimezone(NYC_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def soda_headers(settings: Settings) -> dict[str, str]:
    """``X-App-Token`` only when the token is configured; never an empty header."""
    token = settings.socrata_app_token
    return {"X-App-Token": token} if token else {}


def soda_resource_url(settings: Settings, dataset: str) -> str:
    return f"{settings.socrata_base.rstrip('/')}/resource/{dataset}.json"


def parse_coord(raw: object) -> float | None:
    """Socrata returns coordinates as strings; blank / null / garbage -> None."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_int(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def optional_str(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


async def soda_fetch_all(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    feed: FeedName,
    dataset: str,
    where: str,
    order: str,
    select: str | None = None,
    page_size: int = SODA_PAGE_SIZE,
    max_pages: int = SODA_MAX_PAGES,
    backoff_s: float = 0.5,
) -> list[Row]:
    """GET every row matching ``where``, paging with ``$offset`` while a page is exactly full."""
    url = soda_resource_url(settings, dataset)
    headers = soda_headers(settings)
    rows: list[Row] = []
    for page in range(max_pages):
        params: dict[str, str | int | float] = {
            "$where": where,
            "$order": order,
            "$limit": page_size,
            "$offset": page * page_size,
        }
        if select:
            params["$select"] = select
        resp = await get_with_retry(
            client,
            url,
            feed=feed,
            retries=settings.http_retries,
            params=params,
            headers=headers,
            backoff_s=backoff_s,
        )
        body = _decode_rows(resp, feed=feed, url=url)
        rows.extend(body)
        log.debug("%s: page %d returned %d rows", feed.value, page, len(body))
        if len(body) < page_size:
            return rows
    log.warning(
        "%s: stopped paging %s after %d pages (%d rows); increase SODA_MAX_PAGES if this is real",
        feed.value,
        dataset,
        max_pages,
        len(rows),
    )
    return rows


def _decode_rows(resp: httpx.Response, *, feed: FeedName, url: str) -> list[Row]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise FeedUnavailable(
            feed,
            f"non-JSON body from {url}: {exc}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
            upstream_status=resp.status_code,
        ) from exc
    if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
        raise FeedUnavailable(
            feed,
            f"expected a JSON array of objects from {url}, got {type(body).__name__}",
            kind=ErrorKind.UPSTREAM_PARSE,
            url=url,
            upstream_status=resp.status_code,
        )
    return body


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class _SocrataAdapter[RecordT: BaseModel]:
    """Shared fetch/paging/bbox pipeline. Subclasses declare the query and the row mapper."""

    name: ClassVar[FeedName]
    dataset: ClassVar[str]
    order: ClassVar[str]
    select: ClassVar[str | None] = None
    require_location: ClassVar[bool] = False

    page_size: int = SODA_PAGE_SIZE
    max_pages: int = SODA_MAX_PAGES
    backoff_s: float = 0.5

    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self.ttl: timedelta = DEFAULT_TTL[self.name]
        self._limiter = RateLimiter(self.ttl)

    def is_configured(self) -> bool:
        return True

    @property
    def source_url(self) -> str:
        return soda_resource_url(self._settings, self.dataset)

    def where(self, now: datetime) -> str:
        raise NotImplementedError

    def parse_row(self, row: Row) -> RecordT:
        raise NotImplementedError

    async def fetch(self) -> Snapshot[RecordT]:
        await self._limiter.wait(self.name.value)
        started = time.perf_counter()
        fetched_at = now_utc()
        rows = await soda_fetch_all(
            self._client,
            self._settings,
            feed=self.name,
            dataset=self.dataset,
            where=self.where(fetched_at),
            order=self.order,
            select=self.select,
            page_size=self.page_size,
            max_pages=self.max_pages,
            backoff_s=self.backoff_s,
        )
        if not rows:
            raise FeedUnavailable(
                self.name,
                f"{self.dataset} returned 0 rows for `{self.where(fetched_at)}`; "
                "upstream is lagging or the query is wrong",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )
        records = self._map_rows(rows)
        if not records:
            raise FeedUnavailable(
                self.name,
                f"none of {len(rows)} rows from {self.dataset} could be mapped to {self.name.value}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )
        return Snapshot[RecordT](
            feed=self.name,
            fetched_at=fetched_at,
            stale_after=fetched_at + self.ttl,
            source_url=self.source_url,
            records=records,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    def _map_rows(self, rows: list[Row]) -> list[RecordT]:
        records: list[RecordT] = []
        bad = 0
        unlocated = 0
        outside = 0
        for row in rows:
            try:
                record = self.parse_row(row)
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                bad += 1
                if bad <= 3:
                    log.warning("%s: skipping unparseable row: %s", self.name.value, exc)
                continue
            lat = getattr(record, "lat", None)
            lon = getattr(record, "lon", None)
            if lat is None or lon is None:
                if self.require_location:
                    unlocated += 1
                    continue
            elif not in_nyc_bbox(lat, lon):
                outside += 1
                continue
            records.append(record)
        if bad:
            log.warning("%s: skipped %d unparseable rows of %d", self.name.value, bad, len(rows))
        if unlocated:
            log.info("%s: dropped %d rows without coordinates", self.name.value, unlocated)
        if outside:
            log.info("%s: dropped %d rows outside the NYC bbox", self.name.value, outside)
        return records


# ---------------------------------------------------------------------------
# 311 service requests
# ---------------------------------------------------------------------------


class Nyc311Adapter(_SocrataAdapter[ServiceRequest]):
    """Every 311 request created in the last 24 h, city-wide, newest first.

    Rows without coordinates are kept (``ServiceRequest`` is ``MaybeLocated``);
    rows whose coordinates fall outside the NYC bbox are dropped and counted.
    """

    name = FeedName.NYC_311
    dataset = DATASET_311
    # unique_key as a tiebreak keeps $offset paging stable when created_date ties.
    order = "created_date DESC, unique_key DESC"
    select = ",".join(
        (
            "unique_key",
            "created_date",
            "closed_date",
            "agency",
            "complaint_type",
            "descriptor",
            "status",
            "borough",
            "incident_zip",
            "incident_address",
            "location_type",
            "latitude",
            "longitude",
        )
    )
    require_location = False
    window = NYC_311_WINDOW

    def where(self, now: datetime) -> str:
        return f"created_date > '{format_floating_timestamp(now - self.window)}'"

    def parse_row(self, row: Row) -> ServiceRequest:
        created_at = parse_floating_timestamp(row.get("created_date"))
        if created_at is None:
            raise ValueError(f"311 row {row.get('unique_key')!r} has no created_date")
        return ServiceRequest(
            lat=parse_coord(row.get("latitude")),
            lon=parse_coord(row.get("longitude")),
            unique_key=str(row["unique_key"]),
            created_at=created_at,
            closed_at=parse_floating_timestamp(row.get("closed_date")),
            agency=str(row.get("agency") or "UNKNOWN"),
            complaint_type=str(row.get("complaint_type") or "UNKNOWN"),
            descriptor=optional_str(row.get("descriptor")),
            status=optional_str(row.get("status")),
            borough=optional_str(row.get("borough")),
            incident_zip=optional_str(row.get("incident_zip")),
            incident_address=optional_str(row.get("incident_address")),
            location_type=optional_str(row.get("location_type")),
        )


# ---------------------------------------------------------------------------
# DOHMH restaurant inspections
# ---------------------------------------------------------------------------


class InspectionsAdapter(_SocrataAdapter[RestaurantInspection]):
    """One row per violation for inspections in the last 90 days, geocoded rows only.

    The dataset marks never-inspected venues with ``inspection_date = 1900-01-01``;
    the 90-day window excludes them. Rows without coordinates, or with the
    ``0,0`` placeholder, are dropped and counted.
    """

    name = FeedName.DOHMH_INSPECTIONS
    dataset = DATASET_INSPECTIONS
    order = "inspection_date DESC, camis, violation_code"
    select = ",".join(
        (
            "camis",
            "dba",
            "boro",
            "building",
            "street",
            "zipcode",
            "cuisine_description",
            "inspection_date",
            "action",
            "violation_code",
            "violation_description",
            "critical_flag",
            "score",
            "grade",
            "grade_date",
            "inspection_type",
            "latitude",
            "longitude",
        )
    )
    require_location = True
    window = INSPECTIONS_WINDOW

    def where(self, now: datetime) -> str:
        since = format_floating_timestamp(now - self.window)
        return f"inspection_date >= '{since}' AND latitude IS NOT NULL AND longitude IS NOT NULL"

    def parse_row(self, row: Row) -> RestaurantInspection:
        return RestaurantInspection(
            lat=parse_coord(row.get("latitude")),
            lon=parse_coord(row.get("longitude")),
            camis=str(row["camis"]),
            dba=optional_str(row.get("dba")),
            boro=optional_str(row.get("boro")),
            building=optional_str(row.get("building")),
            street=optional_str(row.get("street")),
            zipcode=optional_str(row.get("zipcode")),
            cuisine=optional_str(row.get("cuisine_description")),
            inspection_date=parse_floating_timestamp(row.get("inspection_date")),
            action=optional_str(row.get("action")),
            violation_code=optional_str(row.get("violation_code")),
            violation_description=optional_str(row.get("violation_description")),
            critical_flag=optional_str(row.get("critical_flag")),
            score=parse_int(row.get("score")),
            grade=optional_str(row.get("grade")),
            grade_date=parse_floating_timestamp(row.get("grade_date")),
            inspection_type=optional_str(row.get("inspection_type")),
        )


__all__ = [
    "DATASET_311",
    "DATASET_INSPECTIONS",
    "NYC_TZ",
    "InspectionsAdapter",
    "Nyc311Adapter",
    "format_floating_timestamp",
    "parse_floating_timestamp",
    "soda_fetch_all",
    "soda_headers",
    "soda_resource_url",
]
