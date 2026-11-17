# Implement a Google Drive Folder Scraper Module

Modify the scraper module in [`folder_scraper.py`](./folder_scraper.py) according to the requirements below.

## Scope & Architectural Overview

Implement a high-performance, robust Google Drive folder scraping module supporting both browser-based crawling (Playwright) and direct REST API scraping (Google Drive v3 API).

The implementation must be designed as a reusable, modular component following Object-Oriented Programming (OOP) and SOLID principles:
- **Single Responsibility (SRP):** Distinct components for browser management, shared context creation, DOM extraction, rate limiting, folder reading, and traversal bookkeeping.
- **Port & Adapter / Dependency Inversion (DIP):** Abstract folder reading protocol (`FolderElementReader`) decoupled from browser crawl execution (`PlaywrightFolderElementReader`).
- **Thread-Safe Singleton:** Scraper instance reuse across the application lifecycle.
- **Settings Encapsulation:** Immutable configuration snapshot (`FolderScrapeSettings`) isolated from global runtime state.

Do not add tests unless explicitly requested. Do not modify unrelated functionality.

Use:
- Python 3.12+
- Playwright Async API (`playwright.async_api`) for concurrent multi-tab browser crawl
- Playwright Sync API (`playwright.sync_api.Page`) for caller-supplied synchronous page crawl
- `requests` with `HTTPAdapter` connection pooling and gzip encoding for Google Drive v3 REST API
- `asyncio` and `concurrent.futures.ThreadPoolExecutor`
- `rich.console.Console` and `typer` for logging and status output
- [`classifiers`](./classifiers/) package (`ClassifiedLink`, `DocumentType`, `DriveElement`, etc.) for document classification and URL generation

---

## Public API

The module must expose two primary public functions supporting both a Google Drive folder URL (`str`) and an existing Playwright `Page` (`playwright.sync_api.Page`):

```python
def scrape(source: str | Page) -> list[str]:
    """Scrapes a Google Drive folder and returns discovered file URLs in first-discovery order."""
    ...

def scrape_classified(source: str | Page) -> list[ClassifiedLink]:
    """Scrapes a Google Drive folder and returns discovered ClassifiedLink objects in first-discovery order."""
    ...
```

Both functions must maintain synchronous public signatures regardless of internal asynchronous execution.

### URL Input (`str`)

When `scrape()` or `scrape_classified()` receives a `str`:
1. Validate that the URL is a valid Google Drive folder URL containing the `folders` path segment. If invalid, raise `ValueError`.
2. Determine execution backend:
   - If `config.USE_GOOGLE_DRIVE_API` is enabled and `config.GOOGLE_API_KEY` is present, scrape using the Google Drive v3 REST API backend.
   - Otherwise, crawl using the Playwright browser backend.
3. In browser mode:
   - Launch a single shared Chromium instance.
   - Open a shared `BrowserContext` with HTTP/2+QUIC connection reuse, TLS session sharing, and asset route blocking.
   - Crawl subfolders concurrently across short-lived tabs up to the concurrency limit.
   - Guarantee clean browser and context shutdown upon completion or failure.
4. Return discovered files in first-discovery order (URLs only for `scrape()`, or `ClassifiedLink` objects for `scrape_classified()`). Folder URLs must not be returned.

### Page Input (`Page`)

When `scrape()` or `scrape_classified()` receives a `Page`:
1. Reuse the existing caller-supplied synchronous Playwright `Page` directly.
2. Do not open, close, or terminate browser resources.
3. Perform sequential breadth-first traversal in-place starting from the current page state.
4. Catch timeouts on the initial folder load gracefully with a warning rather than aborting.
5. Return discovered files in first-discovery order without leaking or closing browser resources.

### Reusable Helpers & Browser Lifecycle

