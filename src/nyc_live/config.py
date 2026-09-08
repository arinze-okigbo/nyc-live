"""Runtime settings. All secrets come from env / .env; nothing is hardcoded."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    user_agent: str = Field(
        default="nyc-live/0.1 (https://github.com/arinze-okigbo/nyc-live)",
        alias="NYC_LIVE_USER_AGENT",
        description="weather.gov rejects requests without a descriptive User-Agent.",
    )
    data_dir: Path = Field(default=Path("./data"), alias="NYC_LIVE_DATA_DIR")
    duckdb_path: Path = Field(default=Path("./data/nyc_live.duckdb"), alias="NYC_LIVE_DUCKDB_PATH")
    http_timeout_s: float = Field(default=15.0, alias="NYC_LIVE_HTTP_TIMEOUT_S")
    http_retries: int = Field(default=2, alias="NYC_LIVE_HTTP_RETRIES", ge=0, le=5)

    socrata_app_token: str | None = Field(default=None, alias="SOCRATA_APP_TOKEN")
    mta_bus_time_api_key: str | None = Field(default=None, alias="MTA_BUS_TIME_API_KEY")
    ny511_api_key: str | None = Field(default=None, alias="NY511_API_KEY")

    # upstream base URLs (verified in the brief; override only for tests)
    dot_cameras_base: str = Field(
        default="https://webcams.nyctmc.org/api/cameras", alias="NYC_LIVE_DOT_CAMERAS_BASE"
    )
    mta_gtfs_base: str = Field(
        default="https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds",
        alias="NYC_LIVE_MTA_GTFS_BASE",
    )
    mta_static_gtfs_url: str = Field(
        default="https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
        alias="NYC_LIVE_MTA_STATIC_GTFS_URL",
        description=(
            "Static GTFS zip for subway stops. Overridable like every other upstream so "
            "mta_subway_stops can be redirected or killed by env; the legacy web.mta.info "
            "URL now returns 403 from MTA."
        ),
    )
    citibike_gbfs_root: str = Field(
        default="https://gbfs.citibikenyc.com/gbfs/gbfs.json", alias="NYC_LIVE_CITIBIKE_GBFS_ROOT"
    )
    socrata_base: str = Field(
        default="https://data.cityofnewyork.us", alias="NYC_LIVE_SOCRATA_BASE"
    )
    weather_base: str = Field(default="https://api.weather.gov", alias="NYC_LIVE_WEATHER_BASE")

    live_tests: bool = Field(default=False, alias="NYC_LIVE_TESTS")

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
