"""Socrata (SoDA 2.1) adapters: NYC 311 service requests and DOHMH restaurant inspections.

Both datasets live on ``settings.socrata_base`` (``https://data.cityofnewyork.us``).
An app token is optional; when ``SOCRATA_APP_TOKEN`` is set it is sent as
``X-App-Token`` (raises the anonymous throttle). Socrata "floating timestamps"
carry no zone; NYC datasets publish them in America/New_York, so every timestamp
is localised there and converted to UTC before it reaches a record.

Record identity differs between the two: 311's ``unique_key`` is one row per
service request, but DOHMH publishes one row per *violation per inspection
visit*, so ``InspectionsAdapter`` collapses those to one record per restaurant
(``camis``), keeping the collapsed rows as ``RestaurantInspection.violations``
-- see ``inspection_rank`` and ``_collapse_visit``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from itertools import groupby
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
    InspectionViolation,
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

INSPECTIONS_WINDOW = timedelta(days=90)

NYC_311_STALENESS_CEILING = timedelta(days=7)
"""erm2-nwe9 publishes in daily batches and has been observed live lagging the
wall clock by 24-48 h (e.g. 37.6 h on 2026-09-09, newest row created
2026-09-08T01:51 EDT). A fixed 24 h ``$where`` window is fragile to that: any
lag past 24 h returns zero rows and the feed goes down for no real reason.
Nyc311Adapter instead queries by recency alone (``$order`` + ``$limit``, no
time filter) and uses this ceiling only as a circuit breaker: if even the
newest row is older than this, the pipeline is genuinely stalled rather than
routinely lagging, and the feed should fail loud rather than silently serve
week-old rows as "current" 311 data."""

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
    where: str | None,
    order: str,
    select: str | None = None,
    page_size: int = SODA_PAGE_SIZE,
    max_pages: int = SODA_MAX_PAGES,
    backoff_s: float = 0.5,
) -> list[Row]:
    """GET every row matching ``where``, paging with ``$offset`` while a page is exactly full.

    ``where`` is optional: a feed that queries by recency alone (``$order`` +
    ``$limit``, no calendar cutoff) passes ``None`` and the clause is omitted
    entirely rather than sent as an empty string.
    """
    url = soda_resource_url(settings, dataset)
    headers = soda_headers(settings)
    rows: list[Row] = []
    for page in range(max_pages):
        params: dict[str, str | int | float] = {
            "$order": order,
            "$limit": page_size,
            "$offset": page * page_size,
        }
        if where:
            params["$where"] = where
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

    def where(self, now: datetime) -> str | None:
        """Build the ``$where`` clause, or ``None`` for a recency-only query (no time filter)."""
        raise NotImplementedError

    def parse_row(self, row: Row) -> RecordT:
        raise NotImplementedError

    def post_process(self, records: list[RecordT]) -> list[RecordT]:
        """Hook: collapse / reorder mapped records before they become a Snapshot.

        Runs after parsing and bbox filtering, so subclasses only ever see valid,
        in-NYC records. Default is a no-op; ``InspectionsAdapter`` overrides it to
        collapse DOHMH's one-row-per-violation output to one row per restaurant.
        """
        return records

    def validate_freshness(self, rows: list[Row], fetched_at: datetime) -> None:
        """Optional circuit breaker for feeds with no calendar ``$where`` window.

        A time-bounded query already fails loud on zero rows if it lags past
        its own window. A feed that queries by recency alone has no such
        signal -- a stalled upstream would still return `page_size` real rows,
        just very old ones -- so subclasses that need one override this to
        raise `FeedUnavailable` when even the newest row is implausibly stale.
        Default is a no-op.
        """
        return None

    async def fetch(self) -> Snapshot[RecordT]:
        await self._limiter.wait(self.name.value)
        try:
            return await self._fetch_once()
        except BaseException:
            # A failed attempt must not hold the cadence floor against the next try:
            # otherwise the second refresh after any failure sleeps a whole TTL
            # (300 s for 311, 6 h for inspections) inside the caller's refresh lock.
            # This covers every exit from _fetch_once, not just the GET: a mid-paging
            # failure, a non-array body, an empty window, and cancellation.
            self._limiter.forget(self.name.value)
            raise

    async def _fetch_once(self) -> Snapshot[RecordT]:
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
            where_desc = self.where(fetched_at) or "no time filter (recency-only query)"
            raise FeedUnavailable(
                self.name,
                f"{self.dataset} returned 0 rows for `{where_desc}`; "
                "upstream is lagging or the query is wrong",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )
        self.validate_freshness(rows, fetched_at)
        records = self._map_rows(rows)
        if not records:
            raise FeedUnavailable(
                self.name,
                f"none of {len(rows)} rows from {self.dataset} could be mapped to {self.name.value}",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )
        records = self.post_process(records)
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
    """The most recent (up to 20,000) 311 requests city-wide, newest first.

    This is deliberately a recency query (``$order`` + ``$limit``), not a
    ``created_date > now - 24h`` calendar window: erm2-nwe9 publishes in daily
    batches and has been observed live lagging the wall clock by well over
    24 h (37.6 h on 2026-09-09), so a fixed 24 h ``$where`` cutoff can -- and
    did -- legitimately return zero rows while the feed was perfectly healthy,
    just running behind. Ordering by ``created_date DESC`` and taking the top
    page is robust to arbitrary publish lag: it always returns whatever is
    actually newest, and paging is capped at one page (``max_pages = 1``) so a
    permanently-caught-up feed doesn't walk the entire multi-million-row table.
    ``validate_freshness`` is the remaining circuit breaker: if even the
    newest row is older than ``NYC_311_STALENESS_CEILING``, that is no longer
    routine lag and the feed fails loud instead of silently serving stale rows.

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
    max_pages = 1
    staleness_ceiling = NYC_311_STALENESS_CEILING

    def where(self, now: datetime) -> str | None:
        return None

    def validate_freshness(self, rows: list[Row], fetched_at: datetime) -> None:
        newest = parse_floating_timestamp(rows[0].get("created_date"))
        if newest is None:
            return
        age = fetched_at - newest
        if age > self.staleness_ceiling:
            raise FeedUnavailable(
                self.name,
                f"{self.dataset}'s newest row is {age} old (created_date={rows[0].get('created_date')!r}), "
                f"past the {self.staleness_ceiling} staleness ceiling; treating this as a stalled "
                "upstream pipeline rather than routine publish lag",
                kind=ErrorKind.UPSTREAM_PARSE,
                url=self.source_url,
            )

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

