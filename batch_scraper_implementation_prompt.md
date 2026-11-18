# Implementation Prompt: Async Batch Scraper & Playwright `async_api` Migration

You are tasked with designing, refactoring, and implementing the **Batch Scraping Module** (`batch_scraper.py`) and migrating the existing scraping pipeline to **`playwright.async_api`** so all concurrent scraping tasks share a **single Chromium `Browser` instance** bounded by `asyncio.Semaphore(config.CONCURRENT_SCRAPER)`.

Follow **OOP and SOLID principles** (see `.agents/skills/oop-solid-architect/SKILL.md` in this repo) and adhere strictly to every specification below.

---

## 1. Current Codebase Context

Read these files first to understand the exact existing APIs and state:
- `config.py` — Already contains `CONCURRENT_SCRAPER`, `OUTPUT_DIR`, `SCRAPE_TYPE`, `API_CONCURRENCY`, `MAX_CONCURENT_FOLDER_EXPLORER`, `MAX_CONCURENT_FOLDER_NODE_VISIT`, `USE_GOOGLE_DRIVE_API`, `GOOGLE_API_KEY`, etc.
- `classifiers/` (`models.py`, `input_parser.py`, `pipeline.py`, `page_doc_classifier.py`) — Provides `ClassifiedLink`, `DocumentType`, `WebsiteType`, `LinkInputParser`, and `LinkClassificationPipeline`.
- `folder_scraper.py` — Provides `DriveFolderScraper` (thread-safe Singleton with `queue`, `visited_folder_ids`, `discovered_file_links`, `_drain_queue_concurrently`, `FolderScrapeSettings`, `PlaywrightFolderElementReader`, `launch_chromium`, `_open_shared_context`, and `_scrape_by_api`).
- `scrape.py` — Provides `start_scraping(page, template_info, scribd_scraper=None) -> Path` (currently sync; top-level demo is guarded by `if __name__ == "__main__":`).
- `pdf_scraper.py`, `ggdoc_scraper.py`, `template_info.py`, `process_images.py`, `tracker.py` — Currently synchronous (`playwright.sync_api` and blocking `urllib.request.urlopen`).
- `batch_scraper.py` & `test_batch_scraper.py` — Currently contain an early **sync `ThreadPoolExecutor` draft** that must be **replaced/refactored** into the async shared-browser architecture below.

---

## 2. Core Design Decisions & Requirements

1. **Full Async Migration (No Dual Sync Wrappers Needed):**
   - Fully migrate Playwright-dependent functions in `process_images.py`, `template_info.py`, `pdf_scraper.py`, `ggdoc_scraper.py`, `scrape.py` (and `classifiers/page_doc_classifier.py` if used with `AsyncPage`) to `async def` using `playwright.async_api`.
   - Update each file's `if __name__ == "__main__":` entrypoint to run via `asyncio.run(...)`.
   - **Never block the event loop:** `pdf_scraper.direct_download` and `ggdoc_scraper.direct_download` currently use blocking `urllib.request.urlopen`. Wrap blocking HTTP calls in `await asyncio.to_thread(...)` (or migrate them to an async HTTP client) so concurrent workers do not stall each other.
   - **Note on `Template` (`template_info.py`):** Python `__init__` cannot be `async`. Provide an async factory method `await Template.create(page: AsyncPage, url: str | None = None) -> Template` (and make `get_file_name`, `get_total_pages`, and internal DOM checks `async def`).

2. **Phase 1 — Filtering & Pre-Classification (`raw_urls` -> `classified_links`):**
   - Accept either a `.txt` file (`Path | str`) or `Sequence[str]` / `list[str]`.
   - Parse, strip comments, and extract normalized URLs into `raw_urls: list[str]` via `classifiers.LinkInputParser`.
   - Classify URLs using `classifiers.LinkClassificationPipeline.preclassify(raw_urls)` to produce `classified_links: list[ClassifiedLink]`.
   - **Important performance rule:** Do **not** launch Playwright in Phase 1. Links whose `DocumentType` requires opening the DOM (e.g., Google Drive `/file/d/.../view` links where `document_type is None`) remain `document_type=None` after Phase 1 and are resolved in Phase 3 once the page is already open.

