"""Google Drive folder scraper module.

Google Drive folders are scraped recursively with a breadth-first search (BFS),
icon-based format classification and robust error handling:

* **Folder URL** -> concurrent crawl with Playwright's *async* API: exactly one
  Chromium process, one short-lived tab per folder, and at most
  ``config.MAX_CONCURENT_FOLDER_NODE_VISIT`` tabs open at any moment.
* **Existing (sync) Playwright Page** -> sequential crawl inside that page.
* **Drive v3 REST API** -> used instead of a browser when enabled in config.

The public API (:func:`scrape`, :func:`scrape_classified`) stays synchronous.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import random
import threading
import time
from collections.abc import AsyncGenerator, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from playwright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
    Route as AsyncRoute,
    async_playwright,
)
from playwright.sync_api import (
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from rich.console import Console
import typer

import config
import classifiers
from classifiers import ClassifiedLink, DocumentType, DriveElement
from tracker import record_batch_error

console = Console()

# ---------------------------------------------------------------------------
# Google Drive REST API (v3) settings
# ---------------------------------------------------------------------------
DRIVE_FILES_API_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_API_PAGE_SIZE = 1000  # Maximum allowed by files.list -> fewer round trips
DRIVE_API_LIST_FIELDS = (
    "nextPageToken,files(id,name,mimeType,shortcutDetails(targetId,targetMimeType))"
)
DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
_RETRYABLE_HTTP_STATUS = {408, 429, 500, 502, 503, 504}
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

MIME_TYPE_TO_DOCUMENT_TYPE: dict[str, DocumentType] = {
    DRIVE_FOLDER_MIME_TYPE: DocumentType.FOLDER,
    "application/pdf": DocumentType.PDF,
    "application/vnd.google-apps.document": DocumentType.GOOGLE_DOC,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocumentType.MICROSOFT_WORD,
    "application/msword": DocumentType.MICROSOFT_WORD,
    "application/vnd.google-apps.spreadsheet": DocumentType.XLSX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": DocumentType.XLSX,
    "application/vnd.ms-excel": DocumentType.XLSX,
    "application/x-zip-compressed": DocumentType.ZIP_FOLDER,
    "application/zip": DocumentType.ZIP_FOLDER,
    "application/x-compressed": DocumentType.ZIP_FOLDER,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": DocumentType.PPTX,
    "application/vnd.ms-powerpoint": DocumentType.PPTX,
    "application/vnd.google-apps.presentation": DocumentType.PPTX,
    DRIVE_SHORTCUT_MIME_TYPE: DocumentType.SHORTCUT,
}

EXTENSION_TO_DOCUMENT_TYPE: dict[str, DocumentType] = {
    "pdf": DocumentType.PDF,
    "docx": DocumentType.MICROSOFT_WORD,
    "doc": DocumentType.MICROSOFT_WORD,
    "pptx": DocumentType.PPTX,
    "ppt": DocumentType.PPTX,
    "xlsx": DocumentType.XLSX,
    "xls": DocumentType.XLSX,
    "zip": DocumentType.ZIP_FOLDER,
    "png": DocumentType.IMAGE,
    "jpg": DocumentType.IMAGE,
    "jpeg": DocumentType.IMAGE,
    "webp": DocumentType.IMAGE,
    "gif": DocumentType.IMAGE,
}


def classify_by_filename(name: str | None) -> DocumentType:
    """Fallback classifier that checks dot-separated segments of ``name`` from right to left."""
    if not name or "." not in name:
        return DocumentType.UNSUPPORTED_FORMAT
    parts = name.strip().split(".")[1:]
    for ext in reversed(parts):
        cleaned_ext = ext.strip().lower()
        doc_type = EXTENSION_TO_DOCUMENT_TYPE.get(cleaned_ext)
        if doc_type is not None:
            return doc_type
    return DocumentType.UNSUPPORTED_FORMAT


def classify_mime_type(
    mime_type: str | None,
    name: str | None = None,
    shortcut_details: dict[str, Any] | None = None,
) -> DocumentType:
    """Map a Google Drive API ``mimeType`` to a DocumentType.

    When ``mime_type`` is ``application/vnd.google-apps.shortcut``, resolves the
    type using ``shortcut_details['targetMimeType']`` first and falls back to
    scanning ``name`` from right to left for a supported file extension.
    """
    if not mime_type:
        return DocumentType.UNSUPPORTED_FORMAT
    document_type = MIME_TYPE_TO_DOCUMENT_TYPE.get(mime_type)
    if document_type == DocumentType.SHORTCUT:
        target_mime = (
            shortcut_details.get("targetMimeType")
            if isinstance(shortcut_details, dict)
            else None
        )
        if target_mime and target_mime != DRIVE_SHORTCUT_MIME_TYPE:
            resolved = classify_mime_type(target_mime)
            if resolved != DocumentType.UNSUPPORTED_FORMAT:
                return resolved
        return classify_by_filename(name)
    if document_type is not None:
        return document_type
    if mime_type.startswith("image/"):  # jpeg, png, webp, heic, ...
        return DocumentType.IMAGE
    return DocumentType.UNSUPPORTED_FORMAT


class RateLimiter:
    """Thread-safe sliding-window rate limiter.

    Allows at most ``max_requests`` calls to :meth:`acquire` within any window of
    ``period_seconds``; callers block until a slot frees up.
    """

    def __init__(self, max_requests: int, period_seconds: float):
        if max_requests <= 0 or period_seconds <= 0:
            raise ValueError("max_requests and period_seconds must be positive")
        self.max_requests = int(max_requests)
        self.period_seconds = float(period_seconds)
        self._timestamps: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request may be sent, then record it."""
        warned = False
        while True:
            with self._lock:
                now = time.monotonic()
                window_start = now - self.period_seconds
                while self._timestamps and self._timestamps[0] <= window_start:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                wait_seconds = self._timestamps[0] - window_start
            if not warned:
                log_warning(
                    f"Drive API rate limit reached ({self.max_requests} requests / "
                    f"{self.period_seconds:g}s). Waiting {wait_seconds:.2f}s..."
                )
                warned = True
            time.sleep(wait_seconds)


