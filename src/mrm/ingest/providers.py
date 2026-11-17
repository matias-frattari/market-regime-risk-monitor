"""Price provider interface shared by every daily-price source."""

from dataclasses import dataclass, field
from typing import Protocol

from mrm.storage.models import PriceBar

REDACTED = "***"


class ProviderError(Exception):
    """A provider failure whose message is already safe to log (no URLs, no API keys)."""


class RateLimitError(ProviderError):
    """The provider refused the request because of its rate or credit limit."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Validated daily bars for one ticker, ascending by date."""

    ticker: str
    bars: list[PriceBar] = field(default_factory=list)
    dropped_null_close: int = 0
    dropped_partial_bar: bool = False


class PriceProvider(Protocol):
    """Source of full-history, split/dividend-adjusted daily bars."""

    name: str

    def fetch_daily(self, ticker: str) -> FetchResult:
        """Return validated bars for ``ticker``; raise ``ProviderError`` on failure."""
        ...


def redact(text: str, secret: str | None) -> str:
    """Replace every occurrence of ``secret`` in ``text`` (safety net for log messages)."""
    if secret:
        return text.replace(secret, REDACTED)
    return text