3. **Phase 2 — Folder Scraping (`DriveFolderScraper` Queue Integration):**
   - Inspect `classified_links` for entries with `DocumentType.FOLDER`.
   - Keep non-folder links separate from folder links.
   - If folder links exist, initialize the `DriveFolderScraper` singleton (`DriveFolderScraper.get_instance()`), reset its session state cleanly, **seed all folder links into `DriveFolderScraper.queue`** (at depth `0`, marking each root folder ID in `visited_folder_ids` and validating URLs via `validate_drive_folder_url`), and let `DriveFolderScraper` drain the queue using its built-in concurrent crawl (`_drain_queue_concurrently`) — or Drive API mode when `should_use_drive_api()` is true.
   - Add a clean public async helper on `folder_scraper.py` (e.g., `async def scrape_many_classified(folder_urls: Sequence[str], browser: AsyncBrowser | None = None) -> list[ClassifiedLink]`) so Phase 2 can optionally reuse the single shared `AsyncBrowser` and avoid nested `asyncio.run(...)` calls.
   - Individual inaccessible/failing folders must log errors and be skipped without aborting the rest of the queue or batch (matching `DriveFolderScraper._visit_folder` behavior).
   - Concatenate the non-folder links with the `list[ClassifiedLink]` returned by `DriveFolderScraper`, **deduplicating by URL while preserving first-seen order**.

4. **Phase 3 — Concurrent Scraping with 1 Shared Browser:**
   - Launch **exactly one** Chromium `Browser` instance (`playwright.async_api.async_playwright`) for the batch run.
   - Bound concurrency with `asyncio.Semaphore(max_concurrency)` where `max_concurrency` defaults to `config.CONCURRENT_SCRAPER` (overridable via constructor / CLI).
   - Each concurrent worker task acquires the semaphore, creates an isolated `BrowserContext` (`accept_downloads=True`) and `Page`, navigates to `link.url` (converting Google Docs URLs via `ggdoc_scraper.switch_url_to_AllTabsView` when applicable, just like `scrape.py`), resolves `await Template.create(page, target_url)` (which handles DOM-based `DocumentType` classification), and calls `await start_scraping(page, template_info)`.
   - Always close the worker's `page` and `context` in a `finally:` block so memory stays low and cookies/downloads stay isolated per task.
   - Catch per-link exceptions and return a structured `ScrapeItemResult(link=..., output_path=..., error=...)` so one bad URL never crashes the other workers.

5. **Safe Output Filename Deduplication:**
   - Multiple concurrent tasks (especially across expanded folders) may share identical document names.
   - Implement a thread/async-safe filename reservation helper (using an `asyncio.Lock` or deterministic naming) before writing to `OUTPUT_DIR`.
   - If a target path already exists or is reserved by an active task, auto-deduplicate safely (e.g., `stem_{file_id}.ext` or `stem_{counter}.ext`) so concurrent tasks never overwrite or corrupt each other's output files.
   - Propagate the final resolved output path back into `ScrapeItemResult.output_path`.

6. **Concurrency-Safe Progress Tracking (`tracker.py` & Batch UI):**
   - `tracker.py` currently uses a global singleton `scrape_progress` with a single Rich `Live` display (`reset()`, `increment()`, `finish()`), which collides when multiple documents scrape concurrently.
   - Refactor `tracker.py` so single-document scraping and batch scraping do not collide (e.g., instantiate per-task `ScrapeProgress` objects or inject an optional progress reporter/callback into `pdf_scraper` / `ggdoc_scraper`, disabling global `Live` display during batch runs).
   - In `batch_scraper.py`, provide a clean **batch-level Rich progress/status display** showing overall progress (`completed / total`), active URLs, and a final `OK` / `FAIL` summary table.