_drive_api_rate_limiter: RateLimiter | None = None
_drive_api_rate_limiter_lock = threading.Lock()


def get_drive_api_rate_limiter() -> RateLimiter:
    """Return the process-wide Drive API rate limiter (quota is shared per API key)."""
    global _drive_api_rate_limiter
    with _drive_api_rate_limiter_lock:
        if _drive_api_rate_limiter is None:
            _drive_api_rate_limiter = RateLimiter(
                getattr(config, "GOOGLE_DRIVE_API_RATE_LIMIT", 12000),
                getattr(config, "GOOGLE_DRIVE_API_RATE_PERIOD_SECONDS", 60),
            )
        return _drive_api_rate_limiter


def should_use_drive_api() -> bool:
    """True when config enables the Drive API and an API key is configured."""
    api_key = getattr(config, "GOOGLE_API_KEY", "") or ""
    return bool(getattr(config, "USE_GOOGLE_DRIVE_API", False)) and bool(api_key.strip())


def log_unsupported_element(file_id: str, tabindex: str | None) -> None:
    """Log unsupported element in orange warning format."""
    message = f"Unsupported URL: https://drive.google.com/open?id={file_id}&authuser=0 | Tab Index: {tabindex}"
    record_batch_error(message)
    console.print(f"[bold orange1]{message}[/bold orange1]")


def log_error(message: str) -> None:
    """Log error message in red."""
    record_batch_error(message)
    console.print(f"[bold red]{message}[/bold red]")


def log_warning(message: str) -> None:
    """Log warning message in orange."""
    console.print(f"[bold orange1]{message}[/bold orange1]")


# Browser-side snippet that serialises every Drive item row ([data-target=doc]).
_FILE_ELEMENTS_JS = """() => {
        const elements = document.querySelectorAll('[data-target=doc]');
        return Array.from(elements).map(el => {
            const dataId = el.getAttribute('data-id') || '';
            const tabindex = el.getAttribute('tabindex');

            // Locate the file icon container specifically so toolbar action icons and
            // location column folder icons are not conflated with the format icon.
            const iconEl = el.querySelector('[data-file-icon]')
                || el.querySelector('[data-file-icon-bg]')
                || el.querySelector('td[data-column-field="6"]')
                || el.querySelector('td:first-child')
                || el;

            const paths = Array.from(iconEl.querySelectorAll('path'))
                .map(p => p.getAttribute('d') || '')
                .filter(d => Boolean(d));

            const gElements = Array.from(iconEl.querySelectorAll('g'))
                .map(g => g.outerHTML);

            const svgInners = Array.from(iconEl.querySelectorAll('svg'))
                .map(s => s.innerHTML);

            const svgOuters = Array.from(iconEl.querySelectorAll('svg'))
                .map(s => s.outerHTML);

            return {
                data_id: dataId,
                tabindex: tabindex,
                paths: paths,
                g_elements: gElements,
                svg_inners: svgInners,
                svg_outers: svgOuters,
                raw_html: el.outerHTML
            };
        });
    }"""

# Present once a folder listing (or its empty state) has rendered.
_FOLDER_ITEMS_SELECTOR = (
    '[data-target="doc"], [aria-label*="empty" i], div:has-text("Drop files here")'
)

# Resource types and analytics domains unnecessary for Drive folder DOM extraction.
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "websocket", "manifest"})
_BLOCKED_URL_PATTERNS = (
    "play.google.com/log",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "clients6.google.com/ batch",
    "og/_/js/",
)
_CHROMIUM_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
)


def _to_drive_elements(raw_data: list[dict[str, Any]]) -> list[DriveElement]:
    """Convert the raw dictionaries returned by ``_FILE_ELEMENTS_JS`` into DriveElements."""
    return [
        DriveElement(
            data_id=item.get("data_id", ""),
            tabindex=item.get("tabindex"),
            paths=item.get("paths", []),
            g_elements=item.get("g_elements", []),
            svg_inners=item.get("svg_inners", []),
            svg_outers=item.get("svg_outers", []),
            raw_html=item.get("raw_html", ""),
        )
        for item in raw_data
    ]


async def get_file_elements(page: AsyncPage) -> list[DriveElement]:
    """Execute document.querySelectorAll('[data-target=doc]') in an async page and return DriveElements."""
    return _to_drive_elements(await page.evaluate(_FILE_ELEMENTS_JS))


def _get_file_elements_sync(page: Page) -> list[DriveElement]:
    """Synchronous twin of :func:`get_file_elements` for caller-supplied sync Pages."""
    return _to_drive_elements(page.evaluate(_FILE_ELEMENTS_JS))


