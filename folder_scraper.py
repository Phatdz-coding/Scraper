"""Google Drive folder scraper module.

This module scrapes Google Drive folders recursively using Playwright synchronous API,
breadth-first search (BFS) traversal, icon-based format classification, and robust
error handling.
"""

from __future__ import annotations

import collections
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup
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

console = Console()


@dataclass
class DriveElement:
    """Represents an extracted Google Drive item element from the DOM."""

    data_id: str
    tabindex: str | None = None
    paths: list[str] = field(default_factory=list)
    g_elements: list[str] = field(default_factory=list)
    svg_inners: list[str] = field(default_factory=list)
    svg_outers: list[str] = field(default_factory=list)
    raw_html: str = ""


def _normalize_path(path_str: str) -> str:
    """Normalize SVG path d attribute by replacing commas with spaces and collapsing whitespace."""
    if not path_str:
        return ""
    return " ".join(path_str.replace(",", " ").split()).strip()


def matches_vector(d: str, expected_vector: str) -> bool:
    """Check if an SVG path d attribute matches an expected vector."""
    if not d or not expected_vector:
        return False
    if d.strip() == expected_vector.strip():
        return True
    return _normalize_path(d) == _normalize_path(expected_vector)


def _get_icon_data(element: DriveElement) -> tuple[list[str], list[str]]:
    """Return (paths, candidate_structures) scoped to the file icon container.

    This ensures that action icons (toolbar buttons, location column folder icons,
    sharing badges) inside the row do not bleed into element format classification.
    """
    if element.raw_html and (
        "data-file-icon" in element.raw_html
        or "data-location-column" in element.raw_html
        or "data-column-field" in element.raw_html
    ):
        try:
            soup = BeautifulSoup(element.raw_html, "html.parser")
            icon_container = (
                soup.select_one("[data-file-icon]")
                or soup.select_one("[data-file-icon-bg]")
                or soup.select_one('td[data-column-field="6"]')
                or soup.select_one("td:first-child")
            )
            if icon_container:
                scoped_paths = [
                    p.get("d", "") for p in icon_container.find_all("path") if p.get("d")
                ]
                scoped_g = [str(g) for g in icon_container.find_all("g")]
                scoped_outers = [str(s) for s in icon_container.find_all("svg")]
                scoped_inners = [str(s.decode_contents()) for s in icon_container.find_all("svg")]
                candidates = scoped_g + scoped_outers + scoped_inners
                return scoped_paths, candidates
        except Exception:
            pass

    candidates = list(element.g_elements) + list(element.svg_outers) + list(element.svg_inners)
    if element.raw_html and not candidates:
        candidates.append(element.raw_html)
    return element.paths, candidates


def _matches_icon_structure(candidate_htmls: list[str], configured_element: str) -> bool:
    """Match SVG/G structures against configured element string using normalized text and structural comparison."""
    if not configured_element or not candidate_htmls:
        return False

    def _normalize_str(s: str) -> str:
        return "".join(s.lower().split())

    norm_expected = _normalize_str(configured_element)
    for html in candidate_htmls:
        if norm_expected in _normalize_str(html):
            return True

    # Structural comparison via ElementTree
    try:
        expected_root = ET.fromstring(configured_element)
        expected_rects = [
            {k.lower(): " ".join(v.lower().split()) for k, v in child.attrib.items()}
            for child in expected_root
            if child.tag.endswith("rect")
        ]
        if expected_rects:
            for html in candidate_htmls:
                try:
                    wrapped = f"<root>{html}</root>"
                    cand_root = ET.fromstring(wrapped)
                    for g_node in cand_root.iter():
                        if g_node.tag.endswith("g"):
                            cand_rects = [
                                {k.lower(): " ".join(v.lower().split()) for k, v in child.attrib.items()}
                                for child in g_node
                                if child.tag.endswith("rect")
                            ]
                            if cand_rects == expected_rects:
                                return True
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback to BeautifulSoup matching
    try:
        soup_expected = BeautifulSoup(configured_element, "html.parser")
        exp_rects = [
            {
                k.lower(): " ".join(v.lower().split()) if isinstance(v, str) else v
                for k, v in r.attrs.items()
            }
            for r in soup_expected.find_all("rect")
        ]
        if exp_rects:
            for html in candidate_htmls:
                soup_cand = BeautifulSoup(html, "html.parser")
                for g in soup_cand.find_all("g"):
                    c_rects = [
                        {
                            k.lower(): " ".join(v.lower().split()) if isinstance(v, str) else v
                            for k, v in r.attrs.items()
                        }
                        for r in g.find_all("rect")
                    ]
                    if c_rects == exp_rects:
                        return True
    except Exception:
        pass

    return False