7. **OOP & SOLID Architecture (`batch_scraper.py`):**
   - **Data Models (DTOs):**
     - `BatchConfig` (or settings object): `output_dir: Path`, `concurrency: int`, `scrape_type: str` (avoids mutating global `config` when CLI overrides are passed).
     - `ScrapeItemResult(link: ClassifiedLink, output_path: Path | None = None, error: str | None = None)` with `@property def succeeded(self) -> bool`.
     - `BatchScrapeResult(raw_urls: list[str], classified_links: list[ClassifiedLink], results: list[ScrapeItemResult])` with `succeeded_count` and `failed_count`.
   - **Ports / Abstractions (`Protocol` or `ABC` — ISP & DIP):**
     - `LinkFilterer`: `filter_and_classify(source: Path | str | Sequence[str]) -> tuple[list[str], list[ClassifiedLink]]`
     - `FolderExpander`: `async def expand(links: Sequence[ClassifiedLink], browser: AsyncBrowser | None = None) -> list[ClassifiedLink]`
     - `LinkScraper`: `async def scrape_one(link: ClassifiedLink, browser: AsyncBrowser, config: BatchConfig) -> ScrapeItemResult`
   - **Concrete Implementations (SRP):**
     - `ClassifierLinkFilterer` (Phase 1)
     - `DriveFolderExpander` (Phase 2 — pushes all folder links into `DriveFolderScraper.queue`)
     - `PlaywrightAsyncLinkScraper` (Phase 3 — shares the single `AsyncBrowser`, manages isolated `BrowserContext`/`Page`, delegates to `Template.create` + `start_scraping`)
   - **Orchestrator (`BatchScraper`):**
     - Constructor-injects `LinkFilterer`, `FolderExpander`, `LinkScraper`, and `BatchConfig`.
     - `async def run(source: Path | str | Sequence[str]) -> BatchScrapeResult` (plus a convenience `run_sync(...)` helper if callers want a one-line sync entrypoint).

8. **CLI Integration (`typer`):**
   - Expose a clean `typer` CLI in `batch_scraper.py` (ready to be mounted as a subcommand in a top-level CLI later):
     - `source`: Path to `.txt` file (or support multiple URLs/arguments if desired, keeping `.txt` path as primary positional argument).
     - `--concurrency`, `-c` (`int`, `min=1`, default `config.CONCURRENT_SCRAPER`)
     - `--output`, `-o` (`Path`, default `Path(config.OUTPUT_DIR)`)
     - `--scrape-type`, `-t` (`str`, `"d"` or `"s"`, default `config.SCRAPE_TYPE`)
   - Exit with non-zero exit code (e.g., `typer.Exit(code=1)`) if any item fails or input is invalid, while still printing the summary of succeeded and failed items.

---

## 3. Deliverables & Verification Checklist

1. **Modified / Created Files:**
   - `process_images.py` (async Playwright `evaluate` helpers)
   - `template_info.py` (async `Template.create` & async inspection methods)
   - `pdf_scraper.py` & `ggdoc_scraper.py` (async Playwright scrapers + non-blocking `direct_download` via `asyncio.to_thread` + safe filename resolution + decoupled `tracker.py`)
   - `tracker.py` (per-task / batch-safe progress tracking)
   - `scrape.py` (`async def start_scraping` + `asyncio.run` in `__main__`)
   - `folder_scraper.py` (support seeding multiple folder links into `DriveFolderScraper.queue` via async helper, optionally sharing `AsyncBrowser`)
   - `batch_scraper.py` (OOP/SOLID async batch scraper + `typer` CLI)
   - `test_batch_scraper.py` (comprehensive `unittest.IsolatedAsyncioTestCase` / `unittest.TestCase` suite using fakes/mocks — no live network required)

2. **Verification Steps to Run After Implementation:**
   - Unit tests for Phase 1 (`.txt` file and `list[str]` parsing + comment stripping + preclassification, leaving `/file/d/.../view` `document_type=None`).
   - Unit tests for Phase 2 (confirming all `DocumentType.FOLDER` links are pushed into `DriveFolderScraper.queue`, failing folders don't abort the batch, and URLs are deduplicated in order).
   - Unit tests for Phase 3 (confirming `asyncio.Semaphore(CONCURRENT_SCRAPER)` caps max concurrent workers, 1 shared `Browser` is reused, context/page cleanup runs even on errors, and colliding filenames are safely deduplicated).
   - Unit tests for CLI flags (`--concurrency`, `--output`, `--scrape-type`) via `typer.testing.CliRunner`.
   - Ensure existing tests across `classifiers/` and `folder_scraper` continue to pass.
