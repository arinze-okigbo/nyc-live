"""Frozen shared contracts for nyc-live.

FREEZE RULE
-----------
This file is frozen after Phase 0. Every parallel agent codes against it and
none of them may edit it. If you need a change (new field, new table, new
enum member), STOP and report the exact change you need to the orchestrator.
Do not work around a missing field with `extra` data or a side channel.

CONVENTIONS
-----------
* All datetimes are timezone-aware UTC (`AwareDatetime`).
* Every record type is a frozen Pydantic model with `extra="forbid"`: adapters
  must map upstream fields explicitly, never pass raw payloads through.
* Every tool / API response is an `Envelope` and always carries `status`,
  `fetched_at`, `stale_after`, and `error`. Consumers must check `status`.
  `status == "error"` implies `records == []` and `error is not None`.
  There is no such thing as a silent empty result.
* Feeds never fabricate data. A missing upstream is an `Envelope(status="error")`
  or a raised `FeedUnavailable`, never synthetic fill.
* Geo filtering is done in the service layer over whole snapshots, so adapters
  stay parameterless and cacheable. Located records get `distance_m` set when a
  `GeoQuery` was applied.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

CONTRACTS_VERSION = "0.1.0"
SCHEMA_VERSION = 1


def now_utc() -> datetime:
    """The one clock every module uses. Patch this in tests, never `datetime.now`."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Feed identity, cadence, privacy constants
# ---------------------------------------------------------------------------


class FeedName(StrEnum):
    DOT_CAMERAS = "dot_cameras"
    DOT_CAMERA_FRAMES = "dot_camera_frames"
    MTA_SUBWAY = "mta_subway"  # trip updates + vehicle positions, all NYCT feeds merged
    MTA_SUBWAY_ALERTS = "mta_subway_alerts"
    MTA_SUBWAY_STOPS = "mta_subway_stops"  # static GTFS stops, needed to place trains on a map
    CITIBIKE = "citibike"
    NYC_311 = "nyc_311"
    DOHMH_INSPECTIONS = "dohmh_inspections"
    WEATHER = "weather"
    MTA_BUS = "mta_bus"  # deferred: key-gated, adapter stub only
    NY511_CAMERAS = "ny511_cameras"  # deferred: key-gated, adapter stub only
    DENSITY = "density"  # derived by nyc-vision, served from DuckDB
    WAREHOUSE = "warehouse"  # query_warehouse tool


DEFAULT_TTL: dict[FeedName, timedelta] = {
    FeedName.DOT_CAMERAS: timedelta(minutes=10),
    FeedName.DOT_CAMERA_FRAMES: timedelta(seconds=2),
    FeedName.MTA_SUBWAY: timedelta(seconds=30),
    FeedName.MTA_SUBWAY_ALERTS: timedelta(seconds=60),
    FeedName.MTA_SUBWAY_STOPS: timedelta(hours=24),
    FeedName.CITIBIKE: timedelta(seconds=60),
    FeedName.NYC_311: timedelta(minutes=5),
    FeedName.DOHMH_INSPECTIONS: timedelta(hours=6),
    FeedName.WEATHER: timedelta(minutes=5),
    FeedName.MTA_BUS: timedelta(seconds=30),
    FeedName.NY511_CAMERAS: timedelta(minutes=10),
    FeedName.DENSITY: timedelta(seconds=60),
    FeedName.WAREHOUSE: timedelta(seconds=0),
}
"""Minimum poll interval AND the freshness window used to compute `stale_after`."""

CAMERA_FRAME_MIN_INTERVAL = timedelta(seconds=2)
"""DOT frames refresh about every 2 s. Never fetch the same camera faster than this."""

FRAME_BUFFER_MAX_FRAMES_PER_CAMERA = 1
FRAME_BUFFER_MAX_AGE = timedelta(minutes=10)
"""Privacy: raw frames live only in memory, one per camera, evicted after this age.
Frames are never written to disk. See docs/privacy.md."""