CRITICAL_FLAG = "Critical"
"""``critical_flag`` value DOHMH uses for a critical violation (vs "Not Critical" /
"Not Applicable")."""

InspectionRank = tuple[float, tuple[bool, bool], int, tuple[bool, str], tuple[bool, str]]


def _newest_first(record: RestaurantInspection) -> float:
    """Ascending sort component putting the newest inspection first, undated rows last."""
    when = record.inspection_date
    return -when.timestamp() if when is not None else float("inf")


def inspection_rank(record: RestaurantInspection) -> InspectionRank:
    """Sort key deciding which of a restaurant's rows survives deduplication (lowest wins).

    43nn-pn8j publishes one row per violation per inspection visit, so a single
    ``camis`` routinely appears 3-10 times in a 90-day window. Collapsing needs a
    *total* order, not just "newest": ~10% of same-day groups hold two different
    inspections (a graded "Cycle Inspection" plus an ungraded ancillary one such as
    "Smoke-Free Air Act" or "Administrative Miscellaneous"), and every violation of
    a visit repeats the same date. Without a full tie-break the surviving row would
    flap between violations on every refresh and could land on an ungraded ancillary
    row that reports ``grade=None, score=None`` for a restaurant that was in fact
    graded that day. Components, in order:

    1. newest ``inspection_date`` first (undated rows last);
    2. rows carrying a score, then a grade, first -- keeps the graded inspection of
       the day over the ancillary one;
    3. ``Critical`` violations before non-critical -- the most consequential
       violation of the visit is the one worth surfacing;
    4. then ``violation_code`` and ``violation_description`` ascending (nulls last)
       purely to make the order total and therefore stable across fetches.

    Rows that still tie after every component are byte-identical duplicates that
    DOHMH itself publishes (48 of 25,418 rows on 2026-09-09), so the choice between
    them is not observable.
    """
    return (
        _newest_first(record),
        (record.score is None, record.grade is None),
        0 if record.critical_flag == CRITICAL_FLAG else 1,
        (record.violation_code is None, record.violation_code or ""),
        (record.violation_description is None, record.violation_description or ""),
    )