The module must provide separate lifecycle and helper functions:
- `init_browser(headless: bool = True) -> tuple[Playwright, Browser, Page]`: Initializes Playwright, Chromium, and a new page for external synchronous callers.
- `cleanup_browser(playwright: Playwright | None = None, browser: Browser | None = None, page: Page | None = None) -> None`: Safely closes synchronous browser resources without raising exceptions.
- `launch_chromium(headless: bool = True) -> AsyncGenerator[AsyncBrowser, None]`: Async context manager that launches Chromium with optimized arguments and ensures reliable teardown.
- `_open_shared_context(browser: AsyncBrowser) -> AsyncGenerator[AsyncBrowserContext | None, None]`: Async context manager creating a shared `BrowserContext` with `service_workers="block"` and route blocking.
- `_close_quietly(resource: AsyncPage | AsyncBrowserContext | AsyncBrowser) -> None`: Safely closes async resources ignoring errors.
- `validate_drive_folder_url(url: str) -> None`: Validates HTTP/HTTPS scheme, `drive.google.com` host, and presence of the `folders` path segment.
- `extract_folder_id(url: str) -> str | None`: Extracts the Google Drive folder ID following `folders/`.

---

## Configuration

Use configuration settings from [`config.py`](./config.py):

| Setting | Default | Description |
|---|---|---|
| `SCRAPE_TIMEOUT_SECONDS` | `15` | Timeout in seconds for navigation, DOM selectors, and HTTP requests |
| `SCRAPE_RETRY_COUNT` | `2` | Retry attempts for failed/timed-out operations (yielding 3 total attempts) |
| `FOLDER_MAX_DEPTH` | `3` or `5` | Maximum recursive folder traversal depth (initial folder is depth `0`) |
| `MAX_CONCURENT_FOLDER_NODE_VISIT` | `2` to `10` | Concurrency limit for simultaneous open tabs in Playwright browser crawl |
| `MAX_CONCURENT_FOLDER_EXPLORER` | `5` | Worker thread count for level-order BFS in Drive API mode |
| `USE_GOOGLE_DRIVE_API` | `False` | Boolean toggle to enable the Google Drive v3 REST API backend |
| `GOOGLE_API_KEY` | `""` | Google Cloud API key with Drive API v3 enabled |
| `GOOGLE_DRIVE_API_RATE_LIMIT` | `12000` | Max API requests permitted per rate limiting window |
| `GOOGLE_DRIVE_API_RATE_PERIOD_SECONDS` | `60` | Sliding window period in seconds for API rate limiting |

### Settings Data Model

Define an immutable, frozen dataclass to capture runtime settings:

```python
@dataclass(frozen=True)
class FolderScrapeSettings:
    timeout_ms: int = 15_000
    max_retries: int = 2
    max_depth: int = 5
    max_concurrency: int = 2
    headless: bool = True

    @classmethod
    def from_config(cls) -> FolderScrapeSettings:
        ...
```

---

## Core Architecture & OOP Design

### 1. Thread-Safe Singleton: `DriveFolderScraper`

Manage discovery and traversal state through a thread-safe singleton class `DriveFolderScraper`:
- Encapsulates:
  - `discovered_file_urls: list[str]`
  - `discovered_file_links: list[ClassifiedLink]`
  - `seen_file_ids: set[str]` (file deduplication)
  - `visited_folder_ids: set[str]` (folder deduplication)
  - `queue: collections.deque[tuple[str, int]]` (FIFO BFS queue of `(folder_url, depth)`)
  - `settings: FolderScrapeSettings`
- Methods:
  - `get_instance(page: Page | None = None) -> DriveFolderScraper`: Access the singleton instance.
  - `reset(page: Page | None = None) -> None`: Clear all discovery lists, visited sets, and queues.
  - `_record_file(label: DocumentType, file_id: str) -> None`: Deduplicate by `file_id`, generate URLs via `classifiers`, and append to `discovered_file_urls` and `discovered_file_links`.
  - `_enqueue_folder(folder_id: str, depth: int) -> None`: Check against `visited_folder_ids` and `settings.max_depth`; enqueue if within limit.
  - `_ingest_elements(elements: list[DriveElement], depth: int) -> None`: Classify extracted elements; route folders to `_enqueue_folder`, files to `_record_file`, and log unsupported items.
  - `crawl(...)`: Concurrent async crawl via `FolderElementReader`.
  - `scrape(...)`: Sequential sync crawl via supplied `Page`.
  - `scrape_classified(...)`: Return `list[ClassifiedLink]`.

