"""Clean layer: prepare the ``*_clean`` views that every downstream module reads.

Layers: ``raw_*`` tables (provider data, untouched) -> ``*_clean`` views (documented,
reversible corrections, one row per raw row, nothing imputed) -> ``*_asof`` views
(point-in-time on the trading calendar; today ``macro_asof`` for the daily FRED series).
``create_schema`` also creates ``macro_asof``, so calling ``prepare_clean_prices`` first is
enough before reading it.
"""

from collections.abc import Sequence
from pathlib import Path

import duckdb

from mrm.config import load_price_adjustments
from mrm.storage.loaders import sync_price_adjustments
from mrm.storage.models import PriceAdjustment
from mrm.storage.schema import create_schema


def prepare_clean_prices(
    con: duckdb.DuckDBPyConnection,
    path: Path | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
) -> int:
    """Create the schema (incl. ``prices_clean``) and sync ``config/price_adjustments.yaml``
    (or the given ``adjustments``, for tests).

    Returns the number of adjustments synced. Call before reading ``prices_clean``.
    Raises ValueError on a malformed config, before anything is written.
    """
    items = load_price_adjustments(path) if adjustments is None else adjustments
    create_schema(con)
    return sync_price_adjustments(con, items)