def _display_order(record: RestaurantInspection) -> tuple[float, str]:
    """Snapshot order: newest inspection first, ``camis`` breaking the (many) date ties."""
    return (_newest_first(record), record.camis)


def _collapse_visit(ranked_group: list[RestaurantInspection]) -> RestaurantInspection:
    """One restaurant's rows (already sorted by :func:`inspection_rank`) -> one record.

    The first row wins and carries every violation cited on its ``inspection_date``,
    itself included, so a consumer can render ``violations`` without special-casing
    the mirrored ``violation_code`` / ``violation_description`` pair. The list keeps
    the ranking's order -- graded inspection first, then critical violations, then
    code -- so it does not reshuffle under a user with the detail panel open.

    Scope is the *date*, not the ``inspection_type``: a restaurant can be inspected
    twice in one day (a graded visit plus an ancillary Smoke-Free Air Act /
    Administrative Miscellaneous one) and both sets of violations were cited that day.
    Rows carrying neither a code nor a description are DOHMH's "nothing cited" marker,
    not a violation, so they are omitted -- that is what makes ``violations`` empty for
    a clean inspection. Rows identical to one already listed are DOHMH's own duplicate
    publications of a single violation and are listed once.
    """
    winner = ranked_group[0]
    cited = [
        InspectionViolation(
            code=r.violation_code,
            description=r.violation_description,
            critical_flag=r.critical_flag,
        )
        for r in ranked_group
        if r.inspection_date == winner.inspection_date
        and (r.violation_code is not None or r.violation_description is not None)
    ]
    unique = list({(v.code, v.description, v.critical_flag): v for v in cited}.values())
    return winner.model_copy(update={"violations": unique})


class InspectionsAdapter(_SocrataAdapter[RestaurantInspection]):
    """One record per restaurant -- its most recent inspection -- over the last 90 days.

    The upstream rows are one-per-violation-per-visit; ``post_process`` collapses
    them by ``camis`` using :func:`inspection_rank` and keeps the discarded rows'
    information in the survivor's ``violations`` list (:func:`_collapse_visit`).
    All matching rows are still fetched -- they are what the ranking chooses
    between and what fills ``violations``, and Socrata offers no server-side "row
    per group" without dropping to the newer pipe/window-function SoQL that is
    mutually exclusive with ``$select``/``$where``/``$offset`` paging.

    The dataset marks never-inspected venues with ``inspection_date = 1900-01-01``;
    the 90-day window excludes them. Rows without coordinates, or with the
    ``0,0`` placeholder, are dropped and counted.
    """

    name = FeedName.DOHMH_INSPECTIONS
    dataset = DATASET_INSPECTIONS
    # Not the dedup rule -- that is `inspection_rank`, applied client-side so the
    # snapshot does not depend on Socrata's ordering. This exists only to make
    # `$offset` paging stable: (inspection_date, camis, violation_code) is unique
    # across the window apart from DOHMH's own byte-identical duplicate rows.
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

    def post_process(self, records: list[RestaurantInspection]) -> list[RestaurantInspection]:
        """Collapse to one record per ``camis``: the winner under :func:`inspection_rank`,
        carrying its visit's violations."""
        ranked = sorted(records, key=lambda r: (r.camis, inspection_rank(r)))
        winners = [
            _collapse_visit(list(group)) for _, group in groupby(ranked, key=lambda r: r.camis)
        ]
        collapsed = sorted(winners, key=_display_order)
        dropped = len(records) - len(collapsed)
        if dropped:
            log.info(
                "%s: collapsed %d violation rows into %d restaurants (%d duplicate rows dropped)",
                self.name.value,
                len(records),
                len(collapsed),
                dropped,
            )
        return collapsed


__all__ = [
    "CRITICAL_FLAG",
    "DATASET_311",
    "DATASET_INSPECTIONS",
    "NYC_TZ",
    "InspectionsAdapter",
    "Nyc311Adapter",
    "format_floating_timestamp",
    "inspection_rank",
    "parse_floating_timestamp",
    "soda_fetch_all",
    "soda_headers",
    "soda_resource_url",
]