def _matches_icon_geometry(
    candidate_htmls: list[str],
    configured_element: str,
    *,
    required_path_count: int | None = None,
    require_rects: bool = True,
) -> bool:
    """Match configured SVG icons using stable path geometry and rectangle attributes."""
    if not configured_element or not candidate_htmls:
        return False

    try:
        expected = BeautifulSoup(configured_element, "html.parser")
        expected_paths = [
            _normalize_path(path.get("d", ""))
            for path in expected.find_all("path")
            if path.get("d")
        ]
        expected_rects = [
            {
                key.lower(): " ".join(value.lower().split())
                for key, value in rect.attrs.items()
                if isinstance(value, str)
            }
            for rect in expected.find_all("rect")
        ]
        if not expected_paths and not expected_rects:
            return False
        required_paths = expected_paths
        if required_path_count is not None:
            required_paths = expected_paths[:required_path_count]

        for html in candidate_htmls:
            candidate = BeautifulSoup(html, "html.parser")
            candidate_paths = {
                _normalize_path(path.get("d", ""))
                for path in candidate.find_all("path")
                if path.get("d")
            }
            if not all(path in candidate_paths for path in required_paths):
                continue

            candidate_rects = [
                {
                    key.lower(): " ".join(value.lower().split())
                    for key, value in rect.attrs.items()
                    if isinstance(value, str)
                }
                for rect in candidate.find_all("rect")
            ]
            if not require_rects or all(rect in candidate_rects for rect in expected_rects):
                return True
    except Exception:
        return False

    return False


def is_pdf(element: DriveElement) -> bool:
    """PDF element contains exactly one <path> element whose d attribute matches config.pdf_icon_vector."""
    expected = getattr(config, "pdf_icon_vector", "")
    paths, _ = _get_icon_data(element)
    matching = [d for d in paths if matches_vector(d, expected)]
    return len(matching) == 1


def is_folder(element: DriveElement) -> bool:
    """Folder element contains an SVG <path> whose d attribute matches any configured folder icon vector.

    Supports:
    - config.folder_icon_vector (shared folder)
    - config.folder_icon_vector2 (shared drive / member folder)
    - config.folder_icon_vector3 (standard folder)
    """
    expected_vectors = [
        getattr(config, "folder_icon_vector", ""),
        getattr(config, "folder_icon_vector2", ""),
        getattr(config, "folder_icon_vector3", ""),
    ]
    expected_vectors = [v for v in expected_vectors if v]
    paths, _ = _get_icon_data(element)
    return any(
        matches_vector(d, expected)
        for d in paths
        for expected in expected_vectors
    )


def is_microsoft_word(element: DriveElement) -> bool:
    """Microsoft Word element contains an SVG <path> whose d attribute matches config.microsoft_word_icon_vector."""
    expected = getattr(config, "microsoft_word_icon_vector", "")
    paths, _ = _get_icon_data(element)
    return any(matches_vector(d, expected) for d in paths)


def is_image(element: DriveElement) -> bool:
    """Image element (PNG, JPG, JPEG, WEBP) contains an SVG <path> whose d attribute matches config.png_icon_vector."""
    expected = getattr(config, "png_icon_vector", "")
    paths, _ = _get_icon_data(element)
    return any(matches_vector(d, expected) for d in paths)


def is_google_doc(element: DriveElement) -> bool:
    """Google Doc element recognized through pattern matching against config.google_doc_icon_element."""
    configured = getattr(config, "google_doc_icon_element", "")
    _, candidates = _get_icon_data(element)
    return _matches_icon_structure(candidates, configured)


def is_xlsx(element: DriveElement) -> bool:
    """XLSX element recognized through pattern matching against config.xlsx_icon_element."""
    configured = getattr(config, "xlsx_icon_element", "")
    _, candidates = _get_icon_data(element)
    return _matches_icon_structure(candidates, configured)


def is_pptx(element: DriveElement) -> bool:
    """PPTX element recognized through its visible SVG geometry."""
    configured = getattr(config, "pptx_icon_element", "")
    _, candidates = _get_icon_data(element)
    # <use> definitions may be hoisted outside the icon subtree by the browser.
    # The first two paths are the visible PPTX background and P glyph.
    return _matches_icon_geometry(
        candidates,
        configured,
        required_path_count=2,
        require_rects=False,
    )


def is_zip_folder(element: DriveElement) -> bool:
    """ZIP folder element recognized through stable SVG geometry from config.zip_folder_icon_element."""
    configured = getattr(config, "zip_folder_icon_element", "")
    _, candidates = _get_icon_data(element)
    return _matches_icon_geometry(candidates, configured)


