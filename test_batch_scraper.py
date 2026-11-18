from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from scrape import (
    BatchConfig,
    BatchScrapeResult,
    BatchScraper,
    ClassifierLinkFilterer,
    DriveFolderExpander,
    PlaywrightAsyncLinkScraper,
    RichBatchReporter,
    ScrapeItemResult,
    app,
)
from classifiers import (
    ClassifiedLink,
    DocumentType,
    DriveElement,
    WebsiteType,
)
import config
import folder_scraper
from folder_scraper import DriveFolderScraper
import ggdoc_scraper
import pdf_scraper
from process_images import convert_image_to_jpeg, fetch_image_batch
from simple_scrape import start_scraping
from template_info import Template
from tracker import OutputFilenameResolver, ScrapeProgress, suppress_live_progress


# ---------------------------------------------------------------------------
# Lightweight Async Playwright Test Doubles
# ---------------------------------------------------------------------------
class FakeAsyncPage:
    def __init__(self, url: str = "", html: str = "<title>Doc - Google Drive</title>") -> None:
        self.url = url
        self._html = html
        self.goto_calls: list[tuple[str, str | None]] = []
        self.closed = False
        self.fail_on_goto = False

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> MagicMock:
        if self.fail_on_goto:
            raise RuntimeError(f"Navigation failed for {url}")
        self.url = url
        self.goto_calls.append((url, wait_until))
        resp = MagicMock()
        resp.status = 200
        return resp

    async def content(self) -> str:
        return self._html

    async def title(self) -> str:
        return "Doc - Google Drive"

    async def evaluate(self, expression: str, arg: object = None) -> object:
        if "viewerData" in expression:
            return "Doc.pdf"
        return None

    async def close(self) -> None:
        self.closed = True


class FakeAsyncContext:
    def __init__(self, accept_downloads: bool = False) -> None:
        self.accept_downloads = accept_downloads
        self.pages: list[FakeAsyncPage] = []
        self.closed = False
        self.next_page_fail_goto = False

    async def new_page(self) -> FakeAsyncPage:
        page = FakeAsyncPage()
        page.fail_on_goto = self.next_page_fail_goto
        self.pages.append(page)
        return page

    async def route(self, pattern: str, handler: object) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class FakeAsyncBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeAsyncContext] = []
        self.closed = False

    async def new_context(
        self, accept_downloads: bool = False, **kwargs: object
    ) -> FakeAsyncContext:
        ctx = FakeAsyncContext(accept_downloads=accept_downloads)
        self.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Phase 1 Unit Tests: Filtering & Pre-Classification
