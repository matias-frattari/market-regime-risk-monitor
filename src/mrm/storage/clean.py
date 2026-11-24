"""Clean layer: prepare the ``*_clean`` and ``*_asof`` views that every downstream module reads.

Layers: ``raw_*`` tables (provider data, untouched) -> ``*_clean`` views (documented,
reversible corrections, one row per raw row, nothing imputed) -> ``*_asof`` views
(point-in-time on the trading calendar: ``macro_asof``, with the publication lags of
``config/macro_lags.yaml``, ADR 0005). ``create_schema`` creates the views and this module
syncs their config tables, so calling ``prepare_clean_prices`` first is enough before reading
``prices_clean`` or ``macro_asof``.
"""

from collections.abc import Sequence
from pathlib import Path

import duckdb

from mrm.config import load_macro_lags, load_price_adjustments
from mrm.storage.loaders import sync_macro_lags, sync_price_adjustments
from mrm.storage.models import MacroLag, PriceAdjustment
from mrm.storage.schema import create_schema


def prepare_clean_prices(
    con: duckdb.DuckDBPyConnection,
    path: Path | None = None,
    adjustments: Sequence[PriceAdjustment] | None = None,
    macro_lags: Sequence[MacroLag] | None = None,
) -> int:
    """Create the schema (incl. ``prices_clean`` and ``macro_asof``), sync
    ``config/price_adjustments.yaml`` (or the given ``adjustments``) and
    ``config/macro_lags.yaml`` (or the given ``macro_lags``, for tests).

    Returns the number of price adjustments synced. Call before reading ``prices_clean`` or
    ``macro_asof``. Raises ValueError on a malformed config, before anything is written.
    """
    items = load_price_adjustments(path) if adjustments is None else adjustments
    lags = load_macro_lags() if macro_lags is None else macro_lags
    create_schema(con)
    sync_macro_lags(con, lags)
    return sync_price_adjustments(con, items)
