"""Clean layer: prepare the ``*_clean`` views that every downstream module reads.

Layers: ``raw_*`` tables (provider data, untouched) -> ``*_clean`` views (documented,
reversible corrections, one row per raw row, nothing imputed) -> ``*_asof`` views
(point-in-time, publication lags; not built yet).
"""

from pathlib import Path

import duckdb

from mrm.config import load_price_adjustments
from mrm.storage.loaders import sync_price_adjustments
from mrm.storage.schema import create_schema


def prepare_clean_prices(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> int:
    """Create the schema (incl. ``prices_clean``) and sync ``config/price_adjustments.yaml``.

    Returns the number of configured adjustments. Call before reading ``prices_clean``.
    """
    create_schema(con)
    return sync_price_adjustments(con, load_price_adjustments(path))
