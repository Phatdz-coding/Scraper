from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console
from typer.testing import CliRunner

from scrape.scrape import (
    BatchConfig,
    BatchScrapeResult,
    BatchScraper,
    BrowserConfig,
    ClassifierLinkFilterer,
    DriveFolderExpander,
    PlaywrightAsyncLinkScraper,
    RichBatchReporter,
    ScrapeItemResult,
    app,
    open_shared_browser,
    resolve_cli_source,
)
from scrape.classifiers import (
    ClassifiedLink,
    DocumentType,
    DriveElement,
    WebsiteType,
)
from scrape import config
from scrape import folder_scraper
from scrape.folder_scraper import DriveFolderScraper
from scrape import ggdoc_scraper
from scrape import pdf_scraper
from scrape.process_images import convert_image_to_jpeg, fetch_image_batch
from scrape.router import start_scraping
from scrape.template_info import Template
from scrape.tracker import OutputFilenameResolver, ScrapeProgress, suppress_live_progress


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

    async def test_skips_document_type_check_when_already_known_and_checks_for_none_shortcut_unknown(self) -> None:
        fake_browser = FakeAsyncBrowser()
        template_factory_calls: list[str] = []

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            template_factory_calls.append(url)
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.PDF
            tmpl.fileID = url.split("/")[-2]
            return tmpl

        seen_templates: list[Template] = []

        async def fake_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            seen_templates.append(tmpl)
            return Path(f"{tmpl.fileID}.pdf")

        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_cfg = BatchConfig(output_dir=Path(tmp_dir), concurrency=1)
            link_scraper = PlaywrightAsyncLinkScraper(
                template_factory=fake_template_create,
                scrape_fn=fake_scrape_fn,
            )

            # 1. Pre-classified PDF link -> must skip template_factory
            known_pdf_link = ClassifiedLink(
                url="https://drive.google.com/file/d/KNOWN_PDF/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.PDF,
            )
            res_known = await link_scraper.scrape_one(
                known_pdf_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )
            self.assertTrue(res_known.succeeded)
            self.assertEqual(template_factory_calls, [])
            self.assertEqual(seen_templates[-1].document_type, DocumentType.PDF)
            self.assertEqual(seen_templates[-1].fileID, "KNOWN_PDF")

            # 2. document_type is None -> must call template_factory
            none_link = ClassifiedLink(
                url="https://drive.google.com/file/d/NONE_DOC/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=None,
            )
            res_none = await link_scraper.scrape_one(
                none_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )
            self.assertTrue(res_none.succeeded)
            self.assertEqual(len(template_factory_calls), 1)
            self.assertEqual(none_link.document_type, DocumentType.PDF)

            # 3. document_type is SHORTCUT -> must call template_factory
            shortcut_link = ClassifiedLink(
                url="https://drive.google.com/file/d/SHORTCUT_DOC/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.SHORTCUT,
            )
            res_shortcut = await link_scraper.scrape_one(
                shortcut_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )
            self.assertTrue(res_shortcut.succeeded)
            self.assertEqual(len(template_factory_calls), 2)
            self.assertEqual(shortcut_link.document_type, DocumentType.PDF)

            # 4. document_type is UNKNOWN -> must call template_factory
            unknown_link = ClassifiedLink(
                url="https://drive.google.com/file/d/UNKNOWN_DOC/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.UNKNOWN,
            )
            res_unknown = await link_scraper.scrape_one(
                unknown_link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )
            self.assertTrue(res_unknown.succeeded)
            self.assertEqual(len(template_factory_calls), 3)
            self.assertEqual(unknown_link.document_type, DocumentType.PDF)