### 2. Port & Adapter: `FolderElementReader`

Decouple folder loading and DOM reading from crawl orchestration:
- **Port:**
  ```python
  class FolderElementReader(Protocol):
      async def read(self, folder_url: str) -> list[DriveElement] | None: ...
  ```
- **Adapter (`PlaywrightFolderElementReader`):**
  - Accepts optional shared `AsyncBrowserContext` (`context`) to reuse HTTP/2+QUIC connections, TLS sessions, and cached Drive JS bundles.
  - Fallback: opens tabs directly on `AsyncBrowser` with route interception if context is omitted.
  - Opens each folder in a short-lived tab (`_open_tab()`).
  - Closes the tab immediately in a `try...finally` block upon extraction or failure to prevent memory leaks.
  - Uses `wait_until="commit"` in `page.goto()` so navigation returns immediately once HTTP response headers arrive, avoiding blocking on heavy Drive JavaScript.
  - Calls `_wait_for_folder_items(page, timeout_ms)` to wait for folder DOM items.
  - Catches `_InaccessibleFolderError` (HTTP >= 400), logs it, and skips immediately without retrying.
  - Retries timeouts and unexpected errors up to `max_retries` with jittered exponential backoff (`_retry_backoff_seconds`).

### 3. Jittered Exponential Backoff: `_retry_backoff_seconds`

Define a backoff calculation function for retries:
```python
def _retry_backoff_seconds(attempt: int) -> float:
    return min(2 ** (attempt - 1), 8) + random.uniform(0.2, 0.7)
```
Apply this backoff sleep between failed attempts in both async (`await asyncio.sleep(...)`) and sync (`time.sleep(...)`) folder reading.

### 4. Thread-Safe Rate Limiter: `RateLimiter`

A sliding-window rate limiter preventing API quota exhaustion:
- Maintains timestamps of requests within `period_seconds`.
- `acquire()` blocks until a slot is free, logging an orange warning when forced to wait.
- Singleton accessor `get_drive_api_rate_limiter()` ensures process-wide quota tracking.

### 5. Async-to-Sync Bridge: `_run_blocking()`

Execute asynchronous coroutines from synchronous entry points:
- If no event loop is running on the current thread, use `asyncio.run()`.
- If an event loop is already active (e.g. Jupyter notebook or an active Playwright sync session), delegate execution to a single-worker `ThreadPoolExecutor` to prevent event loop collision.

---

## Dual-Engine Crawling Strategy

### Engine 1: Google Drive REST API v3 Backend (`_scrape_by_api`)

Used when `config.USE_GOOGLE_DRIVE_API` is True and `config.GOOGLE_API_KEY` is configured:

1. **Initialization & Logging:**
   - Validate `folderID` and `api_key`.
   - Mask the API key in all console output and request representations (`api_key[:4]...api_key[-4:]`).
   - Log execution start using `log_api_scrape_start(folder_id, max_depth=..., workers=..., api_key=...)`.
2. **Session Pooling & Optimization:**
   - Use `requests.Session` with `HTTPAdapter(pool_connections=1, pool_maxsize=workers)`.
   - Enable gzip compression (`Accept-Encoding: gzip`, `User-Agent: DriveFolderScraper (gzip)`).
3. **Querying & Pagination:**
   - Target endpoint: `https://www.googleapis.com/drive/v3/files`.
   - Query: `'<parent_id>' in parents and trashed = false`.
   - Query each parent folder individually (do not combine multiple parents using `OR`, as this triggers HTTP 403 `insufficientFilePermissions` with API keys on link-shared folders).
   - Parameters: `pageSize=1000`, `fields="nextPageToken,files(id,name,mimeType,shortcutDetails(targetId,targetMimeType))"`, `supportsAllDrives=true`, `includeItemsFromAllDrives=true`.
   - Exhaustively paginate using `pageToken`.