def init_browser(headless: bool = True) -> tuple[Playwright, Browser, Page]:
    """Initialize Playwright, chromium browser, and page.

    Separated into its own function so it can be reused or replaced by callers.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page()
    return playwright, browser, page


def cleanup_browser(
    playwright: Playwright | None = None,
    browser: Browser | None = None,
    page: Page | None = None,
) -> None:
    """Clean up browser resources without raising exceptions."""
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


def validate_drive_folder_url(url: str) -> None:
    """Validate that the URL is a Google Drive folder URL containing 'folders' path segment."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Invalid Google Drive folder URL: {url!r}")

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Must be http or https: {url}"
        )

    if "drive.google.com" not in parsed.netloc.lower():
        raise ValueError(
            f"Invalid domain '{parsed.netloc}'. Must be drive.google.com: {url}"
        )

    segments = [s for s in parsed.path.split("/") if s]
    if "folders" not in segments:
        raise ValueError(f"URL path does not contain 'folders' segment: {url}")


def extract_folder_id(url: str) -> str | None:
    """Extract folder ID from a Google Drive folder URL if present."""
    try:
        parsed = urlparse(url)
        segments = [s for s in parsed.path.split("/") if s]
        if "folders" in segments:
            idx = segments.index("folders")
            if idx + 1 < len(segments):
                return segments[idx + 1]
    except Exception:
        pass
    return None


async def _wait_for_folder_items(page: AsyncPage, timeout_ms: int) -> None:
    """Wait for drive folder items or empty state to appear.

    Raises ``PlaywrightTimeoutError`` if items do not render within ``timeout_ms``
    so the caller's retry policy can re-attempt the load instead of extracting an
    unrendered (empty) DOM.
    """
    await page.wait_for_selector(
        _FOLDER_ITEMS_SELECTOR, state="attached", timeout=timeout_ms
    )


def _wait_for_folder_items_sync(page: Page, timeout_ms: int) -> None:
    """Synchronous twin of :func:`_wait_for_folder_items` for caller-supplied sync Pages."""
    page.wait_for_selector(
        _FOLDER_ITEMS_SELECTOR, state="attached", timeout=timeout_ms
    )


def _log_failed_attempt(
    folder_url: str, attempt_info: str, error: Exception
) -> None:
    """Log a failed folder load attempt (timeouts as warnings, everything else as errors)."""
    if isinstance(error, PlaywrightTimeoutError):
        log_warning(f"Timeout accessing folder {folder_url} {attempt_info}: {error}")
    else:
        log_error(f"Error accessing folder {folder_url} {attempt_info}: {error}")


class _InaccessibleFolderError(Exception):
    """A folder answered with HTTP >= 400; retrying will not help."""

    def __init__(self, folder_url: str, status: int):
        super().__init__(f"Inaccessible folder (HTTP {status}): {folder_url}")


# ---------------------------------------------------------------------------
# Playwright crawl settings
# ---------------------------------------------------------------------------
def _config_int(name: str, default: int, minimum: int) -> int:
    """Read an integer setting from ``config`` (``default`` when unset), clamped to ``minimum``."""
    value = getattr(config, name, None)
    return default if value is None else max(minimum, int(value))


@dataclass(frozen=True)
class FolderScrapeSettings:
    """Immutable snapshot of the settings that drive a Playwright folder crawl."""

    timeout_ms: int = 15_000
    max_retries: int = 2
    max_depth: int = 5
    max_concurrency: int = 2
    headless: bool = True

    @classmethod
    def from_config(cls) -> FolderScrapeSettings:
        """Build settings from ``config.py`` (falling back to the defaults above)."""
        timeout_seconds = float(getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15))
        return cls(
            timeout_ms=_config_int("SCRAPE_TIMEOUT_SECONDS", default=5, minimum=2) * 1000,
            max_retries=_config_int("SCRAPE_RETRY_COUNT", default=2, minimum=0),
            max_depth=_config_int("FOLDER_MAX_DEPTH", default=5, minimum=0),
            max_concurrency=_config_int(
                "MAX_CONCURENT_FOLDER_NODE_VISIT", default=2, minimum=1
            ),
        )


# ---------------------------------------------------------------------------
# Folder reading port + async Playwright adapter
# ---------------------------------------------------------------------------
class FolderElementReader(Protocol):
    """Port: loads one Drive folder and returns its item elements.

    Implementations must return ``None`` (after logging) for folders that cannot
    be read, so that one failing folder never stops the rest of the crawl.
    """

    async def read(self, folder_url: str) -> list[DriveElement] | None: ...


async def _intercept_non_essential_resources(route: AsyncRoute) -> None:
    """Abort heavy non-DOM assets (images, fonts, media, telemetry) to speed up Drive loads."""
    request = route.request
    if request.resource_type in _BLOCKED_RESOURCE_TYPES or any(
        pattern in request.url for pattern in _BLOCKED_URL_PATTERNS
    ):
        try:
            await route.abort()
        except Exception:
            pass
    else:
        try:
            await route.continue_()
        except Exception:
            pass


def _retry_backoff_seconds(attempt: int) -> float:
    """Compute jittered exponential backoff before retry ``attempt`` (1-indexed)."""
    return min(2 ** (attempt - 1), 8) + random.uniform(0.2, 0.7)