def classify_element(element: DriveElement) -> str:
    """Assign exactly one label to the element in deterministic order:

    1. pdf
    2. google_doc
    3. microsoft_word
    4. image
    5. xlsx
    6. folder
    7. pptx
    8. zip_folder
    9. unknown
    """
    if is_pdf(element):
        return "pdf"
    if is_google_doc(element):
        return "google_doc"
    if is_microsoft_word(element):
        return "microsoft_word"
    if is_image(element):
        return "image"
    if is_xlsx(element):
        return "xlsx"
    if is_folder(element):
        return "folder"
    if is_pptx(element):
        return "pptx"
    if is_zip_folder(element):
        return "zip_folder"
    return "unknown"


def generate_url(label: str, file_id: str) -> str | None:
    """Generate target URL based on element label and file_id."""
    if not file_id:
        return None
    if label in {"pdf", "google_doc", "microsoft_word", "image", "pptx", "zip_folder"}:
        return f"https://drive.google.com/file/d/{file_id}/view"
    if label == "xlsx":
        return f"https://docs.google.com/spreadsheets/d/{file_id}/view"
    if label == "folder":
        return f"https://drive.google.com/drive/folders/{file_id}"
    return None


def log_unsupported_element(file_id: str, tabindex: str | None) -> None:
    """Log unsupported element in orange warning format."""
    message = f"URL not supported: https://drive.google.com/open?id={file_id}&authuser=0 | Tab Index: {tabindex}"
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
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Must be http or https: {url}")

    if "drive.google.com" not in parsed.netloc.lower():
        raise ValueError(f"Invalid domain '{parsed.netloc}'. Must be drive.google.com: {url}")

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
        self.seen_file_ids: set[str] = set()
        self.visited_folder_ids: set[str] = set()
        self.queue: collections.deque[tuple[str, int]] = collections.deque()

    def _extract_and_queue(self, current_depth: int) -> None:
        """Read and classify elements on the current page, queueing child folders and recording files."""
        max_depth = getattr(config, "FOLDER_MAX_DEPTH", 5)
        elements = get_file_elements(self.page)

        for element in elements:
            label = classify_element(element)

            if label == "unknown":
                log_unsupported_element(element.data_id, element.tabindex)
                continue

            if label == "folder":
                folder_id = element.data_id
                if folder_id and folder_id not in self.visited_folder_ids:
                    self.visited_folder_ids.add(folder_id)
                    if current_depth + 1 <= max_depth:
                        folder_url = generate_url("folder", folder_id)
                        if folder_url:
                            self.queue.append((folder_url, current_depth + 1))
                continue

            # File format (pdf, google_doc, microsoft_word, image, xlsx, pptx, zip_folder)
            file_id = element.data_id
            if file_id and file_id not in self.seen_file_ids:
                url = generate_url(label, file_id)
                if url:
                    self.seen_file_ids.add(file_id)
                    self.discovered_file_urls.append(url)

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
                    log_error(f"Retries exhausted for folder {folder_url}. Skipping folder.")
                    return
            except Exception as e:
                log_error(
                    f"Error accessing folder {folder_url} (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )
                attempt += 1
                if attempt > max_retries:
                    log_error(f"Retries exhausted for folder {folder_url}. Skipping folder.")
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


def scrape(source: str | Page) -> list[str]:
    """Scrapes a Google Drive folder and returns discovered file URLs.

    Supports both a Google Drive folder URL (str) and an existing Playwright Page.

    Args:
        source: A Google Drive folder URL string or a Playwright Page instance.

    Returns:
        A list of discovered file URLs in first-discovery order.
    """
    if isinstance(source, str):
        validate_drive_folder_url(source)
        playwright, browser, page = init_browser()
        try:
            timeout_seconds = getattr(config, "SCRAPE_TIMEOUT_SECONDS", 15)
            timeout_ms = int(timeout_seconds * 1000)
            max_retries = getattr(config, "SCRAPE_RETRY_COUNT", 2)
            _navigate_folder(page, source, timeout_ms, max_retries)
            return scrape(page)
        finally:
            cleanup_browser(playwright, browser, page)
    elif isinstance(source, Page):
        scraper = DriveFolderScraper(source)
        return scraper.scrape()
    else:
        raise TypeError(f"Expected source to be str or Page, got {type(source).__name__}")

if __name__ == "__main__":
    l = scrape("https://drive.google.com/drive/folders/1MBkJcFyPzuLeCOoMLMo-hGzrM4Yu_lTr")
    for e in l:
        print(e)
    print(len(l))