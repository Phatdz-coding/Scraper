"""High-level 3-phase batch scraper orchestrator coordinating via abstract ports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import inspect
from pathlib import Path
from typing import TYPE_CHECKING

from scrape.batch import browser as _browser_module
from scrape.batch.adapters import (
    ClassifierLinkFilterer,
    DriveFolderExpander,
    PlaywrightAsyncLinkScraper,
)
from scrape.batch.browser import (
    FixedBrowserSession,
    HeadedBrowserPolicy,
    RelaunchableBrowserSession,
)
from scrape.batch.models import BatchScrapeResult, ScrapeItemResult, ScrapePass
from scrape.batch.ports import (
    BatchReporter,
    BrowserFactory,
    BrowserLauncher,
    BrowserModePolicy,
    BrowserSession,
    FolderExpander,
    LinkFilterer,
    LinkScraper,
)
from scrape.batch.reporters import NullBatchReporter, _ProgressCounter
from scrape.classifiers import ClassifiedLink, DocumentType
from scrape.config import BatchConfig
from scrape.tracker import (
    BatchErrorCollector,
    batch_error_context,
    suppress_live_progress,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser as AsyncBrowser


class BatchScraper:
    """Orchestrates Phase 1 (filter), Phase 2 (expand folders), and Phase 3 (concurrent scrape)."""

    def __init__(
        self,
        filterer: LinkFilterer | None = None,
        folder_expander: FolderExpander | None = None,
        scraper: LinkScraper | None = None,
        config: BatchConfig | None = None,
        *,
        reporter: BatchReporter | None = None,
        browser_factory: BrowserFactory | None = None,
        headed_policy: BrowserModePolicy | None = None,
        browser_launcher: BrowserLauncher | None = None,
    ) -> None:
        self._config = config or BatchConfig()
        self._filterer: LinkFilterer = filterer or ClassifierLinkFilterer()
        self._folder_expander: FolderExpander = (
            folder_expander or DriveFolderExpander()
        )
        self._scraper: LinkScraper = scraper or PlaywrightAsyncLinkScraper()
        self._reporter: BatchReporter = reporter or NullBatchReporter()
        self._browser_factory = browser_factory
        self._headed_policy: BrowserModePolicy = (
            headed_policy or HeadedBrowserPolicy()
        )
        self._browser_launcher = browser_launcher

    @property
    def config(self) -> BatchConfig:
        return self._config

    def _resolve_browser_launcher(self) -> BrowserLauncher:
        return self._browser_launcher or _browser_module.open_shared_browser

    @asynccontextmanager
    async def _acquire_browser(
        self, browser: AsyncBrowser | None = None
    ) -> AsyncGenerator[AsyncBrowser, None]:
        if browser is not None:
            yield browser
            return
        if self._browser_factory is not None:
            async with self._browser_factory() as managed_browser:
                yield managed_browser
            return
        launcher = self._resolve_browser_launcher()
        async with launcher(
            headless=self._config.headless,
            browser_config=self._config.browser,
        ) as owned_browser:
            yield owned_browser

    def _launch_owned_browser(
        self, headless: bool, clean: bool = False
    ) -> AbstractAsyncContextManager[AsyncBrowser]:
        """Launch an owned shared browser from self._config.browser with the given launch mode."""
        launcher = self._resolve_browser_launcher()
        return launcher(
            headless=headless,
            browser_config=self._config.browser,
            clean=clean,
        )

    @asynccontextmanager
    async def _open_browser_session(
        self, browser: AsyncBrowser | None = None
    ) -> AsyncGenerator[BrowserSession, None]:
        """Yield a fixed session for external/injected browsers, else a lazy relaunchable one."""
        if browser is not None or self._browser_factory is not None:
            async with self._acquire_browser(browser) as external_browser:
                yield FixedBrowserSession(external_browser)
            return
        async with RelaunchableBrowserSession(self._launch_owned_browser) as session:
            yield session

    @staticmethod
    def _count_folders(links: Sequence[ClassifiedLink]) -> int:
        return sum(1 for link in links if link.document_type == DocumentType.FOLDER)

    async def _browser_for_expansion(
        self,
        links: Sequence[ClassifiedLink],
        session: BrowserSession,
    ) -> AsyncBrowser | None:
        """Launch a browser for Phase 2 only when folder links need expanding."""
        if self._count_folders(links) == 0:
            return session.active_browser
        return await session.acquire(self._config.headless)

    async def _expand_links(
        self,
        classified_links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None,
    ) -> list[ClassifiedLink]:
        self._reporter.on_phase2_start(self._count_folders(classified_links))

        sig = inspect.signature(self._folder_expander.expand)
        if "browser" in sig.parameters:
            expanded = await self._folder_expander.expand(
                classified_links, browser=browser
            )
        else:
            expanded = await self._folder_expander.expand(classified_links)  # type: ignore[call-arg]

        self._reporter.on_phase2_complete(expanded)
        return expanded

    @staticmethod
    def _single_pass(
        links: Sequence[ClassifiedLink], headless: bool
    ) -> list[ScrapePass]:
        return [ScrapePass(tuple(links), headless=headless)] if links else []

    def _plan_passes(
        self,
        ordered_links: Sequence[ClassifiedLink],
        session: BrowserSession,
    ) -> list[ScrapePass]:
        """Plan headless/headed passes; non-switchable (external) sessions get one pass."""
        if not session.can_switch_mode:
            return self._single_pass(ordered_links, self._config.headless)
        return self._headed_policy.plan(
            ordered_links, headless=self._config.headless
        )

    @staticmethod
    def _failed_result(link: ClassifiedLink, exc: BaseException) -> ScrapeItemResult:
        return ScrapeItemResult.failure(link, exc)

    def _report_item_complete(
        self, result: ScrapeItemResult, counter: _ProgressCounter
    ) -> None:
        self._reporter.on_item_complete(result, counter.advance(), counter.total)

    async def _scrape_one_safely(
        self, link: ClassifiedLink, browser: AsyncBrowser
    ) -> ScrapeItemResult:
        try:
            return await self._scraper.scrape_one(link, browser, self._config)
        except Exception as exc:
            return self._failed_result(link, exc)

    async def _scrape_links(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        """Scrape links concurrently (bounded by config.concurrency) on one browser."""
        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def _worker(link: ClassifiedLink) -> ScrapeItemResult:
            async with semaphore:
                self._reporter.on_item_start(link)
                result = await self._scrape_one_safely(link, browser)
                self._report_item_complete(result, counter)
                return result

        return list(await asyncio.gather(*(_worker(link) for link in links)))

    def _fail_pass(
        self,
        scrape_pass: ScrapePass,
        exc: BaseException,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        """Mark every link of a pass as failed when its browser could not be launched."""
        reason = str(exc).strip() or type(exc).__name__
        launch_error = RuntimeError(
            f"Failed to launch {scrape_pass.mode_label} browser: {reason}"
        )
        results = [
            self._failed_result(link, launch_error) for link in scrape_pass.links
        ]
        for result in results:
            self._report_item_complete(result, counter)
        return results

    async def _run_pass(
        self,
        scrape_pass: ScrapePass,
        session: BrowserSession,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        try:
            browser = await session.acquire(
                scrape_pass.headless, clean=scrape_pass.clean
            )
        except Exception as exc:
            return self._fail_pass(scrape_pass, exc, counter)
        return await self._scrape_links(scrape_pass.links, browser, counter)

    async def _scrape_passes(
        self,
        passes: Sequence[ScrapePass],
        session: BrowserSession,
    ) -> list[ScrapeItemResult]:
        """Run passes sequentially with one continuous Phase 3 progress bar."""
        counter = _ProgressCounter(sum(len(p.links) for p in passes))
        if counter.total == 0:
            return []

        self._reporter.on_phase3_start(counter.total)
        results: list[ScrapeItemResult] = []
        with suppress_live_progress():
            for scrape_pass in passes:
                results.extend(await self._run_pass(scrape_pass, session, counter))
        return results

    async def _scrape_all_concurrently(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
    ) -> list[ScrapeItemResult]:
        """Single-pass Phase 3 on one given browser (kept for backward compatibility)."""
        return await self._scrape_passes(
            self._single_pass(links, self._config.headless),
            FixedBrowserSession(browser),
        )

    async def _expand_and_scrape(
        self,
        preclassified: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None,
    ) -> tuple[list[ClassifiedLink], list[ScrapeItemResult]]:
        """Phases 2 & 3: expand folders, order links (headed-only last), scrape in passes."""
        async with self._open_browser_session(browser) as session:
            phase2_browser = await self._browser_for_expansion(preclassified, session)
            expanded = await self._expand_links(preclassified, phase2_browser)
            ordered_links = self._headed_policy.order(expanded)
            passes = self._plan_passes(ordered_links, session)
            results = await self._scrape_passes(passes, session)
        return ordered_links, results

    async def run(
        self,
        source: Path | str | Sequence[str],
        *,
        browser: AsyncBrowser | None = None,
    ) -> BatchScrapeResult:
        """Execute the 3-phase batch scrape asynchronously."""
        error_collector = BatchErrorCollector()
        with batch_error_context(error_collector):
            raw_urls, preclassified = self._filterer.filter_and_classify(source)
            self._reporter.on_phase1_complete(raw_urls, preclassified)

            if not preclassified:
                empty_result = BatchScrapeResult(
                    raw_urls=raw_urls,
                    classified_links=[],
                    results=[],
                    errors=error_collector.get_errors(),
                )
                self._reporter.on_batch_complete(empty_result)
                return empty_result

            ordered_links, results = await self._expand_and_scrape(
                preclassified, browser
            )

        batch_result = BatchScrapeResult(
            raw_urls=raw_urls,
            classified_links=ordered_links,
            results=results,
            errors=error_collector.get_errors(),
        )
        self._reporter.on_batch_complete(batch_result)
        return batch_result

    def run_sync(
        self,
        source: Path | str | Sequence[str],
        *,
        browser: AsyncBrowser | None = None,
    ) -> BatchScrapeResult:
        """Synchronous convenience entrypoint for running the batch scraper."""
        return asyncio.run(self.run(source, browser=browser))
