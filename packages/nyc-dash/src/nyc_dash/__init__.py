"""nyc-dash: FastAPI + MapLibre GL / deck.gl dashboard over the nyc-live service layer.

`create_app()` builds the API and mounts the static single-page frontend.
Everything it serves comes from `nyc_live.services`; this package never fetches an
upstream and never fabricates a record.
"""

from nyc_dash.app import STATIC_DIR, create_app

__version__ = "0.1.0"

__all__ = ["STATIC_DIR", "__version__", "create_app"]