# ---------------------------------------------------------------------------
class TestPhase1FilteringAndPreClassification(unittest.TestCase):
    def test_parses_txt_file_strips_comments_and_preclassifies_without_browser(self) -> None:
        content = """
        # Full-line hash comment
        // Full-line slash comment
        /* Multi-line block comment
           https://drive.google.com/file/d/IGNORED_1/view
        */
        \"\"\"
        Triple-quote block comment
        https://drive.google.com/file/d/IGNORED_2/view
        \"\"\"
        https://drive.google.com/file/d/FILE_VIEW_1/view # inline comment
        https://drive.google.com/drive/folders/FOLDER_ID_1 // inline slash comment
        https://docs.google.com/document/d/DOC_ID_1/edit
        https://docs.google.com/spreadsheets/d/SHEET_ID_1/edit
        https://www.scribd.com/document/123456789/Sample-Book
        https://example.com/unsupported-domain
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_path = Path(tmp_dir) / "links.txt"
            txt_path.write_text(content, encoding="utf-8")

            filterer = ClassifierLinkFilterer()
            raw_urls, classified = filterer.filter_and_classify(txt_path)

        self.assertEqual(len(raw_urls), 6)
        self.assertEqual(len(classified), 5)

        # 1. Google Drive /file/d/.../view must keep document_type=None in Phase 1
        self.assertEqual(
            classified[0],
            ClassifiedLink(
                url="https://drive.google.com/file/d/FILE_VIEW_1/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=None,
            ),
        )
        # 2. Google Drive folder is unambiguously DocumentType.FOLDER
        self.assertEqual(
            classified[1],
            ClassifiedLink(
                url="https://drive.google.com/drive/folders/FOLDER_ID_1",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.FOLDER,
            ),
        )
        # 3. Google Docs /document/d/... remains document_type=None until DOM check
        self.assertEqual(
            classified[2],
            ClassifiedLink(
                url="https://docs.google.com/document/d/DOC_ID_1/edit",
                website_type=WebsiteType.GOOGLE_DOC,
                document_type=None,
            ),
        )
        # 4. Google Sheets is unambiguously DocumentType.XLSX
        self.assertEqual(
            classified[3],
            ClassifiedLink(
                url="https://docs.google.com/spreadsheets/d/SHEET_ID_1/edit",
                website_type=WebsiteType.GOOGLE_DOC,
                document_type=DocumentType.XLSX,
            ),
        )
        # 5. Scribd is unambiguously DocumentType.PDF
        self.assertEqual(
            classified[4],
            ClassifiedLink(
                url="https://www.scribd.com/document/123456789/Sample-Book",
                website_type=WebsiteType.SCRIBD,
                document_type=DocumentType.PDF,
            ),
        )

    def test_parses_list_of_strings(self) -> None:
        filterer = ClassifierLinkFilterer()
        raw_urls, classified = filterer.filter_and_classify(
            [
                "https://drive.google.com/file/d/ABC123/view",
                "# comment line",
                "https://www.scribd.com/doc/987654/Title",
            ]
        )
        self.assertEqual(len(raw_urls), 2)
        self.assertEqual(len(classified), 2)
        self.assertIsNone(classified[0].document_type)
        self.assertEqual(classified[1].document_type, DocumentType.PDF)


# ---------------------------------------------------------------------------
# Phase 2 Unit Tests: Folder Scraping & Queue Seeding
# ---------------------------------------------------------------------------
class TestPhase2FolderExpansion(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        DriveFolderScraper._reset_instance()

    def tearDown(self) -> None:
        DriveFolderScraper._reset_instance()

    async def test_all_folder_links_seeded_into_drive_folder_scraper_queue(self) -> None:
        scraper = DriveFolderScraper.get_instance()
        seeded_snapshots: list[list[tuple[str, int]]] = []

        async def fake_drain(reader: object) -> None:
            seeded_snapshots.append(list(scraper.queue))
            while scraper.queue:
                folder_url, _depth = scraper.queue.popleft()
                fid = folder_scraper.extract_folder_id(folder_url)
                scraper._record_file(DocumentType.PDF, f"file_from_{fid}")

        fake_browser = FakeAsyncBrowser()
        folder_1 = "https://drive.google.com/drive/folders/FOLDER_A"
        folder_2 = "https://drive.google.com/drive/folders/FOLDER_B"

        with (
            patch.object(folder_scraper, "should_use_drive_api", return_value=False),
            patch.object(scraper, "_drain_queue_concurrently", side_effect=fake_drain),
        ):
            expander = DriveFolderExpander()
            input_links = [
                ClassifiedLink(
                    url="https://drive.google.com/file/d/DIRECT_1/view",
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=None,
                ),
                ClassifiedLink(
                    url=folder_1,
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.FOLDER,
                ),
                ClassifiedLink(
                    url=folder_2,
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.FOLDER,
                ),
            ]
            expanded = await expander.expand(input_links, browser=fake_browser)  # type: ignore[arg-type]

        # Both folders were seeded into DriveFolderScraper.queue at depth 0 before draining
        self.assertEqual(
            seeded_snapshots,
            [[(folder_1, 0), (folder_2, 0)]],
        )
        self.assertIn("FOLDER_A", scraper.visited_folder_ids)
        self.assertIn("FOLDER_B", scraper.visited_folder_ids)
        self.assertFalse(fake_browser.closed)

        self.assertEqual(
            [link.url for link in expanded],
            [
                "https://drive.google.com/file/d/DIRECT_1/view",
                "https://drive.google.com/file/d/file_from_FOLDER_A/view",
                "https://drive.google.com/file/d/file_from_FOLDER_B/view",
            ],
        )

    async def test_failing_folder_does_not_abort_remaining_folders_and_deduplicates_in_order(self) -> None:
        scraper = DriveFolderScraper.get_instance()
        folder_ok = "https://drive.google.com/drive/folders/FOLDER_OK"
        folder_bad = "https://drive.google.com/drive/folders/FOLDER_BAD"

        class FakeReader:
            async def read(self, folder_url: str) -> list[DriveElement] | None:
                if "FOLDER_BAD" in folder_url:
                    raise RuntimeError("Simulated folder read error")
                return [
                    DriveElement(
                        data_id="SHARED_FILE",
                        paths=[config.pdf_icon_vector],
                    ),
                    DriveElement(
                        data_id="NEW_FOLDER_FILE",
                        paths=[config.pdf_icon_vector],
                    ),
                ]

        with (
            patch.object(folder_scraper, "should_use_drive_api", return_value=False),
            patch.object(
                folder_scraper,
                "PlaywrightFolderElementReader",
                return_value=FakeReader(),
            ),
        ):
            expander = DriveFolderExpander()
            input_links = [
                ClassifiedLink(
                    url="https://drive.google.com/file/d/SHARED_FILE/view",
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=None,
                ),
                ClassifiedLink(
                    url=folder_bad,
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.FOLDER,
                ),
                ClassifiedLink(
                    url=folder_ok,
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.FOLDER,
                ),
                ClassifiedLink(
                    url="https://www.scribd.com/document/111/Doc",
                    website_type=WebsiteType.SCRIBD,
                    document_type=DocumentType.PDF,
                ),
            ]
            expanded = await expander.expand(
                input_links,
                browser=FakeAsyncBrowser(),  # type: ignore[arg-type]
            )

        self.assertEqual(len( scraper.queue), 0)
        self.assertEqual(
            [link.url for link in expanded],
            [
                "https://drive.google.com/file/d/SHARED_FILE/view",
                "https://www.scribd.com/document/111/Doc",
                "https://drive.google.com/file/d/NEW_FOLDER_FILE/view",
            ],
        )
        # First-seen SHARED_FILE gets its document_type upgraded from None to PDF
        self.assertEqual(expanded[0].document_type, DocumentType.PDF)


# ---------------------------------------------------------------------------
# Phase 3 Unit Tests: Concurrency, Shared Browser, Cleanup & Filename Dedup
# ---------------------------------------------------------------------------
class TestPhase3ConcurrentScraping(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_caps_concurrent_workers_and_reuses_single_shared_browser(self) -> None:
        max_concurrency = 3
        total_links = 9
        active_workers = 0
        peak_workers = 0
        seen_browsers: list[object] = []
        shared_browser = FakeAsyncBrowser()
        browser_open_count = 0

        @asynccontextmanager
        async def fake_browser_factory():
            nonlocal browser_open_count
            browser_open_count += 1
            yield shared_browser

        class ConcurrencyProbeScraper:
            async def scrape_one(
                self,
                link: ClassifiedLink,
                browser: object,
                batch_cfg: BatchConfig,
            ) -> ScrapeItemResult:
                nonlocal active_workers, peak_workers
                seen_browsers.append(browser)
                active_workers += 1
                peak_workers = max(peak_workers, active_workers)
                await asyncio.sleep(0.02)
                active_workers -= 1
                return ScrapeItemResult(
                    link=link,
                    output_path=batch_cfg.output_dir / "out.pdf",
                )

        urls = [
            f"https://drive.google.com/file/d/ID_{i}/view"
            for i in range(total_links)
        ]
        batch_cfg = BatchConfig(
            output_dir=Path("/tmp/test_batch_out"),
            concurrency=max_concurrency,
            scrape_type="d",
        )
        orchestrator = BatchScraper(
            scraper=ConcurrencyProbeScraper(),
            config=batch_cfg,
            browser_factory=fake_browser_factory,  # type: ignore[arg-type]
        )

        result = await orchestrator.run(urls)

        self.assertEqual(browser_open_count, 1)
        self.assertEqual(len(seen_browsers), total_links)
        self.assertTrue(all(b is shared_browser for b in seen_browsers))
        self.assertLessEqual(peak_workers, max_concurrency)
        self.assertEqual(peak_workers, max_concurrency)
        self.assertEqual(result.succeeded_count, total_links)
        self.assertEqual(result.failed_count, 0)

    async def test_context_and_page_cleanup_runs_on_success_and_error(self) -> None:
        fake_browser = FakeAsyncBrowser()

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.PDF
            tmpl.fileID = "ID_1"
            return tmpl

        async def failing_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            if "FAIL" in page.url:
                raise ValueError("Viewer parameters missing")
            return Path("doc.pdf")

        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_cfg = BatchConfig(
                output_dir=Path(tmp_dir),
                concurrency=2,
                scrape_type="d",
            )
            link_scraper = PlaywrightAsyncLinkScraper(
                template_factory=fake_template_create,
                scrape_fn=failing_scrape_fn,
            )

            ok_link = ClassifiedLink(
                url="https://drive.google.com/file/d/OK_1/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
            )
            fail_link = ClassifiedLink(
                url="https://drive.google.com/file/d/FAIL_1/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
            )

            res_ok = await link_scraper.scrape_one(
                ok_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )
            res_fail = await link_scraper.scrape_one(
                fail_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )

        self.assertTrue(res_ok.succeeded)
        self.assertEqual(ok_link.document_type, DocumentType.PDF)
        self.assertFalse(res_fail.succeeded)
        self.assertIn("Viewer parameters missing", res_fail.error or "")

        # Every opened BrowserContext and Page must be closed
        self.assertEqual(len(fake_browser.contexts), 2)
        for ctx in fake_browser.contexts:
            self.assertTrue(ctx.closed)
            for page in ctx.pages:
                self.assertTrue(page.closed)

    async def test_google_doc_url_is_converted_to_all_tabs_view_before_scraping(self) -> None:
        fake_browser = FakeAsyncBrowser()
        visited_urls: list[str] = []

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            visited_urls.append(url)
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.GOOGLE_DOC
            tmpl.fileID = "DOC_99"
            return tmpl

        async def fake_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            return Path("notes.pdf")

        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_cfg = BatchConfig(output_dir=Path(tmp_dir), concurrency=1)
            scraper = PlaywrightAsyncLinkScraper(
                template_factory=fake_template_create,
                scrape_fn=fake_scrape_fn,
            )
            doc_link = ClassifiedLink(
                url="https://docs.google.com/document/d/DOC_99/edit?usp=sharing",
                website_type=WebsiteType.GOOGLE_DOC,
            )
            res = await scraper.scrape_one(
                doc_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )

        self.assertTrue(res.succeeded)
        self.assertEqual(
            visited_urls,
            ["https://drive.google.com/file/d/DOC_99/view"],
        )

    async def test_colliding_filenames_are_safely_deduplicated_across_concurrent_tasks(self) -> None:
        fake_browser = FakeAsyncBrowser()

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.PDF
            tmpl.fileID = url.split("/")[-2]
            return tmpl

        async def fake_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            await asyncio.sleep(0.01)
            return Path("DuplicateName.pdf")

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            # Pre-create one file on disk to also verify disk collision handling
            (out_dir / "DuplicateName.pdf").write_bytes(b"%PDF-existing")

            resolver = OutputFilenameResolver()
            link_scraper = PlaywrightAsyncLinkScraper(
                template_factory=fake_template_create,
                scrape_fn=fake_scrape_fn,
                filename_resolver=resolver,
            )
            batch_cfg = BatchConfig(output_dir=out_dir, concurrency=4)

            links = [
                ClassifiedLink(
                    url=f"https://drive.google.com/file/d/FILE_{i}/view",
                    website_type=WebsiteType.GOOGLE_DRIVE,
                )
                for i in range(3)
            ]

            results = await asyncio.gather(
                *(
                    link_scraper.scrape_one(
                        link, fake_browser, batch_cfg  # type: ignore[arg-type]
                    )
                    for link in links
                )
            )

        output_names = [r.output_path.name for r in results if r.output_path]
        self.assertEqual(len(output_names), 3)
        self.assertEqual(len(set(output_names)), 3)
        self.assertEqual(
            sorted(output_names),
            ["DuplicateName_1.pdf", "DuplicateName_2.pdf", "DuplicateName_3.pdf"],
        )


# ---------------------------------------------------------------------------
# Async Pipeline & Module Migration Unit Tests
# ---------------------------------------------------------------------------
class TestAsyncScraperModules(unittest.IsolatedAsyncioTestCase):
    async def test_template_create_and_methods_are_async(self) -> None:
        page = FakeAsyncPage(
            url="https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view",
            html="<html><head><title>Lecture_01.pdf - Google Drive</title></head></html>",
        )
        template = await Template.create(page, page.url)  # type: ignore[arg-type]
        self.assertEqual(template.website_type, WebsiteType.GOOGLE_DRIVE)
        self.assertEqual(template.document_type, DocumentType.PDF)
        self.assertEqual(template.fileID, "10pYur59yGoU42E1D13YU_rbYModCl9xl")
        self.assertEqual(
            await template.get_file_name(await page.content()),
            "Lecture_01.pdf",
        )

    async def test_process_images_async_helpers(self) -> None:
        page = MagicMock()
        page.evaluate = AsyncMock(return_value="ffd8ffe0")
        with patch("process_images.base64.b64decode", return_value=b"\xff\xd8\xff\xe0"):
            jpeg = await convert_image_to_jpeg(page, b"raw-webp", "image/webp")
        self.assertEqual(jpeg, b"\xff\xd8\xff\xe0")

        expected_batch = [{"status": 200, "contentType": "image/jpeg", "body": "AA=="}]
        page.evaluate = AsyncMock(return_value=expected_batch)
        batch = await fetch_image_batch(page, ["https://drive.google.com/viewer/img?page=0"])
        self.assertEqual(batch, expected_batch)

    async def test_start_scraping_respects_scrape_type_s_and_d(self) -> None:
        page = FakeAsyncPage(
            url="https://drive.google.com/file/d/FILE_123/view",
            html="<title>MyBook.pdf - Google Drive</title>",
        )
        template = await Template.create(page, page.url)  # type: ignore[arg-type]

        with (
            patch.object(
                pdf_scraper,
                "direct_download",
                new=AsyncMock(return_value=(Path("/tmp/MyBook.pdf"), 200)),
            ) as mock_direct,
            patch.object(
                pdf_scraper,
                "scrape_inpage_by_api",
                new=AsyncMock(return_value=Path("/tmp/MyBook_api.pdf")),
            ) as mock_inpage,
        ):
            # Mode 'd' uses direct_download first
            path_d = await start_scraping(
                page, template, scrape_type="d"  # type: ignore[arg-type]
            )
            self.assertEqual(path_d, Path("/tmp/MyBook.pdf"))
            mock_direct.assert_awaited_once()
            mock_inpage.assert_not_awaited()

            mock_direct.reset_mock()
            # Mode 's' skips direct_download and calls scrape_inpage_by_api
            path_s = await start_scraping(
                page, template, scrape_type="s"  # type: ignore[arg-type]
            )
            self.assertEqual(path_s, Path("/tmp/MyBook_api.pdf"))
            mock_direct.assert_not_awaited()
            mock_inpage.assert_awaited_once()

    async def test_tracker_suppress_live_progress_prevents_live_collision(self) -> None:
        progress = ScrapeProgress(enable_live=True)
        with suppress_live_progress():
            progress.increment(5)
            progress.increment(5)
            progress.finish()
        self.assertEqual(progress.get_count(), 2)
        self.assertIsNone(progress._live)

    async def test_direct_download_non_blocking_and_releases_reservation_on_html_fallback(self) -> None:
        class FakeHTTPResponse:
            def __init__(self, status: int, content_type: str, body: bytes) -> None:
                self.status = status
                self.headers = {"Content-Type": content_type}
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> FakeHTTPResponse:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            # 1. text/html response (view-only) returns (None, 404) and releases path reservation
            with patch(
                "pdf_scraper.urlopen",
                return_value=FakeHTTPResponse(200, "text/html; charset=utf-8", b"<html></html>"),
            ):
                path_html, status_html = await pdf_scraper.direct_download(
                    "FILE_HTML", "ViewOnly.pdf", output_dir=out_dir
                )
            self.assertIsNone(path_html)
            self.assertEqual(status_html, 404)

            # 2. Subsequent download of the same name gets the clean 'ViewOnly.pdf' name
            with patch(
                "pdf_scraper.urlopen",
                return_value=FakeHTTPResponse(200, "application/pdf", b"%PDF-1.4"),
            ):
                path_pdf, status_pdf = await pdf_scraper.direct_download(
                    "FILE_PDF", "ViewOnly.pdf", output_dir=out_dir
                )
            self.assertEqual(status_pdf, 200)
            self.assertEqual(path_pdf, out_dir / "ViewOnly.pdf")
            self.assertEqual((out_dir / "ViewOnly.pdf").read_bytes(), b"%PDF-1.4")

            # 3. ggdoc_scraper.direct_download writes docx via asyncio.to_thread
            with patch(
                "ggdoc_scraper.urlopen",
                return_value=FakeHTTPResponse(
                    200,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    b"PK-docx",
                ),
            ):
                doc_path, doc_status = await ggdoc_scraper.direct_download(
                    "DOC_1", "Essay.docx", output_dir=out_dir
                )
            self.assertEqual(doc_status, 200)
            self.assertEqual(doc_path, out_dir / "Essay.docx")

    async def test_end_to_end_three_phase_batch_run(self) -> None:
        DriveFolderScraper._reset_instance()
        shared_browser = FakeAsyncBrowser()

        @asynccontextmanager
        async def fake_browser_factory():
            yield shared_browser

        async def fake_folder_scrape(
            folder_urls: list[str], browser: object = None
        ) -> list[ClassifiedLink]:
            self.assertIs(browser, shared_browser)
            return [
                ClassifiedLink(
                    url="https://drive.google.com/file/d/FROM_FOLDER_1/view",
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.PDF,
                )
            ]

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.PDF
            tmpl.fileID = url.split("/")[-2]
            return tmpl

        async def fake_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            return Path(f"{tmpl.fileID}.pdf")

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            batch_cfg = BatchConfig(output_dir=out_dir, concurrency=2, scrape_type="d")
            batch_scraper = BatchScraper(
                filterer=ClassifierLinkFilterer(),
                folder_expander=DriveFolderExpander(folder_scraper_fn=fake_folder_scrape),
                scraper=PlaywrightAsyncLinkScraper(
                    template_factory=fake_template_create,
                    scrape_fn=fake_scrape_fn,
                ),
                config=batch_cfg,
                reporter=RichBatchReporter(enable_live_bar=False),
                browser_factory=fake_browser_factory,  # type: ignore[arg-type]
            )

            result = await batch_scraper.run(
                [
                    "# Batch input",
                    "https://drive.google.com/file/d/DIRECT_FILE_1/view",
                    "https://drive.google.com/drive/folders/FOLDER_1",
                ]
            )

        self.assertEqual(len(result.raw_urls), 2)
        self.assertEqual(len(result.classified_links), 2)
        self.assertEqual(result.succeeded_count, 2)
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(
            [r.output_path.name for r in result.results if r.output_path],
            ["DIRECT_FILE_1.pdf", "FROM_FOLDER_1.pdf"],
        )

    def test_batch_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            BatchConfig(concurrency=0)
        with self.assertRaises(ValueError):
            BatchConfig(scrape_type="invalid")


# ---------------------------------------------------------------------------
# CLI Integration Unit Tests (typer.testing.CliRunner)
# ---------------------------------------------------------------------------
class TestBatchScraperCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_cli_flags_passed_to_batch_config_and_exits_zero_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            out_dir = Path(tmp_dir) / "custom_out"
            url = "https://drive.google.com/file/d/123/view"
            txt_file.write_text(url + "\n", encoding="utf-8")

            link = ClassifiedLink(
                url=url,
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.PDF,
            )
            fake_result = BatchScrapeResult(
                raw_urls=[url],
                classified_links=[link],
                results=[
                    ScrapeItemResult(
                        link=link,
                        output_path=out_dir / "doc.pdf",
                        error=None,
                    )
                ],
            )

            captured_configs: list[BatchConfig] = []

            async def fake_run(
                scraper_self: BatchScraper, source: Path, **kwargs: object
            ) -> BatchScrapeResult:
                captured_configs.append(scraper_self.config)
                return fake_result

            with patch.object(BatchScraper, "run", new=fake_run):
                res = self.runner.invoke(
                    app,
                    [
                        str(txt_file),
                        "--concurrency",
                        "8",
                        "--output",
                        str(out_dir),
                        "--scrape-type",
                        "s",
                    ],
                )

        self.assertEqual(res.exit_code, 0, msg=res.output)
        self.assertEqual(len(captured_configs), 1)
        self.assertEqual(captured_configs[0].concurrency, 8)
        self.assertEqual(captured_configs[0].output_dir, out_dir)
        self.assertEqual(captured_configs[0].scrape_type, "s")
        self.assertIn("OK", res.output)

    def test_cli_exits_nonzero_when_any_item_fails_while_printing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            url_ok = "https://drive.google.com/file/d/OK/view"
            url_fail = "https://drive.google.com/file/d/FAIL/view"
            txt_file.write_text(f"{url_ok}\n{url_fail}\n", encoding="utf-8")

            link_ok = ClassifiedLink(
                url=url_ok,
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.PDF,
            )
            link_fail = ClassifiedLink(
                url=url_fail,
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.PDF,
            )
            fake_result = BatchScrapeResult(
                raw_urls=[url_ok, url_fail],
                classified_links=[link_ok, link_fail],
                results=[
                    ScrapeItemResult(
                        link=link_ok,
                        output_path=Path(tmp_dir) / "ok.pdf",
                    ),
                    ScrapeItemResult(
                        link=link_fail,
                        output_path=None,
                        error="Download failed",
                    ),
                ],
            )

            async def fake_run(
                scraper_self: BatchScraper, source: Path, **kwargs: object
            ) -> BatchScrapeResult:
                return fake_result

            with patch.object(BatchScraper, "run", new=fake_run):
                res = self.runner.invoke(app, [str(txt_file)])

        self.assertEqual(res.exit_code, 1)
        self.assertIn("OK", res.output)
        self.assertIn("FAIL", res.output)
        self.assertIn("Download failed", res.output)

    def test_cli_exits_nonzero_on_invalid_scrape_type_or_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            txt_file.write_text("https://drive.google.com/file/d/1/view\n", encoding="utf-8")

            bad_type_res = self.runner.invoke(
                app, [str(txt_file), "--scrape-type", "invalid"]
            )
            self.assertEqual(bad_type_res.exit_code, 1)

            missing_file_res = self.runner.invoke(
                app, [str(Path(tmp_dir) / "nonexistent.txt")]
            )
            self.assertEqual(missing_file_res.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