4. **Resilience & Rate Limiting:**
   - Call `RateLimiter.acquire()` before each request.
   - Retryable HTTP statuses: `408`, `429`, `500`, `502`, `503`, `504`, and `403` with reasons `rateLimitExceeded` or `userRateLimitExceeded`.
   - Exponential backoff with jitter on retryable failures: `min(2**attempt, 16) + random.uniform(0, 0.5)`.
   - Non-retryable errors (e.g. 400, 401, 404, standard 403) are logged and the folder is skipped.
5. **Level-Order BFS Traversal:**
   - Concurrently fetch child folders at the same depth level using `ThreadPoolExecutor(max_workers=MAX_CONCURENT_FOLDER_EXPLORER)`.
   - Use `executor.map()` to preserve deterministic order across folder branches.
   - Increment depth and continue until queue is empty or `FOLDER_MAX_DEPTH` is reached.
6. **Classification & Shortcut Resolution:**
   - Map MIME types using `MIME_TYPE_TO_DOCUMENT_TYPE`.
   - Resolve shortcuts (`application/vnd.google-apps.shortcut`) via `shortcutDetails`:
     - Inspect `targetId` and `targetMimeType`.
     - Fall back to right-to-left extension parsing via `classify_by_filename()`.
   - Unsupported files generate an orange warning with `Name` and `MimeType`.

### Engine 2: Playwright Browser Backend

Used when the Drive API is disabled or API credentials are not provided.

#### A. Asynchronous URL Crawl (`crawl` & `_crawl_with_browser`)

1. **Browser & Shared Context Management:**
   - Launch Chromium via `launch_chromium(headless=settings.headless)`.
   - Launch flags: `--disable-blink-features=AutomationControlled`, `--disable-dev-shm-usage`, `--no-sandbox`.
   - Wrap in shared `BrowserContext` (`_open_shared_context`) with `service_workers="block"`.
   - Route interception (`_intercept_non_essential_resources`):
     - Abort blocked resource types: `image`, `media`, `font`, `websocket`, `manifest`.
     - Abort analytics/tracking URL patterns: `play.google.com/log`, `google-analytics.com`, `googletagmanager.com`, `doubleclick.net`, `clients6.google.com/batch`, `og/_/js/`.
2. **Concurrent Tab Traversal:**
   - Controlled by `asyncio.Semaphore(settings.max_concurrency)`.
   - At most `MAX_CONCURENT_FOLDER_NODE_VISIT` tabs active concurrently.
   - Short-lived tab lifecycle:
     - Open tab (`context.new_page()` or `browser.new_page()`).
     - Navigate: `page.goto(folder_url, wait_until="commit", timeout=timeout_ms)`.
     - Response validation: If HTTP status >= 400, raise `_InaccessibleFolderError` to immediately log error and skip folder without retrying.
     - Wait for items or empty state selector:
       `[data-target="doc"], [aria-label*="empty" i], div:has-text("Drop files here")`
     - Extract DOM elements via `get_file_elements(page)`.
     - In `finally` block, close tab quietly via `_close_quietly(page)`.
   - On timeout or failure: log warning/error, sleep jittered backoff (`_retry_backoff_seconds`), and retry in a fresh tab up to `SCRAPE_RETRY_COUNT`.
   - If crawl is cancelled or terminates, cancel and gather all in-flight tasks cleanly.

#### B. Synchronous Page Crawl (`scrape` with existing `Page`)

1. Work directly within the caller's synchronous `Page`.
2. Do not open or close browser resources.
3. Sequential traversal:
   - Mark initial folder URL as visited to prevent circular visits.
   - Wait for folder selector with `_wait_for_folder_items_sync(page, timeout_ms)`, catching timeouts with an orange warning.
   - Extract elements from initial page at depth `0`.
   - While `queue` is non-empty:
     - Pop `(folder_url, depth)`.
     - Navigate `page.goto(folder_url, wait_until="commit", timeout=timeout_ms)` with retry attempts and `_retry_backoff_seconds` sleep.
     - Check HTTP status >= 400; if failed, log and skip.
     - Wait for items selector and extract/queue child folders / record files.

---

## Extracting Drive Elements (DOM)

