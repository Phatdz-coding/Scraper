"""Tests for two-stage headless/headed execution of Scribd links in BatchScraper."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

from scrape.classifiers import ClassifiedLink, DocumentType, WebsiteType
from scrape.main import (
    BatchConfig,
    BatchScraper,
    HeadedBrowserPolicy,
    NullBatchReporter,
    ScrapeItemResult,
)

SCRIBD_1 = "https://www.scribd.com/document/111111/Scribd-One"
SCRIBD_2 = "https://www.scribd.com/document/222222/Scribd-Two"
DRIVE_1 = "https://drive.google.com/file/d/DRIVE_1/view"
DRIVE_2 = "https://drive.google.com/file/d/DRIVE_2/view"
FOLDER_1 = "https://drive.google.com/drive/folders/FOLDER_1"


# ---------------------------------------------------------------------------
# Test Doubles
# ---------------------------------------------------------------------------
class FakeBrowser:
    def __init__(self, headless: bool) -> None:
        self.headless = headless
        self.closed = False


class BrowserLauncherSpy:
    """Stands in for scrape.open_shared_browser and records the launch lifecycle."""

    def __init__(self, *, fail_headed: bool = False) -> None:
        self.events: list[tuple[str, bool]] = []
        self.launch_kwargs: list[dict[str, object]] = []
        self.browsers: list[FakeBrowser] = []
        self._fail_headed = fail_headed

    @property
    def headless_calls(self) -> list[bool]:
        return [headless for event, headless in self.events if event == "open"]

    @asynccontextmanager
    async def __call__(
        self,
        headless=None,
        user_data_dir=None,
        *,
        browser_config=None,
        clean: bool = False,
    ):
        self.launch_kwargs.append(
            {
                "headless": headless,
                "browser_config": browser_config,
                "clean": clean,
            }
        )
        if self._fail_headed and headless is False:
            raise RuntimeError("Missing X server or $DISPLAY")
        browser = FakeBrowser(bool(headless))
        self.browsers.append(browser)
        self.events.append(("open", bool(headless)))
        try:
            yield browser
        finally:
            browser.closed = True
            self.events.append(("close", bool(headless)))


class RecordingScraper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, FakeBrowser]] = []

    async def scrape_one(
        self, link: ClassifiedLink, browser: FakeBrowser, cfg: BatchConfig
    ) -> ScrapeItemResult:
        assert not browser.closed, f"{link.url} scraped on a closed browser"
        self.calls.append((link.url, browser))
        return ScrapeItemResult(link=link, output_path=cfg.output_dir / "out.pdf")


class RecordingReporter(NullBatchReporter):
    def __init__(self) -> None:
        self.phase3_totals: list[int] = []
        self.progress: list[tuple[str, int, int]] = []

    def on_phase3_start(self, total: int) -> None:
        self.phase3_totals.append(total)

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None:
        self.progress.append((result.link.url, completed, total))


class FakeFolderExpander:
    """Expands every folder link into the given links (none by default)."""

    def __init__(self, discovered: list[ClassifiedLink] | None = None) -> None:
        self._discovered = discovered or []
        self.browsers: list[object] = []

    async def expand(self, links, browser=None) -> list[ClassifiedLink]:
        self.browsers.append(browser)
        kept = [l for l in links if l.document_type != DocumentType.FOLDER]
        return [*kept, *self._discovered]


def _make_scraper(
    headless: bool,
    *,
    folder_expander: FakeFolderExpander | None = None,
    browser_factory=None,
) -> tuple[BatchScraper, RecordingScraper, RecordingReporter]:
    scraper = RecordingScraper()
    reporter = RecordingReporter()
    batch_cfg = BatchConfig(
        output_dir=Path("/tmp/test_scribd_batch_out"),
        concurrency=2,
        scrape_type="d",
        headless=headless,
    )
    orchestrator = BatchScraper(
        folder_expander=folder_expander,
        scraper=scraper,
        config=batch_cfg,
        reporter=reporter,
        browser_factory=browser_factory,
    )
    return orchestrator, scraper, reporter


# ---------------------------------------------------------------------------
# HeadedBrowserPolicy
# ---------------------------------------------------------------------------
class TestHeadedBrowserPolicy(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = HeadedBrowserPolicy()
        self.links = [
            ClassifiedLink(SCRIBD_1, WebsiteType.SCRIBD),
            ClassifiedLink(DRIVE_1, WebsiteType.GOOGLE_DRIVE),
            ClassifiedLink(SCRIBD_2, WebsiteType.SCRIBD),
            ClassifiedLink(DRIVE_2, WebsiteType.GOOGLE_DOC),
        ]

    def test_order_is_stable_and_sinks_scribd_to_bottom(self) -> None:
        ordered = self.policy.order(self.links)
        self.assertEqual(
            [l.url for l in ordered], [DRIVE_1, DRIVE_2, SCRIBD_1, SCRIBD_2]
        )
        self.assertEqual([l.url for l in self.links][0], SCRIBD_1)  # input untouched

    def test_plan_splits_only_when_headless(self) -> None:
        ordered = self.policy.order(self.links)

        headless_plan = self.policy.plan(ordered, headless=True)
        self.assertEqual([p.headless for p in headless_plan], [True, False])
        self.assertEqual(
            [l.url for p in headless_plan for l in p.links],
            [l.url for l in ordered],
        )

        headed_plan = self.policy.plan(ordered, headless=False)
        self.assertEqual(len(headed_plan), 1)
        self.assertFalse(headed_plan[0].headless)
        self.assertEqual(self.policy.plan([], headless=True), [])


# ---------------------------------------------------------------------------
# BatchScraper two-stage browser lifecycle
# ---------------------------------------------------------------------------
class TestBatchScraperScribdHeadedExecution(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_batch_headless_runs_drive_headless_then_scribd_headed(self) -> None:
        launcher = BrowserLauncherSpy()
        orchestrator, scraper, reporter = _make_scraper(headless=True)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, DRIVE_1, SCRIBD_2, DRIVE_2])

        expected_order = [DRIVE_1, DRIVE_2, SCRIBD_1, SCRIBD_2]
        # Single sorted list shared by classified_links and results.
        self.assertEqual([l.url for l in result.classified_links], expected_order)
        self.assertEqual([r.link.url for r in result.results], expected_order)
        self.assertEqual(result.succeeded_count, 4)

        # Headless launch -> close (profile unlocked) -> headed relaunch.
        self.assertEqual(
            launcher.events,
            [("open", True), ("close", True), ("open", False), ("close", False)],
        )
        for kwargs in launcher.launch_kwargs:
            self.assertIs(kwargs["browser_config"], orchestrator.config.browser)
        self.assertEqual([k["clean"] for k in launcher.launch_kwargs], [False, True])

        headless_browser, headed_browser = launcher.browsers
        browser_by_url = {url: browser for url, browser in scraper.calls}
        self.assertIs(browser_by_url[DRIVE_1], headless_browser)
        self.assertIs(browser_by_url[DRIVE_2], headless_browser)
        self.assertIs(browser_by_url[SCRIBD_1], headed_browser)
        self.assertIs(browser_by_url[SCRIBD_2], headed_browser)

        # Continuous progress: one phase-3 start, counter runs 1..4 without reset.
        self.assertEqual(reporter.phase3_totals, [4])
        self.assertEqual([c for _, c, _ in reporter.progress], [1, 2, 3, 4])
        self.assertTrue(all(total == 4 for _, _, total in reporter.progress))
        self.assertEqual(
            {url for url, _, _ in reporter.progress[2:]}, {SCRIBD_1, SCRIBD_2}
        )

    async def test_scribd_only_headless_skips_headless_launch(self) -> None:
        launcher = BrowserLauncherSpy()
        orchestrator, scraper, _ = _make_scraper(headless=True)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, SCRIBD_2])

        self.assertEqual(launcher.headless_calls, [False])
        self.assertEqual([k["clean"] for k in launcher.launch_kwargs], [True])
        self.assertEqual(result.succeeded_count, 2)
        self.assertTrue(all(not b.headless for _, b in scraper.calls))

    async def test_mixed_batch_headed_uses_single_browser_without_relaunch(self) -> None:
        launcher = BrowserLauncherSpy()
        orchestrator, scraper, reporter = _make_scraper(headless=False)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, DRIVE_1, SCRIBD_2, DRIVE_2])

        self.assertEqual(launcher.headless_calls, [False])
        self.assertEqual(len({id(b) for _, b in scraper.calls}), 1)
        self.assertEqual(
            [l.url for l in result.classified_links],
            [DRIVE_1, DRIVE_2, SCRIBD_1, SCRIBD_2],
        )
        self.assertEqual(reporter.phase3_totals, [4])
        self.assertEqual([c for _, c, _ in reporter.progress], [1, 2, 3, 4])

    async def test_non_scribd_only_headless_uses_single_headless_browser(self) -> None:
        launcher = BrowserLauncherSpy()
        orchestrator, scraper, _ = _make_scraper(headless=True)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([DRIVE_1, DRIVE_2])

        self.assertEqual(launcher.headless_calls, [True])
        self.assertEqual(result.succeeded_count, 2)
        self.assertTrue(all(b.headless for _, b in scraper.calls))

    async def test_folder_expansion_headless_then_headed_for_scribd_only(self) -> None:
        launcher = BrowserLauncherSpy()
        expander = FakeFolderExpander(discovered=[])
        orchestrator, scraper, _ = _make_scraper(headless=True, folder_expander=expander)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([FOLDER_1, SCRIBD_1])

        # Phase 2 needs a headless browser for folders; it is closed before the headed launch.
        self.assertEqual(
            launcher.events,
            [("open", True), ("close", True), ("open", False), ("close", False)],
        )
        self.assertIs(expander.browsers[0], launcher.browsers[0])
        self.assertEqual([url for url, _ in scraper.calls], [SCRIBD_1])
        self.assertEqual(result.succeeded_count, 1)

    async def test_folder_expansion_browser_reused_for_headless_pass(self) -> None:
        launcher = BrowserLauncherSpy()
        discovered = [ClassifiedLink(DRIVE_2, WebsiteType.GOOGLE_DRIVE)]
        expander = FakeFolderExpander(discovered=discovered)
        orchestrator, scraper, _ = _make_scraper(headless=True, folder_expander=expander)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, FOLDER_1, DRIVE_1])

        self.assertEqual(launcher.headless_calls, [True, False])
        self.assertEqual(
            [l.url for l in result.classified_links], [DRIVE_1, DRIVE_2, SCRIBD_1]
        )
        browser_by_url = {url: browser for url, browser in scraper.calls}
        self.assertIs(browser_by_url[DRIVE_2], expander.browsers[0])

    async def test_no_folders_skips_browser_for_phase2(self) -> None:
        launcher = BrowserLauncherSpy()
        expander = FakeFolderExpander()
        orchestrator, _, _ = _make_scraper(headless=True, folder_expander=expander)

        with patch("scrape.main.open_shared_browser", launcher):
            await orchestrator.run([SCRIBD_1])

        self.assertEqual(expander.browsers, [None])
        self.assertEqual(launcher.headless_calls, [False])

    async def test_external_browser_factory_is_never_closed_or_relaunched(self) -> None:
        launcher = BrowserLauncherSpy()
        external_browser = FakeBrowser(headless=True)
        factory_opens = 0

        @asynccontextmanager
        async def fake_factory():
            nonlocal factory_opens
            factory_opens += 1
            yield external_browser

        orchestrator, scraper, reporter = _make_scraper(
            headless=True, browser_factory=fake_factory
        )
        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, DRIVE_1])

        self.assertEqual(launcher.events, [])
        self.assertEqual(factory_opens, 1)
        self.assertTrue(all(b is external_browser for _, b in scraper.calls))
        self.assertEqual([r.link.url for r in result.results], [DRIVE_1, SCRIBD_1])
        self.assertEqual(reporter.phase3_totals, [2])

    async def test_external_browser_argument_is_used_for_all_links(self) -> None:
        launcher = BrowserLauncherSpy()
        external_browser = FakeBrowser(headless=True)
        orchestrator, scraper, _ = _make_scraper(headless=True)

        with patch("scrape.main.open_shared_browser", launcher):
            await orchestrator.run([SCRIBD_1, DRIVE_1], browser=external_browser)

        self.assertEqual(launcher.events, [])
        self.assertFalse(external_browser.closed)
        self.assertTrue(all(b is external_browser for _, b in scraper.calls))

    async def test_headed_launch_failure_marks_scribd_failed_and_keeps_headless_results(self) -> None:
        launcher = BrowserLauncherSpy(fail_headed=True)
        orchestrator, _, reporter = _make_scraper(headless=True)

        with patch("scrape.main.open_shared_browser", launcher):
            result = await orchestrator.run([SCRIBD_1, DRIVE_1])

        self.assertEqual(result.succeeded_count, 1)
        self.assertTrue(result.results[0].succeeded)
        self.assertIn("Failed to launch headed browser", result.results[1].error or "")
        self.assertEqual([c for _, c, _ in reporter.progress], [1, 2])

    async def test_suppress_live_progress_wraps_both_passes_once(self) -> None:
        launcher = BrowserLauncherSpy()
        state = {"active": False, "entries": 0}

        @contextmanager
        def fake_suppress():
            state["entries"] += 1
            state["active"] = True
            try:
                yield
            finally:
                state["active"] = False

        orchestrator, scraper, _ = _make_scraper(headless=True)
        active_during_scrape: list[bool] = []
        original_scrape_one = scraper.scrape_one

        async def probing_scrape_one(link, browser, cfg):
            active_during_scrape.append(state["active"])
            return await original_scrape_one(link, browser, cfg)

        scraper.scrape_one = probing_scrape_one  # type: ignore[method-assign]
        with patch("scrape.main.open_shared_browser", launcher), patch("scrape.main.suppress_live_progress", fake_suppress
        ):
            await orchestrator.run([SCRIBD_1, DRIVE_1])

        self.assertEqual(state["entries"], 1)
        self.assertEqual(active_during_scrape, [True, True])
        self.assertEqual(launcher.headless_calls, [True, False])

    async def test_open_shared_browser_clean_mode_uses_single_shared_fresh_context(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from scrape import config
        from scrape.main import PlaywrightAsyncLinkScraper, open_shared_browser

        fake_context = MagicMock(spec=["new_page", "close"])
        created_pages: list[MagicMock] = []
        active_pages = 0
        peak_active_pages = 0

        async def make_page():
            nonlocal active_pages, peak_active_pages
            active_pages += 1
            peak_active_pages = max(peak_active_pages, active_pages)
            page = MagicMock()
            page.goto = AsyncMock()
            page.content = AsyncMock(return_value="<title>Doc | Scribd</title>")

            async def close_page():
                nonlocal active_pages
                active_pages -= 1

            page.close = AsyncMock(side_effect=close_page)
            created_pages.append(page)
            return page

        fake_context.new_page = AsyncMock(side_effect=make_page)
        fake_context.close = AsyncMock()

        fake_browser = MagicMock()
        fake_browser.new_context = AsyncMock(return_value=fake_context)
        fake_browser.close = AsyncMock()

        fake_playwright = MagicMock()
        fake_playwright.chromium.launch = AsyncMock(return_value=fake_browser)
        fake_playwright.chromium.launch_persistent_context = AsyncMock()

        @asynccontextmanager
        async def fake_async_playwright():
            yield fake_playwright

        async def fake_scribd_scraper(page, doc_name, output_dir=None):
            import asyncio

            await asyncio.sleep(0.01)
            return Path(output_dir or "/tmp") / "scribd.pdf"

        max_concurrency = 2
        links = [
            f"https://www.scribd.com/document/{100 + i}/Doc-{i}"
            for i in range(5)
        ]
        batch_cfg = BatchConfig(
            output_dir=Path("/tmp/test_scribd_single_ctx"),
            concurrency=max_concurrency,
            scrape_type="d",
            headless=True,
        )
        link_scraper = PlaywrightAsyncLinkScraper(scribd_scraper=fake_scribd_scraper)
        orchestrator = BatchScraper(scraper=link_scraper, config=batch_cfg)

        with patch("scrape.main.async_playwright", fake_async_playwright):
            result = await orchestrator.run(links)

        self.assertEqual(result.succeeded_count, 5)
        # Exactly 1 browser launch and 1 fresh BrowserContext for all 5 Scribd links.
        fake_playwright.chromium.launch.assert_awaited_once_with(headless=False)
        fake_browser.new_context.assert_awaited_once_with(
            accept_downloads=config.BROWSER_ACCEPT_DOWNLOADS
        )
        fake_playwright.chromium.launch_persistent_context.assert_not_awaited()
        # 5 pages opened across the single shared context, capped by concurrency.
        self.assertEqual(len(created_pages), 5)
        self.assertLessEqual(peak_active_pages, max_concurrency)
        self.assertEqual(active_pages, 0)
        fake_context.close.assert_awaited_once()
        fake_browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
