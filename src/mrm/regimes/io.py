"""Read regime inputs from ``macro_asof`` and write ``fact_regimes``.

The only module of ``mrm.regimes`` that touches the database. Callers must run
``mrm.storage.clean.prepare_clean_prices(con)`` first (it syncs config/macro_lags.yaml, without
which ``macro_asof`` is empty). Never reads ``raw_*``.
"""

from collections.abc import Collection, Sequence
from datetime import date, datetime

import duckdb
import pandas as pd

from mrm.quality.calendar import is_nyse_business_day
from mrm.regimes.rules import COLUMNS
from mrm.regimes.v2 import COLUMNS_V2

INPUT_COLUMNS: tuple[str, ...] = ("value", "value_date", "available_date", "age_days")


def load_regime_inputs(
    con: duckdb.DuckDBPyConnection,
    series_ids: Sequence[str],
    special_closures: Collection[date] = (),
) -> tuple[dict[str, pd.DataFrame], int]:
    """Point-in-time frames per series (index trade_date, columns ``INPUT_COLUMNS``) on the
    NYSE trading days of the ``macro_asof`` grid, and the number of off-calendar days dropped.

    The grid is the distinct ``prices_clean`` trade dates; dates that are not NYSE trading days
    (``mrm.quality.calendar``) are dropped, as in ``mrm metrics``. Raises ValueError if a
    series is not exposed by ``macro_asof`` (missing from macro_publication_lags).
    """
    exposed = {r[0] for r in con.execute("SELECT series_id FROM macro_publication_lags").fetchall()}
    if missing := sorted(set(series_ids) - exposed):
        raise ValueError(f"series not in macro_asof: {', '.join(missing)}")
    placeholders = ", ".join("?" for _ in series_ids)
    long = con.execute(
        "SELECT trade_date, series_id, value, value_date, available_date, age_days "
        f"FROM macro_asof WHERE series_id IN ({placeholders}) ORDER BY trade_date",
        list(series_ids),
    ).df()
    days = pd.to_datetime(long["trade_date"]).dt.date
    on_calendar = {d: is_nyse_business_day(d, special_closures) for d in set(days)}
    keep = days.map(on_calendar).astype(bool).to_numpy(dtype=bool)
    discarded = len({d for d, ok in on_calendar.items() if not ok})
    long = long[keep]

    frames: dict[str, pd.DataFrame] = {}
    for series_id in series_ids:
        part = long[long["series_id"] == series_id]
        frame = pd.DataFrame(
            {
                "value": part["value"].astype(float).to_numpy(),
                "value_date": pd.to_datetime(part["value_date"]).to_numpy(),
                "available_date": pd.to_datetime(part["available_date"]).to_numpy(),
                "age_days": part["age_days"].astype("Int64").to_numpy(),
            },
            index=pd.DatetimeIndex(pd.to_datetime(part["trade_date"]), name="trade_date"),
        )
        frames[series_id] = frame
    return frames, discarded


def write_regimes(
    con: duckdb.DuckDBPyConnection, regimes: pd.DataFrame, computed_at: datetime
) -> None:
    """Replace every row of ``fact_regimes`` in one transaction (full rebuild)."""
    con.register("new_regimes", regimes[list(COLUMNS)])
    columns = ", ".join(COLUMNS)
    dates = {c for c in COLUMNS if c == "trade_date" or c.endswith("_date")}
    select = ", ".join(f"CAST({c} AS DATE)" if c in dates else c for c in COLUMNS)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM fact_regimes")
        con.execute(
            f"INSERT INTO fact_regimes ({columns}, computed_at) "
            f"SELECT {select}, ? FROM new_regimes",
            [computed_at],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("new_regimes")


def read_regimes(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """``fact_regimes`` ordered by trade_date (columns ``COLUMNS``, no computed_at)."""
    return con.execute(f"SELECT {', '.join(COLUMNS)} FROM fact_regimes ORDER BY trade_date").df()


def write_regimes_v2(
    con: duckdb.DuckDBPyConnection, frame: pd.DataFrame, computed_at: datetime
) -> None:
    """Replace every row of ``fact_regimes_v2`` in one transaction (full rebuild)."""
    con.register("new_regimes_v2", frame[list(COLUMNS_V2)])
    columns = ", ".join(COLUMNS_V2)
    casts = {"trade_date": "DATE", "train_end_date": "DATE", "train_rows": "INTEGER"}
    select = ", ".join(f"CAST({c} AS {casts[c]})" if c in casts else c for c in COLUMNS_V2)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM fact_regimes_v2")
        con.execute(
            f"INSERT INTO fact_regimes_v2 ({columns}, computed_at) "
            f"SELECT {select}, ? FROM new_regimes_v2",
            [computed_at],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.unregister("new_regimes_v2")


def read_regimes_v2(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """``fact_regimes_v2`` ordered by variant (point_in_time first) and trade_date."""
    return con.execute(
        f"SELECT {', '.join(COLUMNS_V2)} FROM fact_regimes_v2 "
        "ORDER BY variant = 'full_sample', trade_date"
    ).df()