Execute JavaScript in the page context to serialize Drive item rows:
- Element selector: `document.querySelectorAll('[data-target=doc]')`
- **File Icon Container Scoping:**
  To prevent action toolbar icons or location column folder icons from conflating with format icons, isolate the file icon container element specifically:
  ```javascript
  const iconEl = el.querySelector('[data-file-icon]')
      || el.querySelector('[data-file-icon-bg]')
      || el.querySelector('td[data-column-field="6"]')
      || el.querySelector('td:first-child')
      || el;
  ```
- Extract and return serialized item properties:
  - `data_id`: from element attribute `data-id`
  - `tabindex`: from element attribute `tabindex`
  - `paths`: array of SVG `<path>` `d` strings (filtered non-empty)
  - `g_elements`: array of `<g>` `outerHTML`
  - `svg_inners`: array of `<svg>` `innerHTML`
  - `svg_outers`: array of `<svg>` `outerHTML`
  - `raw_html`: element `outerHTML`
- Convert the raw JS dictionaries into `DriveElement` dataclass instances.
- Provide both `async` (`get_file_elements`) and `sync` (`_get_file_elements_sync`) functions.

---

## Element Classification & Document Formats

Delegate classification to [`classifiers.classify_element()`](./classifiers/):
The classification system supports the following document types (`DocumentType` enum):
1. `PDF`
2. `GOOGLE_DOC`
3. `MICROSOFT_WORD`
4. `IMAGE` (PNG, JPG, JPEG, WEBP, GIF)
5. `XLSX`
6. `PPTX`
7. `ZIP_FOLDER`
8. `FOLDER`
9. `SHORTCUT`
10. `UNSUPPORTED_FORMAT`

Classification evaluation order:
1. `FOLDER` (folder SVG vector matches config)
2. `PDF` (PDF icon vector matches config)
3. `MICROSOFT_WORD` (Word icon vector matches config)
4. `IMAGE` (PNG icon vector matches config)
5. `GOOGLE_DOC` (Google Docs SVG element matches config)
6. `XLSX` (XLSX SVG element matches config)
7. `PPTX` (PowerPoint SVG element matches config)
8. `ZIP_FOLDER` (ZIP folder SVG element matches config)
9. Fallback to `UNSUPPORTED_FORMAT`

---

## URL & Link Generation

URL and `ClassifiedLink` generation are delegated to `classifiers`:

| DocumentType | URL Format |
|---|---|
| `PDF`, `GOOGLE_DOC`, `MICROSOFT_WORD`, `IMAGE`, `PPTX`, `ZIP_FOLDER` | `https://drive.google.com/file/d/{file_id}/view` |
| `XLSX` | `https://docs.google.com/spreadsheets/d/{file_id}/view` |
| `FOLDER` | `https://drive.google.com/drive/folders/{file_id}` (traversal only) |
| `UNSUPPORTED_FORMAT` | No URL generated |

- `scrape()` returns `list[str]` containing only file URLs.
- `scrape_classified()` returns `list[ClassifiedLink]` with `website_type=WebsiteType.GOOGLE_DRIVE` and `document_type=DocumentType.*`.
- Folder URLs are strictly used for internal queueing and traversal; they are never returned in public outputs.

---

## Deduplication & Depth Traversal

- **File Deduplication:** Keyed on `file_id` via `seen_file_ids`. Subsequent duplicates are ignored.
- **Folder Deduplication:** Keyed on `folder_id` via `visited_folder_ids`.
- **First-Discovery Preservation:** Discovered items are stored in append order in `discovered_file_urls` and `discovered_file_links`.
- **Queue Traversal:** Breadth-first search using `collections.deque`.
- **Depth Limiting:**
  - Initial root folder has depth `0`.
  - Direct children have depth `1`.
  - Child folders with `depth > config.FOLDER_MAX_DEPTH` are not enqueued.
  - Files discovered in folders processed before the limit are retained.

---

## Logging & Error Handling

- **Terminal Output:** Use `rich.console.Console` and `typer` directly.
- **Color Coding:**
  - Red (`[bold red]`): Unrecoverable errors, exhausted retries, HTTP >= 400 responses.
  - Orange (`[bold orange1]`): Warnings, timeouts, rate-limit pauses, unsupported formats.
  - Cyan (`[bold cyan]`): Informational API scraper start messages.