class PlaywrightFolderElementReader:
    """Adapter: reads every folder in a short-lived tab of one shared async browser.

    Uses a shared ``BrowserContext`` (when available) so all worker tabs reuse HTTP/2+QUIC
    connections, TLS sessions, and cached Google Drive JS bundles instead of cold-booting
    an isolated incognito context per folder. Heavy non-essential assets (thumbnails,
    fonts, media, analytics) are aborted via route interception.

    Each tab is closed in a ``try...finally`` block immediately after its items are
    extracted to keep memory minimal.
    """

    def __init__(
        self,
        browser: AsyncBrowser,
        settings: FolderScrapeSettings,
        context: AsyncBrowserContext | None = None,
    ):
        self._browser = browser
        self._context = context
        self._timeout_ms = settings.timeout_ms
        self._attempts = settings.max_retries + 1

    async def read(self, folder_url: str) -> list[DriveElement] | None:
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._read_in_new_tab(folder_url)
            except _InaccessibleFolderError as e:
                log_error(f"{e}. Skipping.")
                return None
            except Exception as e:  # CancelledError is not an Exception -> propagates
                _log_failed_attempt(folder_url, f"(attempt {attempt}/{self._attempts})", e)
                if attempt < self._attempts:
                    await asyncio.sleep(_retry_backoff_seconds(attempt))
        log_error(f"Retries exhausted for folder {folder_url}. Skipping folder.")
        return None

    async def _open_tab(self) -> AsyncPage:
        if self._context is not None:
            return await self._context.new_page()
        page = await self._browser.new_page()
        try:
            await page.route("**/*", _intercept_non_essential_resources)
        except Exception:
            pass
        return page

    async def _read_in_new_tab(self, folder_url: str) -> list[DriveElement]:
        page = await self._open_tab()
        try:
            return await self._read_page(page, folder_url)
        finally:
            await _close_quietly(page)

    async def _read_page(self, page: AsyncPage, folder_url: str) -> list[DriveElement]:
        # Use wait_until="commit" so page.goto returns as soon as HTTP headers arrive
        # and does not burn the full timeout waiting on Drive's heavy initial JS scripts;
        # _wait_for_folder_items then waits up to timeout_ms for the actual folder items.
        response = await page.goto(
            folder_url, wait_until="commit", timeout=self._timeout_ms
        )
        if response is not None and response.status >= 400:
            raise _InaccessibleFolderError(folder_url, response.status)
        await _wait_for_folder_items(page, self._timeout_ms)
        return await get_file_elements(page)


async def _close_quietly(
    resource: AsyncPage | AsyncBrowserContext | AsyncBrowser,
) -> None:
    """Close an async Playwright page/context/browser, ignoring errors (e.g. already closed)."""
    try:
        await resource.close()
    except Exception:
        pass


