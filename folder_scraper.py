"""Google Drive folder scraper module.

This module scrapes Google Drive folders recursively using Playwright synchronous API,
breadth-first search (BFS) traversal, icon-based format classification, and robust
error handling.
"""

from __future__ import annotations

import collections
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
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
    console.print(f"[bold orange1]{message}[/bold orange1]")


def log_error(message: str) -> None:
    """Log error message in red."""
    console.print(f"[bold red]{message}[/bold red]")


def log_warning(message: str) -> None:
    """Log warning message in orange."""
    console.print(f"[bold orange1]{message}[/bold orange1]")


def get_file_elements(page: Page) -> list[DriveElement]:
    """Execute document.querySelectorAll('[data-target=doc]') in browser page and return converted DriveElements."""
    raw_data = page.evaluate("""() => {
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
    }""")
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


def _wait_for_folder_items(page: Page, timeout_ms: int) -> None:
    """Wait for drive folder items or empty state to appear."""
    try:
        page.wait_for_selector(
            '[data-target="doc"], [aria-label*="empty" i], div:has-text("Drop files here")',
            timeout=timeout_ms,
        )
    except Exception:
        pass


def _navigate_folder(page: Page, url: str, timeout_ms: int, max_retries: int) -> bool:
    """Navigate to a folder URL with retries on timeout/error."""
    attempt = 0
    while attempt <= max_retries:
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if resp is not None and resp.status >= 400:
                log_error(f"Inaccessible folder (HTTP {resp.status}): {url}")
                return False
            _wait_for_folder_items(page, timeout_ms)
            return True
        except PlaywrightTimeoutError as e:
            log_warning(
                f"Timeout navigating to {url} (attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            attempt += 1
            if attempt > max_retries:
                log_error(f"Retries exhausted navigating to {url}. Skipping.")
                return False
        except Exception as e:
            log_error(
                f"Error navigating to {url} (attempt {attempt + 1}/{max_retries + 1}): {e}"
            )
            attempt += 1
            if attempt > max_retries:
                log_error(f"Retries exhausted navigating to {url}. Skipping.")
                return False
    return False


class DriveFolderScraper:
    """Scrapes Google Drive folders recursively using breadth-first search (BFS).

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
        # `page` is only required for the Playwright-based scraping; the
        # API-based scraping (`_scrape_by_api`) creates the scraper without it.
        if page is not None:
            self.page = page
        elif not hasattr(self, "page"):
            self.page = None

        if getattr(self, "_initialized", False):
            return

        self._initialized = True
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
                log_warning(
                    f"Unsupported URL: https://drive.google.com/open?id={target_id} | "
                    f"Name: {item.get('name')!r} | MimeType: {item.get('mimeType')}"
                )
                continue

            if label == DocumentType.FOLDER:
                if target_id not in self.visited_folder_ids:
                    self.visited_folder_ids.add(target_id)
                    if current_depth + 1 <= max_depth:
                        next_folder_ids.append(target_id)
                continue

            if target_id not in self.seen_file_ids:
                classified_link = classifiers.generate_classified_url(label, target_id)
                if classified_link:
                    self.seen_file_ids.add(target_id)
                    self.discovered_file_links.append(classified_link)
                    self.discovered_file_urls.append(classified_link.url)

        return next_folder_ids

    def _extract_and_queue(self, current_depth: int) -> None:
        """Read and classify elements on the current page, queueing child folders and recording files."""
        max_depth = getattr(config, "FOLDER_MAX_DEPTH", 5)
        elements = get_file_elements(self.page)

        for element in elements:
            label = classifiers.classify_element(element)

            if label == DocumentType.UNSUPPORTED_FORMAT:
                log_unsupported_element(element.data_id, element.tabindex)
                continue

            if label == DocumentType.FOLDER:
                folder_id = element.data_id
                if folder_id and folder_id not in self.visited_folder_ids:
                    self.visited_folder_ids.add(folder_id)
                    if current_depth + 1 <= max_depth:
                        folder_url = classifiers.generate_url(
                            DocumentType.FOLDER, folder_id
                        )
                        if folder_url:
                            self.queue.append((folder_url, current_depth + 1))
                continue

            # File format (pdf, google_doc, microsoft_word, image, xlsx, pptx, zip_folder)
            file_id = element.data_id
            if file_id and file_id not in self.seen_file_ids:
                classified_link = classifiers.generate_classified_url(label, file_id)
                if classified_link:
                    self.seen_file_ids.add(file_id)
                    self.discovered_file_links.append(classified_link)
                    self.discovered_file_urls.append(classified_link.url)

    def _process_queued_folder(self, folder_url: str, depth: int) -> None:
        """Navigate to a child folder with retries and process its elements."""
        timeout_seconds = getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15)
        timeout_ms = int(timeout_seconds * 1000)
        max_retries = getattr(config, "SCRAPE_RETRY_COUNT", 2)

        attempt = 0
        while attempt <= max_retries:
            try:
                resp = self.page.goto(
                    folder_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if resp is not None and resp.status >= 400:
                    log_error(
                        f"Inaccessible child folder (HTTP {resp.status}): {folder_url}. Skipping."
                    )
                    return

                _wait_for_folder_items(self.page, timeout_ms)
                self._extract_and_queue(current_depth=depth)
                return

            except PlaywrightTimeoutError as e:
                log_warning(
                    f"Timeout accessing folder {folder_url} (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                attempt += 1
                if attempt > max_retries:
                    log_error(
                        f"Retries exhausted for folder {folder_url}. Skipping folder."
                    )
                    return
            except Exception as e:
                log_error(
                    f"Error accessing folder {folder_url} (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                attempt += 1
                if attempt > max_retries:
                    log_error(
                        f"Retries exhausted for folder {folder_url}. Skipping folder."
                    )
                    return

    def scrape(self) -> list[str]:
        """Perform breadth-first traversal starting from current page state."""
        self.reset()
        timeout_seconds = getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15)
        timeout_ms = int(timeout_seconds * 1000)

        # Mark initial folder as visited to prevent re-traversal
        init_id = extract_folder_id(self.page.url)
        if init_id:
            self.visited_folder_ids.add(init_id)

        _wait_for_folder_items(self.page, timeout_ms)
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


def _scrape_source(source: str | Page) -> DriveFolderScraper:
    """Validate and scrape a Google Drive folder from a URL or Playwright Page.

    For URL sources, the Drive API (`_scrape_by_api`) is used when
    ``config.USE_GOOGLE_DRIVE_API`` is true and ``config.GOOGLE_API_KEY`` is set;
    otherwise the folder is scraped with Playwright.
    """
    if isinstance(source, str):
        validate_drive_folder_url(source)
        if should_use_drive_api():
            folder_id = extract_folder_id(source.strip())
            if not folder_id:
                raise ValueError(f"Could not extract folder ID from URL: {source}")
            return _scrape_by_api(folder_id, config.GOOGLE_API_KEY)
        playwright, browser, page = init_browser()
        try:
            timeout_seconds = getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15)
            timeout_ms = int(timeout_seconds * 1000)
            max_retries = getattr(config, "SCRAPE_RETRY_COUNT", 2)
            _navigate_folder(page, source, timeout_ms, max_retries)
            scraper = DriveFolderScraper(page)
            scraper.scrape()
            return scraper
        finally:
            cleanup_browser(playwright, browser, page)
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


if __name__ == "__main__":
    with console.status("[bold white]Exploring folder[/bold white]", spinner="dots"):
        l = scrape_classified(
            "https://drive.google.com/drive/folders/1g__ke0ZPOIrH7DcHocmLDqyltgsnkTjk"
        )
    for e in l:
        print(e)
    print("Total: " + str(len(l)))
