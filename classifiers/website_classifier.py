"""Root classifier (Stage 1) for determining WebsiteType from URLs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from pathlib import Path
from urllib.parse import ParseResult, urlparse

from rich.console import Console

from classifiers.input_parser import InputParser, LinkInputParser
from classifiers.models import ClassifiedLink, WebsiteType

console = Console()


def log_classification_warning(message: str) -> None:
    """Log a classification warning message in bold orange using rich."""
    console.print(f"[bold orange1]{message}[/bold orange1]")


class WebsiteMatchingStrategy(ABC):
    """Strategy interface for matching a parsed URL to a WebsiteType."""

    @property
    @abstractmethod
    def website_type(self) -> WebsiteType:
        """The WebsiteType associated with this strategy."""

    @abstractmethod
    def matches(self, parsed_url: ParseResult) -> bool:
        """Return True if the parsed URL belongs to this website."""


class GoogleDriveWebsiteStrategy(WebsiteMatchingStrategy):
    """Matches Google Drive URLs (drive.google.com)."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DRIVE

    def matches(self, parsed_url: ParseResult) -> bool:
        hostname = (parsed_url.hostname or "").lower()
        return hostname == "drive.google.com" or hostname.endswith(".drive.google.com")


class GoogleDocWebsiteStrategy(WebsiteMatchingStrategy):
    """Matches Google Docs, Sheets, and Slides URLs (docs.google.com)."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DOC

    def matches(self, parsed_url: ParseResult) -> bool:
        hostname = (parsed_url.hostname or "").lower()
        return hostname == "docs.google.com" or hostname.endswith(".docs.google.com")


class ScribdWebsiteStrategy(WebsiteMatchingStrategy):
    """Matches Scribd URLs (scribd.com, www.scribd.com, and regional subdomains)."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.SCRIBD

    def matches(self, parsed_url: ParseResult) -> bool:
        hostname = (parsed_url.hostname or "").lower()
        return hostname == "scribd.com" or hostname.endswith(".scribd.com")


class WebsiteClassifier:
    """Stage 1 classifier that parses input links and classifies each URL's WebsiteType."""

    def __init__(
        self,
        strategies: Iterable[WebsiteMatchingStrategy] | None = None,
        input_parser: InputParser | None = None,
    ) -> None:
        self._strategies: list[WebsiteMatchingStrategy] = (
            list(strategies)
            if strategies is not None
            else [
                GoogleDriveWebsiteStrategy(),
                GoogleDocWebsiteStrategy(),
                ScribdWebsiteStrategy(),
            ]
        )
        self._input_parser: InputParser = input_parser or LinkInputParser()

    def register_strategy(self, strategy: WebsiteMatchingStrategy) -> None:
        """Register an additional website matching strategy."""
        self._strategies.append(strategy)

    def classify_url(
        self, url: str, *, warn_on_invalid: bool = True
    ) -> ClassifiedLink | None:
        """Validate a single URL string and classify its WebsiteType.

        Returns a ``ClassifiedLink`` with ``document_type=None``, or ``None`` if
        the URL is malformed or belongs to an unsupported domain.
        """
        normalized = url.strip()
        if (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in {'"', "'"}
        ):
            normalized = normalized[1:-1].strip()

        if not normalized:
            return None

        try:
            parsed = urlparse(normalized)
        except Exception as exc:
            if warn_on_invalid:
                log_classification_warning(f"Malformed URL skipped: {url!r} ({exc})")
            return None

        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            if warn_on_invalid:
                log_classification_warning(
                    f"Invalid URL skipped (must be http/https with valid domain): {normalized}"
                )
            return None

        for strategy in self._strategies:
            if strategy.matches(parsed):
                return ClassifiedLink(
                    url=normalized,
                    website_type=strategy.website_type,
                    document_type=None,
                )

        if warn_on_invalid:
            log_classification_warning(
                f"Unsupported website domain skipped: {normalized}"
            )
        return None

    def match_website_type(self, url: str) -> WebsiteType | None:
        """Return the WebsiteType for a single URL without logging warnings, or None."""
        classified = self.classify_url(url, warn_on_invalid=False)
        return classified.website_type if classified is not None else None

    def classify(self, source: Path | str | Sequence[str]) -> list[ClassifiedLink]:
        """Parse URLs from a .txt file path or sequence of strings and classify their WebsiteType."""
        raw_urls = self._input_parser.parse(source)
        classified_links: list[ClassifiedLink] = []
        for raw_url in raw_urls:
            classified = self.classify_url(raw_url, warn_on_invalid=True)
            if classified is not None:
                classified_links.append(classified)
        return classified_links


def classify_website(url: str) -> WebsiteType | None:
    """Convenience function to determine the WebsiteType of a single URL."""
    return WebsiteClassifier().match_website_type(url)


def classify_websites(source: Path | str | Sequence[str]) -> list[ClassifiedLink]:
    """Convenience function to parse and classify URLs into ClassifiedLink objects."""
    return WebsiteClassifier().classify(source)