@asynccontextmanager
async def launch_chromium(headless: bool = True) -> AsyncGenerator[AsyncBrowser, None]:
    """Launch exactly one Chromium process; it is closed even if the crawl crashes."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=headless,
            args=list(_CHROMIUM_ARGS),
        )
        try:
            yield browser
        finally:
            await _close_quietly(browser)


@asynccontextmanager
async def _open_shared_context(
    browser: AsyncBrowser,
) -> AsyncGenerator[AsyncBrowserContext | None, None]:
    """Create a single shared BrowserContext with resource blocking enabled.

    Falls back to ``None`` if ``browser`` is a test double without ``new_context``.
    """
    new_context = getattr(browser, "new_context", None)
    if new_context is None:
        yield None
        return
    context = await new_context(service_workers="block")
    try:
        await context.route("**/*", _intercept_non_essential_resources)
        yield context
    finally:
        await _close_quietly(context)


_T = TypeVar("_T")


def _run_blocking(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run ``coroutine`` to completion from synchronous code and return its result.

    ``asyncio.run`` cannot be nested, so when the current thread already runs an
    event loop (Jupyter, an active sync Playwright session, ...) the coroutine is
    run on a short-lived helper thread instead; the caller still just blocks.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive-crawl") as executor:
        return executor.submit(asyncio.run, coroutine).result()


class DriveFolderScraper:
    """Scrapes Google Drive folders recursively using breadth-first search (BFS).

    Owns the discovery state (files, visited folders, BFS queue) and the
    traversal policy (depth limit, de-duplication). How a folder is *loaded* is
    delegated to a :class:`FolderElementReader` (concurrent URL crawl via
    :meth:`crawl`) or to a caller-supplied sync ``Page`` (:meth:`scrape`).

    Implements a thread-safe Singleton pattern so that a single instance is reused
    across the application lifecycle.
    """

    _instance: DriveFolderScraper | None = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> DriveFolderScraper:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    @classmethod
    def get_instance(cls, page: Page | None = None) -> DriveFolderScraper:
        """Return the singleton instance of DriveFolderScraper."""
        return cls(page)

    @classmethod
    def _reset_instance(cls) -> None:
        """Reset the singleton instance (primarily for testing or cleanup)."""
        with cls._lock:
            cls._instance = None

    def __init__(self, page: Page | None = None):
        # `page` is only required for scraping an existing sync Playwright Page;
        # the URL crawl (`crawl`) and the API scraping (`_scrape_by_api`) create
        # the scraper without it.
        if page is not None:
            self.page = page
        elif not hasattr(self, "page"):
            self.page = None

        if getattr(self, "_initialized", False):
            return

        self._initialized = True
        self.settings = FolderScrapeSettings.from_config()
        self.discovered_file_urls: list[str] = []
        self.discovered_file_links: list[ClassifiedLink] = []
        self.seen_file_ids: set[str] = set()
        self.visited_folder_ids: set[str] = set()
        self.queue: collections.deque[tuple[str, int]] = collections.deque()

    def reset(self, page: Page | None = None) -> None:
        """Reset internal discovery state and queue for a fresh scrape session."""
        if page is not None:
            self.page = page
        self.discovered_file_urls.clear()
        self.discovered_file_links.clear()
        self.seen_file_ids.clear()
        self.visited_folder_ids.clear()
        self.queue.clear()

    def _start_session(self, settings: FolderScrapeSettings | None) -> None:
        """Clear all discovery state and pin the settings used by this scrape."""
        self.reset()
        self.settings = settings or FolderScrapeSettings.from_config()

    # ------------------------------------------------------------------
    # Discovery bookkeeping. These methods never await, so under asyncio each
    # call runs atomically with respect to the other concurrent folder tasks.
    # ------------------------------------------------------------------
    def _record_file(self, label: DocumentType, file_id: str) -> None:
        """Record a discovered file once (by file ID), preserving first-discovery order."""
        if not file_id or file_id in self.seen_file_ids:
            return
        classified_link = classifiers.generate_classified_url(label, file_id)
        if classified_link:
            self.seen_file_ids.add(file_id)
            self.discovered_file_links.append(classified_link)
            self.discovered_file_urls.append(classified_link.url)

    def _enqueue_folder(self, folder_id: str, depth: int) -> None:
        """Mark a folder visited and queue it, unless already seen or deeper than FOLDER_MAX_DEPTH."""
        if not folder_id or folder_id in self.visited_folder_ids:
            return
        if depth > self.settings.max_depth:
            return
        folder_url = classifiers.generate_url(DocumentType.FOLDER, folder_id)
        if folder_url:
            self.visited_folder_ids.add(folder_id)
            self.queue.append((folder_url, depth))

    def _ingest_elements(self, elements: list[DriveElement], depth: int) -> None:
        """Classify the items of a folder at ``depth``: record files, queue child folders."""
        for element in elements:
            label = classifiers.classify_element(element)

            if label == DocumentType.UNSUPPORTED_FORMAT:
                log_unsupported_element(element.data_id, element.tabindex)
            elif label == DocumentType.FOLDER:
                self._enqueue_folder(element.data_id, depth + 1)
            else:  # pdf, google_doc, microsoft_word, image, xlsx, pptx, zip_folder
                self._record_file(label, element.data_id)

    def _record_api_items(
        self, items: list[dict[str, Any]], current_depth: int
    ) -> list[str]:
        """Classify Drive API file resources, record files and return child folder IDs to explore next."""
        max_depth = getattr(config, "FOLDER_MAX_DEPTH", 5)
        next_folder_ids: list[str] = []

        for item in items:
            item_id = item.get("id") or ""
            if not item_id:
                continue
            shortcut_details = item.get("shortcutDetails")
            target_id = (
                (shortcut_details.get("targetId") if isinstance(shortcut_details, dict) else None)
                or item_id
            )
            label = classify_mime_type(
                item.get("mimeType"),
                name=item.get("name"),
                shortcut_details=shortcut_details,
            )

            if label == DocumentType.UNSUPPORTED_FORMAT:
                unsupported_msg = (
                    f"Unsupported URL: https://drive.google.com/open?id={target_id} | "
                    f"Name: {item.get('name')!r} | MimeType: {item.get('mimeType')}"
                )
                record_batch_error(unsupported_msg)
                log_warning(unsupported_msg)
                continue

            if label == DocumentType.FOLDER:
                if target_id not in self.visited_folder_ids:
                    self.visited_folder_ids.add(target_id)
                    if current_depth + 1 <= max_depth:
                        next_folder_ids.append(target_id)
                continue

            self._record_file(label, target_id)

        return next_folder_ids

    # ------------------------------------------------------------------
    # Concurrent crawl of a folder URL (async, one tab per folder)
    # ------------------------------------------------------------------
    async def crawl(
        self,
        root_url: str,
        reader: FolderElementReader,
        settings: FolderScrapeSettings | None = None,
    ) -> list[str]:
        """Crawl ``root_url`` (depth 0) breadth-first, reading up to
        ``settings.max_concurrency`` folders at the same time through ``reader``.

        Returns the discovered file URLs in first-discovery order.
        """
        self._start_session(settings)
        root_id = extract_folder_id(root_url)
        if root_id:
            self.visited_folder_ids.add(root_id)
        self.queue.append((root_url, 0))
        await self._drain_queue_concurrently(reader)
        return self.discovered_file_urls

    def seed_root_folders(
        self,
        folder_urls: Sequence[str],
        settings: FolderScrapeSettings | None = None,
    ) -> list[str]:
        """Reset session state and seed all valid root folder URLs into ``self.queue`` at depth 0."""
        self._start_session(settings)
        seeded_ids: list[str] = []
        seen_urls: set[str] = set()

        for raw_url in folder_urls:
            normalized = raw_url.strip() if isinstance(raw_url, str) else ""
            try:
                validate_drive_folder_url(normalized)
            except ValueError as exc:
                log_error(f"{exc}. Skipping.")
                continue

            root_id = extract_folder_id(normalized)
            if root_id:
                if root_id in self.visited_folder_ids:
                    continue
                self.visited_folder_ids.add(root_id)
                seeded_ids.append(root_id)
            elif normalized in seen_urls:
                continue

            seen_urls.add(normalized)
            self.queue.append((normalized, 0))

        return seeded_ids

    async def crawl_many(
        self,
        folder_urls: Sequence[str],
        reader: FolderElementReader,
        settings: FolderScrapeSettings | None = None,
    ) -> list[ClassifiedLink]:
        """Seed multiple root folder URLs at depth 0 and drain ``self.queue`` concurrently."""
        self.seed_root_folders(folder_urls, settings)
        if self.queue:
            await self._drain_queue_concurrently(reader)
        return list(self.discovered_file_links)

    async def _drain_queue_concurrently(self, reader: FolderElementReader) -> None:
        """Dispatch queued folders as tasks while a concurrency slot is free, until
        the queue is empty and no folder is in flight."""
        slots = asyncio.Semaphore(self.settings.max_concurrency)
        in_flight: set[asyncio.Task[None]] = set()
        try:
            while self.queue or in_flight:
                # Never blocks: a slot is free whenever the semaphore is not locked.
                while self.queue and not slots.locked():
                    await slots.acquire()
                    folder_url, depth = self.queue.popleft()
                    task = asyncio.create_task(self._visit_folder(reader, folder_url, depth))
                    task.add_done_callback(lambda _task: slots.release())
                    in_flight.add(task)
                _, in_flight = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
        finally:
            # Only non-empty if the crawl itself was cancelled or crashed.
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)

    async def _visit_folder(
        self, reader: FolderElementReader, folder_url: str, depth: int
    ) -> None:
        """Read one folder and ingest its items; failures are logged, never propagated."""
        try:
            elements = await reader.read(folder_url)
            if elements is not None:
                self._ingest_elements(elements, depth)
        except Exception as e:
            log_error(f"Unexpected error scraping folder {folder_url}: {e}. Skipping folder.")

    # ------------------------------------------------------------------
    # Sequential crawl inside a caller-supplied sync Playwright Page
    # ------------------------------------------------------------------
    def _extract_and_queue(self, current_depth: int) -> None:
        """Read and classify elements on the current page, queueing child folders and recording files."""
        self._ingest_elements(_get_file_elements_sync(self.page), current_depth)

    def _process_queued_folder(self, folder_url: str, depth: int) -> None:
        """Navigate the sync page to a child folder with retries and process its elements."""
        timeout_ms = self.settings.timeout_ms
        attempts = self.settings.max_retries + 1

        for attempt in range(1, attempts + 1):
            try:
                resp = self.page.goto(
                    folder_url, wait_until="commit", timeout=timeout_ms
                )
                if resp is not None and resp.status >= 400:
                    log_error(
                        f"Inaccessible child folder (HTTP {resp.status}): {folder_url}. Skipping."
                    )
                    return
                _wait_for_folder_items_sync(self.page, timeout_ms)
                self._extract_and_queue(current_depth=depth)
                return
            except Exception as e:
                _log_failed_attempt(folder_url, f"(attempt {attempt}/{attempts})", e)
                if attempt < attempts:
                    time.sleep(_retry_backoff_seconds(attempt))
        log_error(f"Retries exhausted for folder {folder_url}. Skipping folder.")

    def scrape(self, settings: FolderScrapeSettings | None = None) -> list[str]:
        """Perform breadth-first traversal starting from the current sync page state."""
        if self.page is None:
            raise RuntimeError(
                "DriveFolderScraper.scrape() needs a Playwright Page; "
                "use the module-level scrape(url) or crawl() for folder URLs."
            )
        self._start_session(settings)

        # Mark initial folder as visited to prevent re-traversal
        init_id = extract_folder_id(self.page.url)
        if init_id:
            self.visited_folder_ids.add(init_id)

        try:
            _wait_for_folder_items_sync(self.page, self.settings.timeout_ms)
        except Exception as e:
            log_warning(f"Timed out waiting for initial folder items on {self.page.url}: {e}")
        self._extract_and_queue(current_depth=0)

        # BFS queue loop
        while self.queue:
            folder_url, depth = self.queue.popleft()
            self._process_queued_folder(folder_url, depth=depth)

        return self.discovered_file_urls

    def scrape_classified(self) -> list[ClassifiedLink]:
        """Perform breadth-first traversal and return discovered ClassifiedLink objects."""
        self.scrape()
        return self.discovered_file_links


async def _crawl_with_browser(
    folder_url: str, settings: FolderScrapeSettings
) -> DriveFolderScraper:
    """Crawl ``folder_url`` concurrently using one Chromium process (closed afterwards)."""
    scraper = DriveFolderScraper.get_instance()
    async with launch_chromium(headless=settings.headless) as browser:
        async with _open_shared_context(browser) as context:
            reader = PlaywrightFolderElementReader(browser, settings, context=context)
            await scraper.crawl(folder_url, reader, settings)
    return scraper


def _scrape_source(source: str | Page) -> DriveFolderScraper:
    """Validate and scrape a Google Drive folder from a URL or Playwright Page.

    For URL sources, the Drive API (`_scrape_by_api`) is used when
    ``config.USE_GOOGLE_DRIVE_API`` is true and ``config.GOOGLE_API_KEY`` is set;
    otherwise the folder tree is crawled concurrently with async Playwright
    (blocking until done). A sync ``Page`` is crawled sequentially in place.
    """
    if isinstance(source, str):
        validate_drive_folder_url(source)
        if should_use_drive_api():
            folder_id = extract_folder_id(source.strip())
            if not folder_id:
                raise ValueError(f"Could not extract folder ID from URL: {source}")
            return _scrape_by_api(folder_id, config.GOOGLE_API_KEY)
        settings = FolderScrapeSettings.from_config()
        return _run_blocking(_crawl_with_browser(source.strip(), settings))
    elif isinstance(source, Page):
        scraper = DriveFolderScraper(source)
        scraper.scrape()
        return scraper
    else:
        raise TypeError(
            f"Expected source to be str or Page, got {type(source).__name__}"
        )

def _api_error_details(resp: requests.Response) -> tuple[str, set[str]]:
    """Extract the error message and error reasons from a Drive API error response."""
    try:
        error = resp.json().get("error", {})
        reasons = {e.get("reason") for e in error.get("errors", []) if e.get("reason")}
        return error.get("message") or resp.reason, reasons
    except (ValueError, AttributeError):
        return (resp.text or resp.reason or "")[:200], set()


def _api_get_json(
    session: requests.Session, url: str, params: dict[str, Any], label: str
) -> dict[str, Any] | None:
    """GET a Drive API endpoint with retries (network errors, 429, 5xx, rate-limit 403).

    Returns the parsed JSON, or None when the request ultimately fails.
    """
    timeout_seconds = getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15)
    max_retries = getattr(config, "SCRAPE_RETRY_COUNT", 2)
    api_key = str(params.get("key", ""))

    def _mask(text: str) -> str:
        # Never leak the API key into logs (request URLs contain it).
        return text.replace(api_key, "***") if api_key else text

    for attempt in range(max_retries + 1):
        attempt_info = f"(attempt {attempt + 1}/{max_retries + 1})"
        try:
            get_drive_api_rate_limiter().acquire()  # every attempt counts toward quota
            resp = session.get(url, params=params, timeout=timeout_seconds)
        except requests.RequestException as e:
            log_warning(f"Network error listing {label} {attempt_info}: {_mask(str(e))}")
        else:
            if resp.ok:
                try:
                    return resp.json()
                except ValueError:
                    log_warning(f"Invalid JSON response listing {label} {attempt_info}")
            else:
                message, reasons = _api_error_details(resp)
                retryable = resp.status_code in _RETRYABLE_HTTP_STATUS or (
                    resp.status_code == 403 and bool(reasons & _RETRYABLE_403_REASONS)
                )
                if not retryable:
                    log_error(
                        f"Drive API error (HTTP {resp.status_code}) for {label}: {_mask(message)}. Skipping."
                    )
                    return None
                log_warning(
                    f"Drive API error (HTTP {resp.status_code}) for {label} {attempt_info}: {_mask(message)}"
                )

        if attempt < max_retries:
            # Exponential backoff with jitter, as recommended by Google APIs.
            time.sleep(min(2**attempt, 16) + random.uniform(0, 0.5))

    log_error(f"Retries exhausted for {label}. Skipping.")
    return None


def _api_list_children(
    session: requests.Session, parent_id: str, api_key: str
) -> list[dict[str, Any]]:
    """List all non-trashed direct children of a single folder (with pagination).

    Note: Each folder must be queried individually (`'<id>' in parents`).
    Combining multiple folders with `OR` causes the Drive v3 API to return
    HTTP 403 (`insufficientFilePermissions`) when using an API key on
    link-shared folders or when any single folder in the batch is restricted.
    """
    params: dict[str, Any] = {
        "q": f"'{parent_id}' in parents and trashed = false",
        "fields": DRIVE_API_LIST_FIELDS,
        "pageSize": DRIVE_API_PAGE_SIZE,
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "key": api_key,
    }
    label = f"folder {parent_id}"

    items: list[dict[str, Any]] = []
    while True:
        data = _api_get_json(session, DRIVE_FILES_API_URL, params, label)
        if data is None:
            break
        items.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token
    return items


def log_api_scrape_start(folder_id: str, **kwargs: Any) -> None:
    """Log when `_scrape_by_api` is invoked, including folder/file ID and configuration details."""
    details = [f"folder_id={folder_id}"]
    for k, v in kwargs.items():
        details.append(f"{k}={v}")
    console.print(f"[bold cyan][API Scraper][/bold cyan] Starting scrape: {', '.join(details)}")


def _scrape_by_api(folderID:str, api_key:str) -> DriveFolderScraper:
    """Recursively scrape a public Google Drive folder using the Drive v3 REST API.

    Traversal is a level-order BFS (same depth semantics as the Playwright
    scraper, bounded by ``config.FOLDER_MAX_DEPTH``). Performance optimizations:

    * All folders at the same depth level are listed concurrently using
      ``ThreadPoolExecutor`` (``config.MAX_CONCURENT_FOLDER_EXPLORER``).
    * ``pageSize=1000`` (API max), minimal ``fields``, gzip and a pooled
      keep-alive HTTP session.

    Args:
        folderID: The Google Drive folder ID to start from.
        api_key: A Google Cloud API key with the Drive API enabled.

    Returns:
        A DriveFolderScraper populated with ``discovered_file_links`` / ``discovered_file_urls``.
    """
    if not isinstance(folderID, str) or not folderID.strip():
        raise ValueError(f"Invalid Google Drive folder ID: {folderID!r}")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("A Google API key is required to scrape by API.")
    folderID, api_key = folderID.strip(), api_key.strip()

    workers = max(1, int(getattr(config, "MAX_CONCURENT_FOLDER_EXPLORER", 5)))
    max_depth = getattr(config, "FOLDER_MAX_DEPTH", 5)
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
    log_api_scrape_start(
        folderID,
        max_depth=max_depth,
        workers=workers,
        api_key=masked_key,
    )
    scraper = DriveFolderScraper()
    scraper.reset()

    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=workers))
    # Google APIs only return gzip-compressed bodies when the UA contains "gzip".
    session.headers.update(
        {"Accept-Encoding": "gzip", "User-Agent": "DriveFolderScraper (gzip)"}
    )

    try:
        scraper.visited_folder_ids.add(folderID)
        current_level = [folderID]
        depth = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            while current_level:
                next_level: list[str] = []
                # executor.map keeps folder order -> deterministic discovery order.
                for items in executor.map(
                    lambda pid: _api_list_children(session, pid, api_key),
                    current_level,
                ):
                    next_level.extend(scraper._record_api_items(items, depth))
                current_level = next_level
                depth += 1
    finally:
        session.close()

    return scraper

def scrape(source: str | Page) -> list[str]:
    """Scrapes a Google Drive folder and returns discovered file URLs.

    Supports both a Google Drive folder URL (str) and an existing Playwright Page.

    Args:
        source: A Google Drive folder URL string or a Playwright Page instance.

    Returns:
        A list of discovered file URLs in first-discovery order.
    """
    return _scrape_source(source).discovered_file_urls


def scrape_classified(source: str | Page) -> list[ClassifiedLink]:
    """Scrapes a Google Drive folder and returns discovered ClassifiedLink objects.

    Supports both a Google Drive folder URL (str) and an existing Playwright Page.

    Args:
        source: A Google Drive folder URL string or a Playwright Page instance.

    Returns:
        A list of discovered ClassifiedLink instances in first-discovery order.
    """
    return _scrape_source(source).discovered_file_links


def _scrape_many_by_api(
    folder_ids: Sequence[str],
    api_key: str,
    scraper: DriveFolderScraper | None = None,
) -> DriveFolderScraper:
    """Recursively scrape multiple root Google Drive folders using the Drive v3 REST API."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("A Google API key is required to scrape by API.")
    api_key = api_key.strip()

    active_scraper = scraper or DriveFolderScraper.get_instance()
    active_scraper.queue.clear()

    valid_ids = [fid.strip() for fid in folder_ids if isinstance(fid, str) and fid.strip()]
    if not valid_ids:
        return active_scraper

    workers = max(1, int(getattr(config, "MAX_CONCURENT_FOLDER_EXPLORER", 5)))
    max_depth = getattr(config, "FOLDER_MAX_DEPTH", 5)
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
    for fid in valid_ids:
        log_api_scrape_start(
            fid,
            max_depth=max_depth,
            workers=workers,
            api_key=masked_key,
        )
        active_scraper.visited_folder_ids.add(fid)

    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=workers))
    session.headers.update(
        {"Accept-Encoding": "gzip", "User-Agent": "DriveFolderScraper (gzip)"}
    )

    try:
        current_level = list(valid_ids)
        depth = 0
        parent_ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while current_level:
                next_level: list[str] = []
                for items in executor.map(
                    lambda pid: parent_ctx.copy().run(
                        _api_list_children, session, pid, api_key
                    ),
                    current_level,
                ):
                    next_level.extend(active_scraper._record_api_items(items, depth))
                current_level = next_level
                depth += 1
    finally:
        session.close()

    return active_scraper


