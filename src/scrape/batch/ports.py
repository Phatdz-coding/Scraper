"""Abstract protocol ports for the 3-phase batch scraper pipeline (ISP & DIP)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol, TypeAlias, runtime_checkable

from playwright.async_api import Browser as AsyncBrowser

from scrape.batch.models import BatchScrapeResult, ScrapeItemResult, ScrapePass
from scrape.classifiers import ClassifiedLink
from scrape.config import BatchConfig

BrowserFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[AsyncBrowser]]
BrowserLauncher: TypeAlias = Callable[..., AbstractAsyncContextManager[AsyncBrowser]]


@runtime_checkable
class LinkFilterer(Protocol):
    """Phase 1 port: parses raw input and pre-classifies links without a browser."""

    def filter_and_classify(
        self, source: Path | str | Sequence[str]
    ) -> tuple[list[str], list[ClassifiedLink]]: ...


@runtime_checkable
class FolderExpander(Protocol):
    """Phase 2 port: expands DocumentType.FOLDER links into contained file links."""

    async def expand(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None = None,
    ) -> list[ClassifiedLink]: ...


@runtime_checkable
class LinkScraper(Protocol):
    """Phase 3 port: scrapes a single ClassifiedLink using the shared AsyncBrowser."""

    async def scrape_one(
        self,
        link: ClassifiedLink,
        browser: AsyncBrowser,
        config: BatchConfig,
    ) -> ScrapeItemResult: ...


@runtime_checkable
class BatchReporter(Protocol):
    """Observer port for reporting batch lifecycle progress and final summary."""

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None: ...

    def on_phase2_start(self, folder_count: int) -> None: ...

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None: ...

    def on_phase3_start(self, total: int) -> None: ...

    def on_item_start(self, link: ClassifiedLink) -> None: ...

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None: ...

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None: ...


@runtime_checkable
class BrowserSession(Protocol):
    """Port that hands out a browser for a requested launch mode."""

    @property
    def active_browser(self) -> AsyncBrowser | None: ...

    @property
    def can_switch_mode(self) -> bool:
        """True when acquire() honours the requested launch mode (may relaunch)."""
        ...

    async def acquire(
        self, headless: bool, *, clean: bool = False
    ) -> AsyncBrowser: ...


@runtime_checkable
class BrowserModePolicy(Protocol):
    """Strategy port for ordering links and planning headless vs. headed passes."""

    def requires_headed(self, link: ClassifiedLink) -> bool: ...

    def order(self, links: Sequence[ClassifiedLink]) -> list[ClassifiedLink]: ...

    def plan(
        self, ordered_links: Sequence[ClassifiedLink], *, headless: bool
    ) -> list[ScrapePass]: ...
