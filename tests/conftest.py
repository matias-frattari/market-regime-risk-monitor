from collections.abc import Iterator

import duckdb
import pytest

from mrm.storage.db import connect
from mrm.storage.schema import create_schema


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    """In-memory warehouse with the schema created."""
    connection = connect(":memory:")
    create_schema(connection)
    yield connection
    connection.close()