async def scrape_many_classified(
    folder_urls: Sequence[str],
    browser: AsyncBrowser | None = None,
    settings: FolderScrapeSettings | None = None,
) -> list[ClassifiedLink]:
    """Seed all ``folder_urls`` into ``DriveFolderScraper.queue`` and drain concurrently.

    Reuses ``browser`` when supplied instead of launching a second Chromium instance.
    """
    if not folder_urls:
        return []

    scraper = DriveFolderScraper.get_instance()
    active_settings = settings or FolderScrapeSettings.from_config()
    root_ids = scraper.seed_root_folders(folder_urls, active_settings)
    if not scraper.queue:
        return []

    if should_use_drive_api():
        await asyncio.to_thread(
            _scrape_many_by_api, root_ids, config.GOOGLE_API_KEY, scraper
        )
        return list(scraper.discovered_file_links)

    if browser is not None:
        async with _open_shared_context(browser) as context:
            reader = PlaywrightFolderElementReader(
                browser, active_settings, context=context
            )
            await scraper._drain_queue_concurrently(reader)
    else:
        async with launch_chromium(headless=active_settings.headless) as owned_browser:
            async with _open_shared_context(owned_browser) as context:
                reader = PlaywrightFolderElementReader(
                    owned_browser, active_settings, context=context
                )
                await scraper._drain_queue_concurrently(reader)

    return list(scraper.discovered_file_links)


if __name__ == "__main__":
    # config.USE_GOOGLE_DRIVE_API="true"
    with console.status("[bold white]Exploring folder[/bold white]", spinner="dots"):
        l = scrape_classified(
            "https://drive.google.com/drive/folders/{ID}"
        )
    for e in l:
        print(e)
    print("Total: " + str(len(l)))