# ---------------------------------------------------------------------------
# Lazy Browser Page / -t d vs -t s Routing Tests
# ---------------------------------------------------------------------------
class _FakeHTTPResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class TestLazyPageRouting(unittest.IsolatedAsyncioTestCase):
    URL = "https://drive.google.com/file/d/LAZY_1/view"

    @staticmethod
    def _opened_pages(browser: FakeAsyncBrowser) -> list[FakeAsyncPage]:
        return [p for ctx in browser.contexts for p in ctx.pages]

    def _factory(self, calls: list[str], doc_type: DocumentType = DocumentType.PDF):
        async def factory(page: object, url: str) -> MagicMock:
            calls.append(url)
            tmpl = MagicMock(spec=Template)
            tmpl.document_type = doc_type
            tmpl.fileID = "LAZY_1"
            return tmpl

        return factory

    async def test_t_d_direct_download_success_opens_no_browser_page(self) -> None:
        browser = FakeAsyncBrowser()
        factory_calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = BatchConfig(output_dir=Path(tmp_dir), concurrency=1, scrape_type="d")
            scraper = PlaywrightAsyncLinkScraper(
                template_factory=self._factory(factory_calls)
            )
            link = ClassifiedLink(url=self.URL, website_type=WebsiteType.GOOGLE_DRIVE)
            response = _FakeHTTPResponse(
                200,
                {
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="Lecture 1.pdf"',
                },
                b"%PDF-1.4",
            )
            with patch("scrape.pdf_scraper.urlopen", return_value=response):
                res = await scraper.scrape_one(link, browser, cfg)  # type: ignore[arg-type]

            self.assertTrue(res.succeeded, res.error)
            self.assertEqual(res.output_path, Path(tmp_dir) / "Lecture 1.pdf")
            self.assertEqual(res.output_path.read_bytes(), b"%PDF-1.4")
        self.assertEqual(browser.contexts, [])
        self.assertEqual(factory_calls, [])

    async def test_t_d_direct_download_infers_extension_from_content_type(self) -> None:
        browser = FakeAsyncBrowser()
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = BatchConfig(output_dir=Path(tmp_dir), concurrency=1, scrape_type="d")
            scraper = PlaywrightAsyncLinkScraper()
            link = ClassifiedLink(url=self.URL, website_type=WebsiteType.GOOGLE_DRIVE)
            response = _FakeHTTPResponse(200, {"Content-Type": "application/pdf"}, b"x")
            with patch("scrape.pdf_scraper.urlopen", return_value=response):
                res = await scraper.scrape_one(link, browser, cfg)  # type: ignore[arg-type]
            self.assertTrue(res.succeeded, res.error)
            self.assertEqual(res.output_path.name, "LAZY_1.pdf")
        self.assertEqual(browser.contexts, [])

    async def test_t_d_fallback_opens_page_once_and_skips_type_detection_when_resolved(
        self,
    ) -> None:
        cases = {
            DocumentType.PDF: "pdf_scraper.scrape_inpage_by_api",
            DocumentType.IMAGE: "image_scraper.scrape_inpage_by_api",
            DocumentType.GOOGLE_DOC: "ggdoc_scraper.scrape_inpage_by_api",
        }
        from scrape import image_scraper
        for doc_type, target in cases.items():
            with self.subTest(document_type=doc_type):
                browser = FakeAsyncBrowser()
                factory_calls: list[str] = []
                module_name, fn_name = target.split(".")
                module = {
                    "pdf_scraper": pdf_scraper,
                    "image_scraper": image_scraper,
                    "ggdoc_scraper": ggdoc_scraper,
                }[module_name]
                with tempfile.TemporaryDirectory() as tmp_dir:
                    cfg = BatchConfig(
                        output_dir=Path(tmp_dir), concurrency=1, scrape_type="d"
                    )
                    scraper = PlaywrightAsyncLinkScraper(
                        template_factory=self._factory(factory_calls)
                    )
                    link = ClassifiedLink(
                        url=self.URL,
                        website_type=WebsiteType.GOOGLE_DRIVE,
                        document_type=doc_type,
                    )
                    html_resp = _FakeHTTPResponse(200, {"Content-Type": "text/html"}, b"<html>")
                    inpage = AsyncMock(return_value=Path(tmp_dir) / "out.bin")
                    with (
                        patch("scrape.pdf_scraper.urlopen", return_value=html_resp),
                        patch("scrape.ggdoc_scraper.urlopen",
                            side_effect=__import__("urllib.error").error.HTTPError(
                                self.URL, 404, "nf", {}, None  # type: ignore[arg-type]
                            ),
                        ),
                        patch.object(module, fn_name, new=inpage),
                    ):
                        res = await scraper.scrape_one(link, browser, cfg)  # type: ignore[arg-type]
                self.assertTrue(res.succeeded, res.error)
                inpage.assert_awaited_once()
                self.assertEqual(factory_calls, [])
                pages = self._opened_pages(browser)
                self.assertEqual(len(browser.contexts), 1)
                self.assertEqual(len(pages), 1)
                self.assertEqual(len(pages[0].goto_calls), 1)
                self.assertTrue(pages[0].closed)

    async def test_t_d_fallback_runs_type_detection_when_missing_or_unknown(self) -> None:
        for doc_type in (None, DocumentType.UNKNOWN):
            with self.subTest(document_type=doc_type):
                browser = FakeAsyncBrowser()
                factory_calls: list[str] = []
                with tempfile.TemporaryDirectory() as tmp_dir:
                    cfg = BatchConfig(
                        output_dir=Path(tmp_dir), concurrency=1, scrape_type="d"
                    )
                    scraper = PlaywrightAsyncLinkScraper(
                        template_factory=self._factory(factory_calls)
                    )
                    link = ClassifiedLink(
                        url=self.URL,
                        website_type=WebsiteType.GOOGLE_DRIVE,
                        document_type=doc_type,
                    )
                    html_resp = _FakeHTTPResponse(200, {"Content-Type": "text/html"}, b"<html>")
                    inpage = AsyncMock(return_value=Path(tmp_dir) / "out.pdf")
                    with (
                        patch("scrape.pdf_scraper.urlopen", return_value=html_resp),
                        patch.object(pdf_scraper, "scrape_inpage_by_api", new=inpage),
                    ):
                        res = await scraper.scrape_one(link, browser, cfg)  # type: ignore[arg-type]
                self.assertTrue(res.succeeded, res.error)
                self.assertEqual(factory_calls, [self.URL])
                inpage.assert_awaited_once()
                self.assertEqual(link.document_type, DocumentType.PDF)
                pages = self._opened_pages(browser)
                self.assertEqual(len(pages), 1)
                self.assertEqual(len(pages[0].goto_calls), 1)

    async def test_t_s_never_direct_downloads_and_opens_page_once(self) -> None:
        for doc_type, expect_factory in ((DocumentType.PDF, False), (None, True)):
            with self.subTest(document_type=doc_type):
                browser = FakeAsyncBrowser()
                factory_calls: list[str] = []
                with tempfile.TemporaryDirectory() as tmp_dir:
                    cfg = BatchConfig(
                        output_dir=Path(tmp_dir), concurrency=1, scrape_type="s"
                    )
                    scraper = PlaywrightAsyncLinkScraper(
                        template_factory=self._factory(factory_calls)
                    )
                    link = ClassifiedLink(
                        url=self.URL,
                        website_type=WebsiteType.GOOGLE_DRIVE,
                        document_type=doc_type,
                    )
                    direct = AsyncMock(return_value=(Path(tmp_dir) / "d.pdf", 200))
                    inpage = AsyncMock(return_value=Path(tmp_dir) / "s.pdf")
                    with (
                        patch.object(pdf_scraper, "direct_download", new=direct),
                        patch.object(pdf_scraper, "scrape_inpage_by_api", new=inpage),
                    ):
                        res = await scraper.scrape_one(link, browser, cfg)  # type: ignore[arg-type]
                self.assertTrue(res.succeeded, res.error)
                direct.assert_not_awaited()
                inpage.assert_awaited_once()
                self.assertEqual(bool(factory_calls), expect_factory)
                pages = self._opened_pages(browser)
                self.assertEqual(len(pages), 1)
                self.assertEqual(len(pages[0].goto_calls), 1)

    async def test_template_supports_page_none(self) -> None:
        tmpl = Template(None, self.URL, website_type=WebsiteType.GOOGLE_DRIVE)
        self.assertEqual(tmpl.fileID, "LAZY_1")
        self.assertEqual(await tmpl.get_file_name(), "LAZY_1")
        self.assertEqual(await Template(None).get_file_name(), "document")


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
        with patch("scrape.process_images.base64.b64decode", return_value=b"\xff\xd8\xff\xe0"):
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
            with patch("scrape.pdf_scraper.urlopen",
                return_value=FakeHTTPResponse(200, "text/html; charset=utf-8", b"<html></html>"),
            ):
                path_html, status_html = await pdf_scraper.direct_download(
                    "FILE_HTML", "ViewOnly.pdf", output_dir=out_dir
                )
            self.assertIsNone(path_html)
            self.assertEqual(status_html, 404)

            # 2. Subsequent download of the same name gets the clean 'ViewOnly.pdf' name
            with patch("scrape.pdf_scraper.urlopen",
                return_value=FakeHTTPResponse(200, "application/pdf", b"%PDF-1.4"),
            ):
                path_pdf, status_pdf = await pdf_scraper.direct_download(
                    "FILE_PDF", "ViewOnly.pdf", output_dir=out_dir
                )
            self.assertEqual(status_pdf, 200)
            self.assertEqual(path_pdf, out_dir / "ViewOnly.pdf")
            self.assertEqual((out_dir / "ViewOnly.pdf").read_bytes(), b"%PDF-1.4")

            # 3. ggdoc_scraper.direct_download writes docx via asyncio.to_thread
            with patch("scrape.ggdoc_scraper.urlopen",
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
    _CONFIG_ATTRS = (
        "BROWSER_HEADLESS",
        "USE_GOOGLE_DRIVE_API",
        "GOOGLE_API_KEY",
        "MAX_CONCURENT_FOLDER_NODE_VISIT",
        "FOLDER_TIMEOUT",
        "MAX_FOLDER_DEPTH",
        "SCRAPE_RETRY_COUNT",
        "MAX_CONCURRENT_IMG_API",
    )

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._saved_config = {
            attr: getattr(config, attr) for attr in self._CONFIG_ATTRS
        }

    def tearDown(self) -> None:
        for attr, value in self._saved_config.items():
            setattr(config, attr, value)

    @staticmethod
    def _make_success_result(url: str, out_dir: Path) -> BatchScrapeResult:
        link = ClassifiedLink(
            url=url,
            website_type=WebsiteType.GOOGLE_DRIVE,
            document_type=DocumentType.PDF,
        )
        return BatchScrapeResult(
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
        self.assertIn("#Errors:", res.output)
        self.assertIn("#Failures:", res.output)
        self.assertIn("Total: 1 | Fail: 0", res.output)
        self.assertNotIn("doc.pdf", res.output)

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
        self.assertNotIn(url_ok, res.output)
        self.assertNotIn("ok.pdf", res.output)
        self.assertIn("#Errors:", res.output)
        self.assertIn("#Failures:", res.output)
        self.assertIn("No.1", res.output)
        self.assertIn(f"URL: {url_fail}", res.output)
        self.assertIn("Website/DocumentType: GOOGLE_DRIVE.PDF", res.output)
        self.assertIn("Log: Download failed", res.output)
        self.assertIn("Total: 2 | Fail: 1", res.output)

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

    def test_cli_new_options_long_and_short_flags_update_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            out_dir = Path(tmp_dir) / "out"
            url = "https://drive.google.com/file/d/123/view"
            txt_file.write_text(url + "\n", encoding="utf-8")
            fake_result = self._make_success_result(url, out_dir)

            captured_configs: list[BatchConfig] = []

            async def fake_run(
                scraper_self: BatchScraper, source: Path, **kwargs: object
            ) -> BatchScrapeResult:
                captured_configs.append(scraper_self.config)
                return fake_result

            # Long flags
            config.BROWSER_HEADLESS = True
            config.USE_GOOGLE_DRIVE_API = False
            config.GOOGLE_API_KEY = "OLD_KEY"
            with patch.object(BatchScraper, "run", new=fake_run):
                res_long = self.runner.invoke(
                    app,
                    [
                        str(txt_file),
                        "--headless-disable",
                        "--use-gg-api",
                        "LONG_API_KEY",
                        "--concurrent-folder",
                        "4",
                        "--folder-timeout",
                        "25",
                        "--max-folder-depth",
                        "12",
                        "--max-retry",
                        "7",
                        "--max-concurrent-img-api",
                        "15",
                    ],
                )

            self.assertEqual(res_long.exit_code, 0, msg=res_long.output)
            self.assertFalse(config.BROWSER_HEADLESS)
            self.assertFalse(captured_configs[-1].headless)
            self.assertFalse(captured_configs[-1].browser.headless)
            self.assertTrue(config.USE_GOOGLE_DRIVE_API)
            self.assertEqual(config.GOOGLE_API_KEY, "LONG_API_KEY")
            self.assertEqual(config.MAX_CONCURENT_FOLDER_NODE_VISIT, 4)
            self.assertEqual(config.FOLDER_TIMEOUT, 25)
            self.assertEqual(config.MAX_FOLDER_DEPTH, 12)
            self.assertEqual(config.SCRAPE_RETRY_COUNT, 7)
            self.assertEqual(config.MAX_CONCURRENT_IMG_API, 15)

            # Short flags at boundary values
            config.BROWSER_HEADLESS = True
            config.USE_GOOGLE_DRIVE_API = False
            config.GOOGLE_API_KEY = "OLD_KEY"
            with patch.object(BatchScraper, "run", new=fake_run):
                res_short = self.runner.invoke(
                    app,
                    [
                        str(txt_file),
                        "-hd",
                        "-ugga",
                        "SHORT_API_KEY",
                        "-cf",
                        "1",
                        "-ft",
                        "1",
                        "-mfd",
                        "100",
                        "-mr",
                        "0",
                        "-mcia",
                        "30",
                    ],
                )

            self.assertEqual(res_short.exit_code, 0, msg=res_short.output)
            self.assertFalse(config.BROWSER_HEADLESS)
            self.assertFalse(captured_configs[-1].headless)
            self.assertTrue(config.USE_GOOGLE_DRIVE_API)
            self.assertEqual(config.GOOGLE_API_KEY, "SHORT_API_KEY")
            self.assertEqual(config.MAX_CONCURENT_FOLDER_NODE_VISIT, 1)
            self.assertEqual(config.FOLDER_TIMEOUT, 1)
            self.assertEqual(config.MAX_FOLDER_DEPTH, 100)
            self.assertEqual(config.SCRAPE_RETRY_COUNT, 0)
            self.assertEqual(config.MAX_CONCURRENT_IMG_API, 30)

    def test_cli_use_gg_api_optional_key_parsing_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            out_dir = Path(tmp_dir) / "out"
            url = "https://drive.google.com/file/d/123/view"
            txt_file.write_text(url + "\n", encoding="utf-8")
            fake_result = self._make_success_result(url, out_dir)

            captured_sources: list[Path] = []

            async def fake_run(
                scraper_self: BatchScraper, source: Path, **kwargs: object
            ) -> BatchScrapeResult:
                captured_sources.append(source)
                return fake_result

            # Variants without an API key: enable flag only, keep existing GOOGLE_API_KEY
            no_key_cases = [
                [str(txt_file), "--use-gg-api"],
                ["--use-gg-api", str(txt_file)],
                [str(txt_file), "-ugga"],
                ["-ugga", str(txt_file)],
                ["-ugga", "-hd", str(txt_file)],
                [str(txt_file), "--use-gg-api", "-hd"],
                ["-c", "3", "--use-gg-api", str(txt_file)],
                ["--use-gg-api=", str(txt_file)],
            ]
            for argv in no_key_cases:
                with self.subTest(no_key_argv=argv):
                    config.USE_GOOGLE_DRIVE_API = False
                    config.GOOGLE_API_KEY = "ENV_KEY_UNCHANGED"
                    config.BROWSER_HEADLESS = True
                    captured_sources.clear()
                    with patch.object(BatchScraper, "run", new=fake_run):
                        res = self.runner.invoke(app, argv)
                    self.assertEqual(res.exit_code, 0, msg=f"{argv}: {res.output}")
                    self.assertTrue(config.USE_GOOGLE_DRIVE_API)
                    self.assertEqual(config.GOOGLE_API_KEY, "ENV_KEY_UNCHANGED")
                    self.assertEqual(captured_sources, [txt_file])

            # Variants with an API key: enable flag AND update GOOGLE_API_KEY
            with_key_cases = [
                ([str(txt_file), "--use-gg-api", "KEY_1"], "KEY_1"),
                (["--use-gg-api", "KEY_2", str(txt_file)], "KEY_2"),
                (["--use-gg-api=KEY_3", str(txt_file)], "KEY_3"),
                (["-ugga=KEY_4", str(txt_file)], "KEY_4"),
                (["-ugga", "KEY_5", str(txt_file)], "KEY_5"),
                (["-c", "3", "-ugga", "KEY_6", str(txt_file)], "KEY_6"),
                ([str(txt_file), "-ugga", "KEY_7", "-hd"], "KEY_7"),
            ]
            for argv, expected_key in with_key_cases:
                with self.subTest(with_key_argv=argv):
                    config.USE_GOOGLE_DRIVE_API = False
                    config.GOOGLE_API_KEY = "ENV_KEY_OLD"
                    config.BROWSER_HEADLESS = True
                    captured_sources.clear()
                    with patch.object(BatchScraper, "run", new=fake_run):
                        res = self.runner.invoke(app, argv)
                    self.assertEqual(res.exit_code, 0, msg=f"{argv}: {res.output}")
                    self.assertTrue(config.USE_GOOGLE_DRIVE_API)
                    self.assertEqual(config.GOOGLE_API_KEY, expected_key)
                    self.assertEqual(captured_sources, [txt_file])

    def test_cli_omitting_new_options_preserves_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            out_dir = Path(tmp_dir) / "out"
            url = "https://drive.google.com/file/d/123/view"
            txt_file.write_text(url + "\n", encoding="utf-8")
            fake_result = self._make_success_result(url, out_dir)

            config.BROWSER_HEADLESS = True
            config.USE_GOOGLE_DRIVE_API = False
            config.GOOGLE_API_KEY = "KEEP_ME"
            config.MAX_CONCURENT_FOLDER_NODE_VISIT = 7
            config.FOLDER_TIMEOUT = 18
            config.MAX_FOLDER_DEPTH = 9
            config.SCRAPE_RETRY_COUNT = 4
            config.MAX_CONCURRENT_IMG_API = 11

            async def fake_run(
                scraper_self: BatchScraper, source: Path, **kwargs: object
            ) -> BatchScrapeResult:
                return fake_result

            with patch.object(BatchScraper, "run", new=fake_run):
                res = self.runner.invoke(app, [str(txt_file)])

            self.assertEqual(res.exit_code, 0, msg=res.output)
            self.assertTrue(config.BROWSER_HEADLESS)
            self.assertFalse(config.USE_GOOGLE_DRIVE_API)
            self.assertEqual(config.GOOGLE_API_KEY, "KEEP_ME")
            self.assertEqual(config.MAX_CONCURENT_FOLDER_NODE_VISIT, 7)
            self.assertEqual(config.FOLDER_TIMEOUT, 18)
            self.assertEqual(config.MAX_FOLDER_DEPTH, 9)
            self.assertEqual(config.SCRAPE_RETRY_COUNT, 4)
            self.assertEqual(config.MAX_CONCURRENT_IMG_API, 11)

    def test_cli_rejects_out_of_range_option_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "urls.txt"
            txt_file.write_text(
                "https://drive.google.com/file/d/1/view\n", encoding="utf-8"
            )
            invalid_cases = [
                [str(txt_file), "-cf", "0"],
                [str(txt_file), "-ft", "0"],
                [str(txt_file), "-mfd", "0"],
                [str(txt_file), "-mfd", "101"],
                [str(txt_file), "-mr", "-1"],
                [str(txt_file), "-mr", "11"],
                [str(txt_file), "-mcia", "0"],
                [str(txt_file), "-mcia", "31"],
            ]
            for argv in invalid_cases:
                with self.subTest(invalid_argv=argv):
                    res = self.runner.invoke(app, argv)
                    self.assertNotEqual(res.exit_code, 0, msg=f"Expected failure for {argv}")

    def test_cli_help_documents_all_new_options(self) -> None:
        res = self.runner.invoke(app, ["--help"], env={"COLUMNS": "140"})
        self.assertEqual(res.exit_code, 0, msg=res.output)
        for token in (
            "--headless-disable",
            "-hd",
            "--use-gg-api",
            "-ugga",
            "--concurrent-folder",
            "-cf",
            "--folder-timeout",
            "-ft",
            "--max-folder-depth",
            "-mfd",
            "--max-retry",
            "-mr",
            "--max-concurrent-img-api",
            "-mcia",
        ):
            self.assertIn(token, res.output)

    def test_runtime_config_overrides_validation_and_scoped_restore(self) -> None:
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(concurrent_folder=0)
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(folder_timeout=0)
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(max_folder_depth=101)
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(max_retry=11)
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(max_concurrent_img_api=31)
        with self.assertRaises(ValueError):
            config.RuntimeConfigOverrides(max_retry=True)  # type: ignore[arg-type]

        overrides = config.RuntimeConfigOverrides(
            headless_disable=True,
            use_google_drive_api=True,
            google_api_key="   ",
            folder_timeout=15,
            max_folder_depth=5,
        )
        self.assertIsNone(overrides.google_api_key)
        self.assertEqual(
            overrides.to_config_updates(),
            {
                "BROWSER_HEADLESS": False,
                "USE_GOOGLE_DRIVE_API": True,
                "FOLDER_TIMEOUT": 15,
                "MAX_FOLDER_DEPTH": 5,
            },
        )

        orig_headless = config.BROWSER_HEADLESS
        orig_timeout = config.FOLDER_TIMEOUT
        orig_depth = config.MAX_FOLDER_DEPTH
        with overrides.scoped():
            self.assertFalse(config.BROWSER_HEADLESS)
            self.assertEqual(config.FOLDER_TIMEOUT, 15)
            self.assertEqual(config.MAX_FOLDER_DEPTH, 5)
        self.assertEqual(config.BROWSER_HEADLESS, orig_headless)
        self.assertEqual(config.FOLDER_TIMEOUT, orig_timeout)
        self.assertEqual(config.MAX_FOLDER_DEPTH, orig_depth)

    def test_cli_accepts_single_url_and_preserves_scheme(self) -> None:
        url_cases = [
            ("https://drive.google.com/file/d/123/view", "https://drive.google.com/file/d/123/view"),
            (
                "https://drive.google.com/file/d/123/view?usp=sharing&authuser=0",
                "https://drive.google.com/file/d/123/view?usp=sharing&authuser=0",
            ),
            (
                '"https://drive.google.com/drive/folders/FOLDER_ABC"',
                "https://drive.google.com/drive/folders/FOLDER_ABC",
            ),
            (
                "http://docs.google.com/document/d/DOC_1/edit",
                "http://docs.google.com/document/d/DOC_1/edit",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir) / "out"
            for raw_arg, expected_url in url_cases:
                with self.subTest(url_arg=raw_arg):
                    captured_sources: list[object] = []
                    fake_result = self._make_success_result(expected_url, out_dir)

                    async def fake_run(
                        scraper_self: BatchScraper, source: object, **kwargs: object
                    ) -> BatchScrapeResult:
                        captured_sources.append(source)
                        return fake_result

                    with patch.object(BatchScraper, "run", new=fake_run):
                        res = self.runner.invoke(app, [raw_arg, "-o", str(out_dir)])

                    self.assertEqual(res.exit_code, 0, msg=res.output)
                    self.assertEqual(captured_sources, [expected_url])
                    self.assertIsInstance(captured_sources[0], str)

    def test_cli_single_url_with_optional_use_gg_api_flag(self) -> None:
        folder_url = "https://drive.google.com/drive/folders/FOLDER_XYZ"
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir) / "out"
            fake_result = self._make_success_result(folder_url, out_dir)
            captured_sources: list[object] = []

            async def fake_run(
                scraper_self: BatchScraper, source: object, **kwargs: object
            ) -> BatchScrapeResult:
                captured_sources.append(source)
                return fake_result

            cases = [
                (["--use-gg-api", folder_url], "EXISTING_KEY"),
                ([folder_url, "--use-gg-api"], "EXISTING_KEY"),
                (["-ugga", "CLI_KEY_1", folder_url], "CLI_KEY_1"),
                ([folder_url, "-ugga", "CLI_KEY_2"], "CLI_KEY_2"),
                (["--use-gg-api=CLI_KEY_3", folder_url], "CLI_KEY_3"),
            ]
            for argv, expected_key in cases:
                with self.subTest(argv=argv):
                    config.USE_GOOGLE_DRIVE_API = False
                    config.GOOGLE_API_KEY = "EXISTING_KEY"
                    captured_sources.clear()
                    with patch.object(BatchScraper, "run", new=fake_run):
                        res = self.runner.invoke(app, argv)
                    self.assertEqual(res.exit_code, 0, msg=res.output)
                    self.assertTrue(config.USE_GOOGLE_DRIVE_API)
                    self.assertEqual(config.GOOGLE_API_KEY, expected_key)
                    self.assertEqual(captured_sources, [folder_url])

    def test_resolve_cli_source_validates_files_and_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_file = Path(tmp_dir) / "links.txt"
            txt_file.write_text("https://drive.google.com/file/d/1/view\n", encoding="utf-8")

            self.assertEqual(resolve_cli_source(str(txt_file)), txt_file)
            self.assertEqual(
                resolve_cli_source("https://drive.google.com/file/d/1/view"),
                "https://drive.google.com/file/d/1/view",
            )
            self.assertEqual(
                resolve_cli_source("'https://drive.google.com/file/d/1/view'"),
                "https://drive.google.com/file/d/1/view",
            )

            # ClassifierLinkFilterer parses a resolved single URL cleanly
            raw_urls, classified = ClassifierLinkFilterer().filter_and_classify(
                resolve_cli_source("https://drive.google.com/file/d/1/view")
            )
            self.assertEqual(raw_urls, ["https://drive.google.com/file/d/1/view"])
            self.assertEqual(len(classified), 1)
            self.assertEqual(classified[0].website_type, WebsiteType.GOOGLE_DRIVE)

            for bad in ("", "   ", "https://", "http://", str(Path(tmp_dir) / "missing.txt"), tmp_dir):
                with self.subTest(bad_source=bad):
                    with self.assertRaises(ValueError):
                        resolve_cli_source(bad)

            malformed_res = self.runner.invoke(app, ["https://"])
            self.assertEqual(malformed_res.exit_code, 1)


class TestRichBatchReporterFormat(unittest.IsolatedAsyncioTestCase):
    def test_report_format_shows_errors_and_failures_only_without_wrapping_urls(self) -> None:
        buf = io.StringIO()
        test_console = Console(file=buf, width=50, force_terminal=False, color_system=None)
        reporter = RichBatchReporter(test_console, enable_live_bar=False)

        ok_link = ClassifiedLink(
            url="https://drive.google.com/file/d/OK_FILE_ID/view",
            website_type=WebsiteType.GOOGLE_DRIVE,
            document_type=DocumentType.PDF,
        )
        fail_link_1 = ClassifiedLink(
            url="https://drive.google.com/file/d/1mEvlHDjbEgke1tWHXo-XwU9FiGXeGa2A/view",
            website_type=WebsiteType.GOOGLE_DRIVE,
            document_type=DocumentType.PDF,
        )
        fail_link_2 = ClassifiedLink(
            url="https://docs.google.com/document/d/SOME_LONG_DOC_ID_1234567890/edit",
            website_type=WebsiteType.GOOGLE_DOC,
            document_type=None,
        )

        batch_result = BatchScrapeResult(
            raw_urls=[ok_link.url, fail_link_1.url, fail_link_2.url],
            classified_links=[ok_link, fail_link_1, fail_link_2],
            results=[
                ScrapeItemResult(
                    link=ok_link,
                    output_path=Path("output/SuccessDoc.pdf"),
                ),
                ScrapeItemResult(
                    link=fail_link_1,
                    output_path=None,
                    error="Could not capture viewer parameters from the page: id, fssid",
                ),
                ScrapeItemResult(
                    link=fail_link_2,
                    output_path=None,
                    error="Error [503]: Service unavailable",
                ),
            ],
            errors=[
                "Unsupported website domain skipped: https://example.com/bad",
                "Inaccessible folder (HTTP 403): https://drive.google.com/drive/folders/SECRET",
            ],
        )

        reporter.print_summary(batch_result)
        output = buf.getvalue()

        expected = (
            "#Errors:\n"
            "Unsupported website domain skipped: https://example.com/bad\n"
            "Inaccessible folder (HTTP 403): https://drive.google.com/drive/folders/SECRET\n"
            "#Failures:\n"
            "----------------------------\n"
            "No.1\n"
            "URL: https://drive.google.com/file/d/1mEvlHDjbEgke1tWHXo-XwU9FiGXeGa2A/view\n"
            "Website/DocumentType: GOOGLE_DRIVE.PDF\n"
            "Log: Could not capture viewer parameters from the page: id, fssid\n"
            "----------------------------\n"
            "No.2\n"
            "URL: https://docs.google.com/document/d/SOME_LONG_DOC_ID_1234567890/edit\n"
            "Website/DocumentType: GOOGLE_DOC.UNKNOWN\n"
            "Log: Error [503]: Service unavailable\n"
            "----------------------------\n"
            "Total: 3 | Fail: 2\n"
        )
        self.assertEqual(output, expected)

    def test_report_format_when_no_errors_and_no_failures(self) -> None:
        buf = io.StringIO()
        test_console = Console(file=buf, width=80, force_terminal=False, color_system=None)
        reporter = RichBatchReporter(test_console, enable_live_bar=False)

        ok_link = ClassifiedLink(
            url="https://drive.google.com/file/d/OK_ONLY/view",
            website_type=WebsiteType.GOOGLE_DRIVE,
            document_type=DocumentType.PDF,
        )
        batch_result = BatchScrapeResult(
            raw_urls=[ok_link.url],
            classified_links=[ok_link],
            results=[ScrapeItemResult(link=ok_link, output_path=Path("output/ok.pdf"))],
        )

        reporter.print_summary(batch_result)
        self.assertEqual(
            buf.getvalue(),
            "#Errors:\nNone\n#Failures:\nNone\nTotal: 1 | Fail: 0\n",
        )

    async def test_batch_run_collects_phase1_and_phase2_errors_into_report(self) -> None:
        DriveFolderScraper._reset_instance()
        shared_browser = FakeAsyncBrowser()

        @asynccontextmanager
        async def fake_browser_factory():
            yield shared_browser

        async def fake_folder_scrape(
            folder_urls: list[str], browser: object = None
        ) -> list[ClassifiedLink]:
            folder_scraper.log_error(
                "Inaccessible folder (HTTP 403): https://drive.google.com/drive/folders/BROKEN. Skipping."
            )
            folder_scraper.log_unsupported_element("UNSUPPORTED_ID", "0")
            return []

        async def fake_template_create(page: FakeAsyncPage, url: str) -> MagicMock:
            tmpl = MagicMock(spec=Template)
            tmpl.website_type = WebsiteType.GOOGLE_DRIVE
            tmpl.document_type = DocumentType.PDF
            tmpl.fileID = "GOOD_1"
            return tmpl

        async def fake_scrape_fn(page: FakeAsyncPage, tmpl: Template) -> Path:
            return Path("GOOD_1.pdf")

        buf = io.StringIO()
        test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)
        reporter = RichBatchReporter(test_console, enable_live_bar=False)

        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_cfg = BatchConfig(output_dir=Path(tmp_dir), concurrency=2)
            orchestrator = BatchScraper(
                filterer=ClassifierLinkFilterer(),
                folder_expander=DriveFolderExpander(folder_scraper_fn=fake_folder_scrape),
                scraper=PlaywrightAsyncLinkScraper(
                    template_factory=fake_template_create,
                    scrape_fn=fake_scrape_fn,
                ),
                config=batch_cfg,
                reporter=reporter,
                browser_factory=fake_browser_factory,  # type: ignore[arg-type]
            )

            result = await orchestrator.run(
                [
                    "https://example.com/not-supported",
                    "https://drive.google.com/drive/folders/BROKEN",
                    "https://drive.google.com/file/d/GOOD_1/view",
                ]
            )

        self.assertEqual(len(result.errors), 3)
        output = buf.getvalue()
        self.assertIn("#Errors:", output)
        self.assertIn("Unsupported website domain skipped: https://example.com/not-supported", output)
        self.assertIn("Inaccessible folder (HTTP 403)", output)
        self.assertIn("Unsupported URL: https://drive.google.com/open?id=UNSUPPORTED_ID&authuser=0 | Tab Index: 0", output)
        self.assertIn("#Failures:\nNone\nTotal: 1 | Fail: 0", output)

    async def test_open_shared_browser_uses_persistent_context_and_stealth_options(self) -> None:
        fake_persistent_ctx = FakeAsyncContext(accept_downloads=True)
        fake_persistent_ctx.add_init_script = AsyncMock()  # type: ignore[attr-defined]

        fake_chromium = MagicMock()
        fake_chromium.launch_persistent_context = AsyncMock(
            return_value=fake_persistent_ctx
        )

        fake_pw = MagicMock()
        fake_pw.chromium = fake_chromium

        @asynccontextmanager
        async def fake_async_playwright():
            yield fake_pw

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch("scrape.scrape.async_playwright", side_effect=fake_async_playwright),
        ):
            profile_dir = Path(tmp_dir) / "profile"
            async with open_shared_browser(
                headless=True, user_data_dir=profile_dir
            ) as shared_ctx:
                self.assertIs(shared_ctx, fake_persistent_ctx)
                fake_chromium.launch_persistent_context.assert_awaited_once()
                call_kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(call_kwargs["user_data_dir"], str(profile_dir))
                self.assertTrue(call_kwargs["accept_downloads"])
                self.assertIn(
                    "--disable-blink-features=AutomationControlled",
                    call_kwargs["args"],
                )
                self.assertIn(
                    "--enable-automation",
                    call_kwargs["ignore_default_args"],
                )
                self.assertNotIn("HeadlessChrome", call_kwargs["user_agent"])
                fake_persistent_ctx.add_init_script.assert_awaited_once()

                # Scraping a link on the persistent context closes the tab, not the context
                scraper = PlaywrightAsyncLinkScraper(
                    scrape_fn=AsyncMock(return_value=Path("doc.pdf"))
                )
                link = ClassifiedLink(
                    url="https://drive.google.com/file/d/P1/view",
                    website_type=WebsiteType.GOOGLE_DRIVE,
                    document_type=DocumentType.PDF,
                )
                item_res = await scraper.scrape_one(
                    link,
                    shared_ctx,  # type: ignore[arg-type]
                    BatchConfig(output_dir=Path(tmp_dir) / "out", concurrency=1),
                )
                self.assertTrue(item_res.succeeded)
                self.assertFalse(fake_persistent_ctx.closed)
                self.assertTrue(fake_persistent_ctx.pages[0].closed)

            self.assertTrue(fake_persistent_ctx.closed)

    async def test_open_shared_browser_uses_custom_browser_config_from_config_module(self) -> None:
        fake_persistent_ctx = FakeAsyncContext(accept_downloads=False)
        fake_persistent_ctx.add_init_script = AsyncMock()  # type: ignore[attr-defined]

        fake_chromium = MagicMock()
        fake_chromium.launch_persistent_context = AsyncMock(
            return_value=fake_persistent_ctx
        )

        fake_pw = MagicMock()
        fake_pw.chromium = fake_chromium

        @asynccontextmanager
        async def fake_async_playwright():
            yield fake_pw

        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            patch("scrape.scrape.async_playwright", side_effect=fake_async_playwright),
        ):
            profile_dir = Path(tmp_dir) / "custom_profile"
            custom_browser = config.BrowserConfig(
                user_data_dir=profile_dir,
                headless=False,
                accept_downloads=False,
                args=("--no-sandbox",),
                ignore_default_args=(),
                user_agent="CustomAgent/1.0",
                viewport_width=1280,
                viewport_height=720,
                locale="vi-VN",
                stealth_init_script="",
                wait_until="load",
            )
            async with open_shared_browser(browser_config=custom_browser) as shared_ctx:
                self.assertIs(shared_ctx, fake_persistent_ctx)
                call_kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
                self.assertEqual(call_kwargs["user_data_dir"], str(profile_dir))
                self.assertFalse(call_kwargs["headless"])
                self.assertFalse(call_kwargs["accept_downloads"])
                self.assertEqual(call_kwargs["args"], ["--no-sandbox"])
                self.assertEqual(call_kwargs["ignore_default_args"], [])
                self.assertEqual(call_kwargs["user_agent"], "CustomAgent/1.0")
                self.assertEqual(call_kwargs["viewport"], {"width": 1280, "height": 720})
                self.assertEqual(call_kwargs["locale"], "vi-VN")
                fake_persistent_ctx.add_init_script.assert_not_awaited()