- **Unsupported Element Warning Format:**
  - DOM Scraper:
    ```text
    Unsupported URL: https://drive.google.com/open?id={file_id}&authuser=0 | Tab Index: {tabindex}
    ```
  - API Scraper:
    ```text
    Unsupported URL: https://drive.google.com/open?id={target_id} | Name: {name!r} | MimeType: {mime_type}
    ```
- **Inaccessible Folders:**
  - HTTP >= 400 status codes trigger immediate failure logging and folder skipping without redundant retries.
  - Failures in individual folders must never terminate the crawl of remaining queued folders.
- **Timeouts & Transient Errors:**
  - Timeouts during navigation or element loading are logged as warnings and retried up to `SCRAPE_RETRY_COUNT` with jittered exponential backoff.
  - In Playwright mode, each retry is performed in a clean, newly-created tab.
- **Credential Protection:**
  - Google API keys must always be masked (`***` or `f"{api_key[:4]}...{api_key[-4:]}"`) in URLs, headers, and logs.

---

## Acceptance Criteria

1. **Public Interface:** Expose synchronous functions `scrape(source)` returning `list[str]` and `scrape_classified(source)` returning `list[ClassifiedLink]`.
2. **Polymorphic Source:** Support both Google Drive folder URL (`str`) and caller-supplied Playwright `Page` (`sync_api.Page`).
3. **Execution Engines:**
   - Automatically route to Google Drive v3 REST API when `USE_GOOGLE_DRIVE_API` is True and `GOOGLE_API_KEY` is present.
   - Fall back to Playwright Chromium crawl when API is disabled or key is absent.
4. **Drive API Capabilities:**
   - Implement rate-limited sliding window (`RateLimiter`).
   - Use pooled keep-alive session with gzip compression.
   - Query each folder individually with `pageSize=1000` and pagination.
   - Explore folders concurrently at each depth level using `ThreadPoolExecutor`.
   - Resolve shortcuts via `shortcutDetails` and filename extension parsing.
5. **Playwright URL Crawling:**
   - Launch exactly one Chromium process with anti-detection and performance flags.
   - Share a single `BrowserContext` with `service_workers="block"` to share TLS/HTTP-2 connections and cached assets.
   - Route-block non-essential web assets (images, media, fonts) and analytics trackers.
   - Use `wait_until="commit"` on navigation so response headers trigger the next stage without burning timeouts on heavy JS downloads.
   - Crawl subfolders concurrently up to `MAX_CONCURENT_FOLDER_NODE_VISIT` tabs using Playwright's async API and an `asyncio.Semaphore`.
   - Each folder is read in a short-lived tab and closed immediately after extraction.
   - Seamlessly bridge async crawl to synchronous caller via `_run_blocking()`.
6. **Playwright Sync Page Crawling:**
   - Crawl sequentially in-place using the caller's `Page`.
   - Do not open or close browser resources.
   - Gracefully handle slow initial folder item loads with warning logging.
7. **DOM Extraction & Classification:**
   - Evaluate `_FILE_ELEMENTS_JS` with icon container scoping to avoid false matches.
   - Support all document formats: `PDF`, `GOOGLE_DOC`, `MICROSOFT_WORD`, `IMAGE`, `XLSX`, `PPTX`, `ZIP_FOLDER`.
   - Log unsupported items in orange warning format.
8. **Ordering & Deduplication:**
   - Deduplicate files and folders by ID.
   - Strictly preserve first-discovery order in public results.
   - Exclude folder URLs and unsupported elements from public output.
9. **Depth & Inaccessibility Handling:**
   - Respect `FOLDER_MAX_DEPTH` boundary.
   - Detect HTTP >= 400 responses, log as error, and skip immediately without retries.
   - Retry timeouts and transient errors up to `SCRAPE_RETRY_COUNT` with jittered backoff.
   - Never abort overall scraping due to a single folder failure.
10. **Resource Cleanliness:** Guarantee zero browser or HTTP connection leaks upon normal completion or unhandled failure.
