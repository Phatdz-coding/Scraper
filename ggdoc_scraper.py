import base64
import re
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import ConsoleMessage, Page, sync_playwright

import config
from process_images import (
    convert_image_to_jpeg,
    fetch_image_batch,
    write_jpegs_as_pdf,
)
from template_info import Template
from tracker import scrape_progress

script = (Path(__file__).parent / "scripts/ggdrive_doc.js").read_text(encoding="utf-8")
PAGE_SCRAPED_MARKER = "__PDF_SCRAPER_PAGE_SCRAPED__"


def _track_scraped_page(message: ConsoleMessage, total_pages: int) -> None:
    if message.type == "log" and message.text == PAGE_SCRAPED_MARKER:
        scrape_progress.increment(total_pages)


def _download_doc(page: Page, output_path: Path, total_pages: int) -> None:
    callback = partial(_track_scraped_page, total_pages=total_pages)
    page.on("console", callback)
    try:
        page.wait_for_selector(
            'img[src^="blob:"], .kix-appview-editor, .a-b-r-La, [role="main"]',
            timeout=60_000,
        )

        with page.expect_download(timeout=600_000) as download_info:
            page.evaluate(script)

        download_info.value.save_as(str(output_path))
    finally:
        page.remove_listener("console", callback)
        scrape_progress.finish()


_download_pdf = _download_doc


def scrape_in_url_by_script(URL: str) -> Path:
    """Save the document rendered by the target page as PDF and return its path."""
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
            try:
                total_pages = template.get_total_pages()
            except Exception:
                total_pages = 1

            try:
                doc_name = template.get_file_name(page.content())
            except Exception:
                doc_name = page.title() or "document.pdf"

            if not doc_name.lower().endswith(".pdf"):
                doc_name = (
                    re.sub(r"\.docx?$", "", doc_name, flags=re.IGNORECASE) + ".pdf"
                )

            output_path = destination / doc_name
            _download_doc(page, output_path, total_pages)
        finally:
            browser.close()

    return output_path


def scrape_inpage_by_script(page: Page, document_name: str) -> Path:
    """Save the document from the provided page as PDF and return its path."""
    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    try:
        total_pages = Template(page).get_total_pages()
    except Exception:
        total_pages = 1

    if not document_name.lower().endswith(".pdf"):
        document_name = (
            re.sub(r"\.docx?$", "", document_name, flags=re.IGNORECASE) + ".pdf"
        )

    output_path = destination / document_name
    _download_doc(page, output_path, total_pages)
    return output_path


def scrape_inpage_by_api(page: Page, document_name: str) -> Path:
    """Download document viewer images from the page and assemble a PDF.
    IMPORTANT: this function only works if the page.url="https://drive.google.com/file/d/{fileID}/view"
    """
    parsed_url = urlparse(page.url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "drive.google.com"
        or not re.fullmatch(r"/file/d/[^/]+/view/?", parsed_url.path)
    ):
        raise ValueError(
            "Page URL must match "
            "https://drive.google.com/file/{fileID}/view"
        )

    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    scrape_progress.reset()

    if not document_name.lower().endswith(".docx"):
        document_name = document_name + ".docx"
    output_path = destination / document_name

    viewer_params: dict[str, str] = {}

    def handle_request(request) -> None:
        if "/viewer/img" not in request.url:
            return
        query_params = parse_qs(urlparse(request.url).query)
        if not viewer_params:
            viewer_params.update(
                {
                    name: values[0]
                    for name, values in query_params.items()
                    if values and values[0]
                }
            )

    try:
        page.on("request", handle_request)
        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(
                'span[jsname="Dt5gRb"], img[src*="/viewer/img"]',
                state="attached",
                timeout=30_000,
            )
            page.wait_for_timeout(2_000)
        finally:
            page.remove_listener("request", handle_request)

        try:
            total_pages = Template(page).get_total_pages()
        except Exception:
            total_pages = 1

        missing = [name for name in ("id", "fssid") if name not in viewer_params]
        if missing:
            raise ValueError(
                f"Could not capture viewer parameters from the page: {', '.join(missing)}"
            )

        first_page = int(viewer_params.get("page", "0"))
        try:
            width = max(int(viewer_params.get("w", "1600")), 1600)
        except ValueError:
            width = 1600

        if config.API_CONCURRENCY <= 0:
            raise ValueError("config.API_CONCURRENCY must be greater than zero")

        endpoints = []
        for page_number in range(first_page, first_page + total_pages):
            request_params = dict(viewer_params)
            request_params.update(
                {"page": str(page_number), "w": str(width), "webp": "true"}
            )
            endpoints.append(
                "https://drive.google.com/viewer/img?" + urlencode(request_params)
            )

        images: list[bytes] = []
        for batch_start in range(0, len(endpoints), config.API_CONCURRENCY):
            responses = fetch_image_batch(
                page,
                endpoints[batch_start : batch_start + config.API_CONCURRENCY],
            )
            stop_after_batch = False

            for response in responses:
                status = response["status"]
                if status in {400, 404}:
                    stop_after_batch = True
                    break
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise RuntimeError(f"Viewer API request failed with HTTP {status}")

                body = response["body"]
                content_type = response["contentType"]
                if not isinstance(body, str) or not isinstance(content_type, str):
                    raise RuntimeError("Viewer API returned an invalid image response")

                image = base64.b64decode(body)
                if image[:2] != b"\xff\xd8":
                    image = convert_image_to_jpeg(
                        page, image, content_type.split(";", 1)[0]
                    )
                images.append(image)
                scrape_progress.increment(total_pages)

            if stop_after_batch:
                break

        if not images:
            raise ValueError("Viewer API returned no document page images")
        write_jpegs_as_pdf(images, output_path)
        return output_path
    finally:
        scrape_progress.finish()


def direct_download(file_id: str, document_name: str) -> tuple[Path, int]:
    """Download a Google Doc file directly as docx and return its path and status."""
    if not file_id or not file_id.strip():
        raise ValueError("fileID must be a non-empty string")

    endpoint = f"https://docs.google.com/document/export?format=docx&id={file_id.strip()}&includes_info_params=true"
    request = Request(
        endpoint,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        method="GET",
    )

    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / document_name

    with urlopen(request) as response:
        response_status = response.status
        output_path.write_bytes(response.read())

    return output_path, response_status

def switch_url_to_AllTabsView(url: str) -> str:
    """Convert a Google Docs URL to its Google Drive viewer URL."""
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or parsed_url.hostname != "docs.google.com":
        raise ValueError("URL must be a Google Docs URL")

    match = re.fullmatch(
        r"/document/(?:u/\d+/)?d/([^/]+)(?:/.*)?",
        parsed_url.path,
    )
    if match is None:
        raise ValueError("URL must contain a Google Docs document ID")

    file_id = match.group(1)
    return f"https://drive.google.com/file/d/{file_id}/view"

if __name__ == "__main__":
    path,status = direct_download("15mUH39Zw_zsABKGTIWrf__eOx_4TOUgoRAyEkhFzbXE", "test.docx")
    print(status)