NYC_CENTER = (40.7128, -74.0060)
NYC_BBOX = (40.4774, -74.2591, 40.9176, -73.7004)
"""(min_lat, min_lon, max_lat, max_lon). Used for sanity-checking upstream coordinates."""


# ---------------------------------------------------------------------------
# Base models
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, ser_json_bytes="base64")


class Located(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    distance_m: float | None = Field(
        default=None, description="Set only when the result was filtered by a GeoQuery."
    )


class MaybeLocated(StrictModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    distance_m: float | None = None


@runtime_checkable
class HasLatLon(Protocol):
    @property
    def lat(self) -> float | None: ...
    @property
    def lon(self) -> float | None: ...


class GeoQuery(StrictModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=1000, gt=0, le=50_000)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorKind(StrEnum):
    NOT_CONFIGURED = "not_configured"  # required key missing; feed intentionally skipped
    UPSTREAM_HTTP = "upstream_http"  # non-2xx from upstream
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_PARSE = "upstream_parse"  # body did not match expected shape
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"  # e.g. unknown camera id
    INTERNAL = "internal"


class FeedError(StrictModel):
    kind: ErrorKind
    message: str
    feed: FeedName
    url: str | None = None
    upstream_status: int | None = None
    occurred_at: AwareDatetime
    retry_after_s: float | None = None


class FeedUnavailable(Exception):
    """Raised by adapters when a feed cannot be served. Always loud, never empty."""

    def __init__(
        self,
        feed: FeedName,
        message: str,
        *,
        kind: ErrorKind = ErrorKind.UPSTREAM_HTTP,
        url: str | None = None,
        upstream_status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{feed.value}: {message}")
        self.feed = feed
        self.message = message
        self.kind = kind
        self.url = url
        self.upstream_status = upstream_status
        self.retry_after_s = retry_after_s

    def to_model(self) -> FeedError:
        return FeedError(
            kind=self.kind,
            message=self.message,
            feed=self.feed,
            url=self.url,
            upstream_status=self.upstream_status,
            occurred_at=now_utc(),
            retry_after_s=self.retry_after_s,
        )


class FeedNotConfigured(FeedUnavailable):
    """A key-gated feed whose key is absent. Adapters raise this from fetch()."""

    def __init__(self, feed: FeedName, env_var: str) -> None:
        super().__init__(feed, f"{env_var} is not set; feed skipped", kind=ErrorKind.NOT_CONFIGURED)
        self.env_var = env_var


# ---------------------------------------------------------------------------
# Snapshot (adapter output) and Envelope (tool / API output)
# ---------------------------------------------------------------------------


class Snapshot[RecordT: BaseModel](StrictModel):
    """One successful fetch of a whole feed. Adapters return this or raise."""

    feed: FeedName
    fetched_at: AwareDatetime
    stale_after: AwareDatetime
    source_url: str
    records: list[RecordT]
    upstream_generated_at: AwareDatetime | None = Field(
        default=None,
        description="Upstream's own timestamp (GTFS header, GBFS last_updated) when present.",
    )
    latency_ms: float | None = None


FeedStatus = Literal["fresh", "stale", "error"]


class Envelope[RecordT: BaseModel](StrictModel):
    """What every MCP tool and dashboard endpoint returns."""

    feed: FeedName
    status: FeedStatus
    fetched_at: AwareDatetime | None
    stale_after: AwareDatetime | None
    records: list[RecordT]
    error: FeedError | None = None
    query: GeoQuery | None = None
    total_before_filter: int | None = Field(
        default=None, description="Snapshot size before geo/limit filtering."
    )
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status != "error"


class FeedHealth(StrictModel):
    feed: FeedName
    configured: bool
    status: FeedStatus | Literal["never_fetched"]
    ttl_s: float
    last_ok_at: AwareDatetime | None = None
    last_error: FeedError | None = None
    consecutive_failures: int = 0
    record_count: int | None = None


# ---------------------------------------------------------------------------
# Adapter protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class FeedAdapter[RecordT: BaseModel](Protocol):
    """One upstream, one record type, no parameters.

    Implementations are constructed as `Adapter(client=httpx.AsyncClient, settings=Settings)`.
    `fetch()` returns a full Snapshot or raises FeedUnavailable. It must honour
    `DEFAULT_TTL[name]` as its own minimum upstream interval (the cache also
    enforces it, belt and braces). Never return an empty Snapshot when the
    upstream failed; raise.
    """

    name: FeedName
    ttl: timedelta

    def is_configured(self) -> bool: ...

    async def fetch(self) -> Snapshot[RecordT]: ...


@runtime_checkable
class FrameSource(Protocol):
    """Per-camera JPEG fetch with the 2 s cadence enforced per camera id."""

    async def get_frame(self, camera_id: str) -> CameraFrame: ...


# ---------------------------------------------------------------------------
# Record types: cameras
# ---------------------------------------------------------------------------


class CameraSource(StrEnum):
    NYC_DOT = "nyc_dot"
    NY511 = "ny511"


class Camera(Located):
    id: str = Field(description="Upstream id. DOT ids are UUIDs and rotate; never hardcode.")
    source: CameraSource
    name: str
    is_online: bool
    image_url: str
    area: str | None = None
    roadway: str | None = None
    direction: str | None = None


class CameraFrame(StrictModel):
    camera_id: str
    fetched_at: AwareDatetime
    stale_after: AwareDatetime
    content_type: str
    data: bytes = Field(repr=False, exclude=True)
    byte_size: int
    width: int | None = None
    height: int | None = None


class CameraFrameFetch(StrictModel):
    """Telemetry row for the <2 % frame-fetch failure gate."""

    camera_id: str
    ts: AwareDatetime
    ok: bool
    status_code: int | None = None
    latency_ms: float | None = None
    byte_size: int | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Record types: subway
# ---------------------------------------------------------------------------

SubwayDirection = Literal["N", "S"]


class VehicleStatus(StrEnum):
    INCOMING_AT = "INCOMING_AT"
    STOPPED_AT = "STOPPED_AT"
    IN_TRANSIT_TO = "IN_TRANSIT_TO"


class StopTimeUpdate(StrictModel):
    stop_id: str
    arrival: AwareDatetime | None = None
    departure: AwareDatetime | None = None


class VehiclePosition(StrictModel):
    current_stop_id: str | None
    status: VehicleStatus | None
    timestamp: AwareDatetime | None
    current_stop_sequence: int | None = None


class SubwayTrip(StrictModel):
    trip_id: str
    route_id: str
    feed_slug: str = Field(description="e.g. 'gtfs-ace'; verified at runtime, 404s are fatal.")
    direction: SubwayDirection | None = None
    start_date: str | None = None
    stop_times: list[StopTimeUpdate]
    vehicle: VehiclePosition | None = None


class SubwayStop(Located):
    stop_id: str
    name: str
    parent_station: str | None = None
    routes: list[str] = Field(default_factory=list)


class SubwayAlert(StrictModel):
    id: str
    header: str
    description: str | None = None
    active_from: AwareDatetime | None = None
    active_until: AwareDatetime | None = None
    routes: list[str] = Field(default_factory=list)
    stop_ids: list[str] = Field(default_factory=list)
    effect: str | None = None
    updated_at: AwareDatetime | None = None


class SubwayArrival(StrictModel):
    """Derived, per-stop view served by `subway_arrivals`. Built by the service layer."""

    stop_id: str
    stop_name: str | None
    route_id: str
    trip_id: str
    direction: SubwayDirection | None
    arrival: AwareDatetime
    eta_s: float
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None


# ---------------------------------------------------------------------------
# Record types: bus (deferred, key-gated)
# ---------------------------------------------------------------------------


class BusVehicle(Located):
    vehicle_id: str
    route_id: str | None
    trip_id: str | None
    bearing: float | None = None
    timestamp: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Record types: Citi Bike
# ---------------------------------------------------------------------------


class BikeStation(Located):
    station_id: str
    name: str
    capacity: int | None
    bikes_available: int
    ebikes_available: int | None
    docks_available: int
    is_renting: bool
    is_returning: bool
    is_installed: bool = True
    last_reported: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Record types: civic (Socrata) and weather
# ---------------------------------------------------------------------------


class ServiceRequest(MaybeLocated):
    unique_key: str
    created_at: AwareDatetime
    closed_at: AwareDatetime | None
    agency: str
    complaint_type: str
    descriptor: str | None
    status: str | None
    borough: str | None
    incident_zip: str | None = None
    incident_address: str | None = None
    location_type: str | None = None


class RestaurantInspection(MaybeLocated):
    camis: str
    dba: str | None
    boro: str | None
    building: str | None = None
    street: str | None = None
    zipcode: str | None = None
    cuisine: str | None = None
    inspection_date: AwareDatetime | None
    action: str | None = None
    violation_code: str | None = None
    violation_description: str | None = None
    critical_flag: str | None = None
    score: int | None = None
    grade: str | None = None
    grade_date: AwareDatetime | None = None
    inspection_type: str | None = None


class WeatherObservation(StrictModel):
    observed_at: AwareDatetime
    text: str | None
    temperature_c: float | None
    dewpoint_c: float | None = None
    humidity_pct: float | None = None
    wind_speed_kmh: float | None = None
    wind_gust_kmh: float | None = None
    wind_direction_deg: float | None = None
    pressure_pa: float | None = None
    visibility_m: float | None = None
    precip_last_hour_mm: float | None = None


class WeatherForecastPeriod(StrictModel):
    name: str
    start: AwareDatetime
    end: AwareDatetime
    is_daytime: bool
    temperature_c: float | None
    short_forecast: str
    precip_probability_pct: float | None = None
    wind_speed: str | None = None


class WeatherReport(Located):
    station_id: str
    station_name: str
    observation: WeatherObservation
    forecast: list[WeatherForecastPeriod] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Record types: vision / density
# ---------------------------------------------------------------------------


class DetectionClass(StrEnum):
    PERSON = "person"
    BICYCLE = "bicycle"
    CAR = "car"
    MOTORCYCLE = "motorcycle"
    BUS = "bus"
    TRUCK = "truck"


VEHICLE_CLASSES: frozenset[DetectionClass] = frozenset(
    {DetectionClass.CAR, DetectionClass.MOTORCYCLE, DetectionClass.BUS, DetectionClass.TRUCK}
)


class DensitySample(StrictModel):
    """One row of `density_samples`. Counts and box statistics only, never pixels."""

    camera_id: str
    ts: AwareDatetime
    cls: DetectionClass
    count: int = Field(ge=0)
    confidence_mean: float | None = Field(default=None, ge=0, le=1)
    bbox_area_frac_mean: float | None = Field(
        default=None, ge=0, le=1, description="Mean box area as a fraction of frame area."
    )
    model: str
    inference_ms: float | None = None
    frame_w: int | None = None
    frame_h: int | None = None


class CameraDensity(Located):
    """Aggregate served by `density_now` / `density_history`."""

    camera_id: str
    name: str | None
    window_start: AwareDatetime
    window_end: AwareDatetime
    sample_count: int
    person_mean: float
    vehicle_mean: float
    person_max: int
    vehicle_max: int
    latest_ts: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Warehouse
# ---------------------------------------------------------------------------


class WarehouseResult(StrictModel):
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


# ---------------------------------------------------------------------------
# DuckDB schema (applied by nyc_live.store; frozen with this file)
# ---------------------------------------------------------------------------

DUCKDB_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cameras (
        camera_id  VARCHAR PRIMARY KEY,
        source     VARCHAR NOT NULL,
        name       VARCHAR,
        lat        DOUBLE,
        lon        DOUBLE,
        is_online  BOOLEAN,
        first_seen TIMESTAMPTZ NOT NULL,
        last_seen  TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS camera_frame_fetches (
        camera_id   VARCHAR NOT NULL,
        ts          TIMESTAMPTZ NOT NULL,
        ok          BOOLEAN NOT NULL,
        status_code INTEGER,
        latency_ms  DOUBLE,
        byte_size   INTEGER,
        error       VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS density_samples (
        camera_id           VARCHAR NOT NULL,
        ts                  TIMESTAMPTZ NOT NULL,
        class               VARCHAR NOT NULL,
        count               INTEGER NOT NULL,
        confidence_mean     DOUBLE,
        bbox_area_frac_mean DOUBLE,
        model               VARCHAR NOT NULL,
        inference_ms        DOUBLE,
        frame_w             INTEGER,
        frame_h             INTEGER,
        PRIMARY KEY (camera_id, ts, class)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feed_fetches (
        feed         VARCHAR NOT NULL,
        ts           TIMESTAMPTZ NOT NULL,
        ok           BOOLEAN NOT NULL,
        status_code  INTEGER,
        latency_ms   DOUBLE,
        record_count INTEGER,
        error_kind   VARCHAR,
        error        VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bike_station_status (
        station_id       VARCHAR NOT NULL,
        ts               TIMESTAMPTZ NOT NULL,
        bikes_available  INTEGER,
        ebikes_available INTEGER,
        docks_available  INTEGER,
        is_renting       BOOLEAN,
        is_returning     BOOLEAN,
        PRIMARY KEY (station_id, ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS weather_observations (
        station_id          VARCHAR NOT NULL,
        observed_at         TIMESTAMPTZ NOT NULL,
        text                VARCHAR,
        temperature_c       DOUBLE,
        dewpoint_c          DOUBLE,
        humidity_pct        DOUBLE,
        wind_speed_kmh      DOUBLE,
        wind_gust_kmh       DOUBLE,
        wind_direction_deg  DOUBLE,
        pressure_pa         DOUBLE,
        visibility_m        DOUBLE,
        precip_last_hour_mm DOUBLE,
        PRIMARY KEY (station_id, observed_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS service_requests_311 (
        unique_key     VARCHAR PRIMARY KEY,
        created_at     TIMESTAMPTZ NOT NULL,
        closed_at      TIMESTAMPTZ,
        agency         VARCHAR,
        complaint_type VARCHAR,
        descriptor     VARCHAR,
        status         VARCHAR,
        borough        VARCHAR,
        lat            DOUBLE,
        lon            DOUBLE,
        last_seen_at   TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subway_positions (
        trip_id         VARCHAR NOT NULL,
        ts              TIMESTAMPTZ NOT NULL,
        route_id        VARCHAR NOT NULL,
        direction       VARCHAR,
        current_stop_id VARCHAR,
        status          VARCHAR,
        PRIMARY KEY (trip_id, ts)
    )
    """,
)

PARQUET_ARCHIVE_TABLES: tuple[str, ...] = (
    "density_samples",
    "camera_frame_fetches",
    "feed_fetches",
    "bike_station_status",
    "weather_observations",
    "subway_positions",
)
"""Append-only tables archived to Parquet by day under $NYC_LIVE_DATA_DIR/archive/<table>/."""

WAREHOUSE_READ_ONLY_TABLES: tuple[str, ...] = tuple(
    t
    for t in (
        "cameras",
        "camera_frame_fetches",
        "density_samples",
        "feed_fetches",
        "bike_station_status",
        "weather_observations",
        "service_requests_311",
        "subway_positions",
    )
)
"""The only tables `query_warehouse` may reference. Queries are SELECT-only."""
