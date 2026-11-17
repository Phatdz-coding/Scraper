"""Google Drive folder scraper module.

This module scrapes Google Drive folders recursively using Playwright synchronous API,
breadth-first search (BFS) traversal, icon-based format classification, and robust
error handling.
"""

from __future__ import annotations

import collections
from urllib.parse import urlparse
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
    """Scrapes Google Drive folders recursively using breadth-first search (BFS)."""

    def __init__(self, page: Page):
        self.page = page
        self.discovered_file_urls: list[str] = []
        self.discovered_file_links: list[ClassifiedLink] = []
        self.seen_file_ids: set[str] = set()
        self.visited_folder_ids: set[str] = set()
        self.queue: collections.deque[tuple[str, int]] = collections.deque()

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
    """Validate and scrape a Google Drive folder from a URL or Playwright Page."""
    if isinstance(source, str):
        validate_drive_folder_url(source)
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
