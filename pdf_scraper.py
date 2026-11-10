import config
from html.parser import HTMLParser
from pathlib import Path
from functools import partial
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from template_info import Template


from playwright.sync_api import Browser, ConsoleMessage, Page, sync_playwright

from tracker import scrape_progress

script = (Path(__file__).parent / "scripts/ggdrive_pdf.js").read_text(encoding="utf-8")
PAGE_SCRAPED_MARKER = "__PDF_SCRAPER_PAGE_SCRAPED__"


def _track_scraped_page(message: ConsoleMessage, total_pages: int) -> None:
    if message.type == "log" and message.text == PAGE_SCRAPED_MARKER:
        scrape_progress.increment(total_pages)


def _download_pdf(page: Page, output_path: Path, total_pages: int) -> None:
    callback = partial(_track_scraped_page, total_pages=total_pages)
    page.on("console", callback)
    try:
        page.wait_for_selector('img[src^="blob:"]', timeout=60_000)

        with page.expect_download(timeout=600_000) as download_info:
            page.evaluate(script)

        download_info.value.save_as(str(output_path))
    finally:
        page.remove_listener("console", callback)


def simple_scraper(URL: str) -> Path:
    """Save the PDF rendered by the target page and return its path."""
    parsed_url = urlparse(URL)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL")

    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(accept_downloads=True)
            page.goto(URL, wait_until="domcontentloaded")
            template = Template(page, URL)
            total_pages = template.get_total_pages()
            output_path = destination / template.get_file_name(page.content())
            _download_pdf(page, output_path, total_pages)
        finally:
            browser.close()

    return output_path


def simple_scraper_with_browser(browser: Browser, document_name: str) -> Path:
    """Save the PDF from the browser's current page and return its path."""
    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    if not browser.contexts:
        raise ValueError("Browser must have an open context")

    context = browser.contexts[0]
    if not context.pages:
        raise ValueError("Browser context must have an open page")

    page = context.pages[-1]
    total_pages = Template(page).get_total_pages()
    output_path = destination / document_name
    _download_pdf(page, output_path, total_pages)
    return output_path


def direct_download(file_id: str, document_name: str) -> Path:
    """Download a Google Drive file directly and return the saved path."""
    if not file_id or not file_id.strip():
        raise ValueError("fileID must be a non-empty string")
    endpoint = "https://drive.usercontent.google.com/download"
    request = Request(
        f"{endpoint}?{urlencode({'id': file_id.strip(), 'export': 'download', 'authuser': '0'})}",
        method="GET",
    )

    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / document_name

    with urlopen(request) as response:
        output_path.write_bytes(response.read())

    return output_path


if __name__ == "__main__":
    simple_scraper(
        "https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view"
    )