class TestConfigSeparation(unittest.IsolatedAsyncioTestCase):
    def test_batch_and_browser_config_live_in_config_and_reexport_in_scrape(self) -> None:
        self.assertIs(BatchConfig, config.BatchConfig)
        self.assertIs(BrowserConfig, config.BrowserConfig)

        default_batch = config.BatchConfig()
        self.assertEqual(default_batch.output_dir, Path(config.OUTPUT_DIR))
        self.assertEqual(default_batch.concurrency, config.CONCURRENT_SCRAPER)
        self.assertEqual(default_batch.scrape_type, config.SCRAPE_TYPE)
        self.assertEqual(default_batch.headless, config.BROWSER_HEADLESS)
        self.assertEqual(default_batch.browser.user_data_dir, Path(config.USER_DATA_DIR))
        self.assertEqual(default_batch.browser.viewport, config.BROWSER_VIEWPORT)
        self.assertEqual(default_batch.browser.locale, config.BROWSER_LOCALE)
        self.assertEqual(default_batch.browser.wait_until, config.BROWSER_WAIT_UNTIL)

        with self.assertRaises(FrozenInstanceError):
            default_batch.concurrency = 9  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            default_batch.browser.locale = "fr-FR"  # type: ignore[misc]

    def test_batch_and_browser_config_validation_and_dynamic_module_defaults(self) -> None:
        with self.assertRaises(ValueError):
            config.BrowserConfig(viewport_width=0)
        with self.assertRaises(ValueError):
            config.BrowserConfig(channel="   ")
        with self.assertRaises(ValueError):
            config.BrowserConfig(wait_until="invalid_state")
        with self.assertRaises(TypeError):
            config.BrowserConfig(args="--no-sandbox")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            config.BatchConfig(concurrency=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            config.BatchConfig(output_dir="   ")

        cfg_via_browser = config.BatchConfig(
            browser=config.BrowserConfig(headless=False, locale="fr-FR")
        )
        self.assertFalse(cfg_via_browser.headless)
        self.assertFalse(cfg_via_browser.browser.headless)
        self.assertEqual(cfg_via_browser.browser.locale, "fr-FR")

        cfg_via_flag = config.BatchConfig(headless=False)
        self.assertFalse(cfg_via_flag.headless)
        self.assertFalse(cfg_via_flag.browser.headless)

        with (
            patch.object(config, "OUTPUT_DIR", "custom_out"),
            patch.object(config, "CONCURRENT_SCRAPER", 9),
            patch.object(config, "SCRAPE_TYPE", "S"),
            patch.object(config, "BROWSER_HEADLESS", False),
            patch.object(config, "BROWSER_WAIT_UNTIL", "load"),
        ):
            dynamic_cfg = config.BatchConfig.from_config()
            self.assertEqual(dynamic_cfg.output_dir, Path("custom_out"))
            self.assertEqual(dynamic_cfg.concurrency, 9)
            self.assertEqual(dynamic_cfg.scrape_type, "s")
            self.assertFalse(dynamic_cfg.headless)
            self.assertEqual(dynamic_cfg.browser.wait_until, "load")

    async def test_link_scraper_respects_browser_config_wait_until_and_accept_downloads(self) -> None:
        fake_browser = FakeAsyncBrowser()
        with tempfile.TemporaryDirectory() as tmp_dir:
            batch_cfg = config.BatchConfig(
                output_dir=Path(tmp_dir),
                browser=config.BrowserConfig(
                    accept_downloads=False,
                    wait_until="networkidle",
                ),
            )
            scraper = PlaywrightAsyncLinkScraper(
                scrape_fn=AsyncMock(return_value=Path("doc.pdf"))
            )
            link = ClassifiedLink(
                url="https://drive.google.com/file/d/CFG_1/view",
                website_type=WebsiteType.GOOGLE_DRIVE,
                document_type=DocumentType.PDF,
            )
            res = await scraper.scrape_one(
                link, fake_browser, batch_cfg  # type: ignore[arg-type]
            )

        self.assertTrue(res.succeeded)
        self.assertEqual(len(fake_browser.contexts), 1)
        self.assertFalse(fake_browser.contexts[0].accept_downloads)
        self.assertEqual(
            fake_browser.contexts[0].pages[0].goto_calls,
            [("https://drive.google.com/file/d/CFG_1/view", "networkidle")],
        )

    def test_browser_config_new_headless_channel_and_args(self) -> None:
        headless_cfg = config.BrowserConfig(headless=True)
        headless_kwargs = headless_cfg.to_persistent_context_kwargs()
        self.assertEqual(headless_kwargs["channel"], "chromium")
        self.assertIn("--headless=new", headless_kwargs["args"])
        self.assertIn("Chrome/156.0.0.0", headless_kwargs["user_agent"])

        headed_cfg = config.BrowserConfig(headless=False, channel=None)
        headed_kwargs = headed_cfg.to_persistent_context_kwargs()
        self.assertNotIn("channel", headed_kwargs)
        self.assertNotIn("--headless=new", headed_kwargs["args"])


if __name__ == "__main__":
    unittest.main()
