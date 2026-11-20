"""Immutable data transfer objects (DTOs) and value objects for batch scraping."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scrape.classifiers import ClassifiedLink


@dataclass(frozen=True)
class ScrapeItemResult:
    """Immutable outcome of scraping a single ClassifiedLink."""

    link: ClassifiedLink
    output_path: Path | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return True when the link was scraped without error and produced an output path."""
        return self.error is None and self.output_path is not None

    @classmethod
    def failure(cls, link: ClassifiedLink, exc: BaseException) -> ScrapeItemResult:
        """Build a failed item result from an exception."""
        error_msg = str(exc).strip() or type(exc).__name__
        return cls(link=link, output_path=None, error=error_msg)


@dataclass(frozen=True)
class BatchScrapeResult:
    """Immutable aggregate outcome of a 3-phase batch scrape."""

    raw_urls: list[str]
    classified_links: list[ClassifiedLink]
    results: list[ScrapeItemResult]
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if not item.succeeded)

    @property
    def error_count(self) -> int:
        return len(self.errors)


@dataclass(frozen=True)
class ScrapePass:
    """Immutable contiguous slice of ordered links sharing one browser launch mode."""

    links: tuple[ClassifiedLink, ...]
    headless: bool
    clean: bool = False

    @property
    def mode_label(self) -> str:
        """Human-readable label for the pass's browser mode."""
        return "headless" if self.headless else "headed"
