from __future__ import annotations

import asyncio
import base64
import re
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from playwright.async_api import ConsoleMessage, Page, async_playwright

from scrape.classifiers import DocumentType
from scrape import config
from scrape.load_script import load as load_script
from scrape.process_images import (
    convert_image_to_jpeg,
    fetch_image_batch,
    write_jpegs_as_pdf,
)
from scrape.template_info import Template
from scrape.tracker import (
    ProgressReporter,
    get_active_output_dir,
    get_active_progress,
    release_output_path,
    reserve_output_path,
    resolve_download_filename,
    scrape_progress,
)

script = load_script("ggdrive_ms_word.js")
PAGE_SCRAPED_MARKER = "__PDF_SCRAPER_PAGE_SCRAPED__"


def _resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    if output_dir is not None:
        destination = Path(output_dir)
    else:
        destination = get_active_output_dir() or Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _resolve_progress(progress: ProgressReporter | None = None) -> ProgressReporter:
    return progress or get_active_progress() or scrape_progress


def _track_scraped_page(
    message: ConsoleMessage,
    total_pages: int,
    progress: ProgressReporter | None = None,
) -> None:
    if message.type == "log" and message.text == PAGE_SCRAPED_MARKER:
        _resolve_progress(progress).increment(total_pages)


async def _download_doc(
    page: Page,
    output_path: Path,
    total_pages: int,
    progress: ProgressReporter | None = None,
) -> None:
    active_progress = _resolve_progress(progress)
    callback = partial(
        _track_scraped_page, total_pages=total_pages, progress=active_progress
    )
    page.on("console", callback)
    try:
        await page.wait_for_selector(
            'img[src^="blob:"], .kix-appview-editor, .a-b-r-La, [role="main"]',
            timeout=60_000,
        )

        async with page.expect_download(timeout=600_000) as download_info:
            await page.evaluate(script)

        download = await download_info.value
        await download.save_as(str(output_path))
    finally:
        page.remove_listener("console", callback)
        active_progress.finish()


_download_pdf = _download_doc


async def scrape_in_url_by_script(
    URL: str,
    *,
    output_dir: Path | str | None = None,
    progress: ProgressReporter | None = None,
) -> Path:
    """Save the document rendered by the target page as PDF and return its path."""
    parsed_url = urlparse(URL)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL")

    destination = _resolve_output_dir(output_dir)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(accept_downloads=True)
            await page.goto(URL, wait_until="domcontentloaded")
            template = await Template.create(page, URL)
            try:
                total_pages = await template.get_total_pages()
            except Exception:
                total_pages = 1

            try:
                doc_name = await template.get_file_name(await page.content())
            except Exception:
                doc_name = (await page.title()) or "document.pdf"

            if not doc_name.lower().endswith(".pdf"):
                doc_name = (
                    re.sub(r"\.docx?$", "", doc_name, flags=re.IGNORECASE) + ".pdf"
                )

            output_path = await reserve_output_path(
                destination, doc_name, file_id=template.fileID
            )
            try:
                await _download_doc(page, output_path, total_pages, progress=progress)
            except Exception:
                release_output_path(output_path)
                raise
        finally:
            await browser.close()

    return output_path


async def scrape_inpage_by_script(
    page: Page,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
    progress: ProgressReporter | None = None,
) -> Path:
    """Save the document from the provided page as PDF and return its path."""
    destination = _resolve_output_dir(output_dir)

    file_id: str | None = None
    try:
        template = await Template.create(
            page, document_type=DocumentType.MICROSOFT_WORD
        )
        file_id = template.fileID
        total_pages = await template.get_total_pages()
    except Exception:
        total_pages = 1

    if not document_name.lower().endswith(".pdf"):
        document_name = (
            re.sub(r"\.docx?$", "", document_name, flags=re.IGNORECASE) + ".pdf"
        )

    output_path = await reserve_output_path(
        destination, document_name, file_id=file_id
    )
    try:
        await _download_doc(page, output_path, total_pages, progress=progress)
    except Exception:
        release_output_path(output_path)
        raise
    return output_path


async def scrape_inpage_by_api(
    page: Page,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
    progress: ProgressReporter | None = None,
) -> Path:
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
            "Page URL must match https://drive.google.com/file/{fileID}/view"
        )

    destination = _resolve_output_dir(output_dir)
    active_progress = _resolve_progress(progress)
    active_progress.reset()

    if not document_name.lower().endswith(".pdf"):
        document_name = document_name + ".pdf"
    output_path = await reserve_output_path(destination, document_name)

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
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_selector(
                'span[jsname="Dt5gRb"], img[src*="/viewer/img"]',
                state="attached",
                timeout=30_000,
            )
            await page.wait_for_timeout(2_000)
        finally:
            page.remove_listener("request", handle_request)

        try:
            template = await Template.create(
                page, document_type=DocumentType.GOOGLE_DOC
            )
            total_pages = await template.get_total_pages()
        except Exception:
            total_pages = 1

        missing = [name for name in ("id", "fssid") if name not in viewer_params]
        if missing:
            raise ValueError(
                f"Could not capture viewer parameters from the page: {', '.join(missing)}"
            )

        first_page = 0
        try:
            width = max(
                int(viewer_params.get("w", "1600")),
                1600,
            )
        except ValueError:
            width = 1600

        if config.MAX_CONCURRENT_IMG_API <= 0:
            raise ValueError("config.MAX_CONCURRENT_IMG_API must be greater than zero")

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
        for batch_start in range(0, len(endpoints), config.MAX_CONCURRENT_IMG_API):
            responses = await fetch_image_batch(
                page,
                endpoints[batch_start : batch_start + config.MAX_CONCURRENT_IMG_API],
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
                    image = await convert_image_to_jpeg(
                        page, image, content_type.split(";", 1)[0]
                    )
                images.append(image)
                active_progress.increment(total_pages)

            if stop_after_batch:
                break

        if not images:
            raise ValueError("Viewer API returned no document page images")
        await asyncio.to_thread(write_jpegs_as_pdf, images, output_path)
        return output_path
    except Exception:
        release_output_path(output_path)
        raise
    finally:
        active_progress.finish()


def _direct_download_sync(request: Request, output_path: Path) -> tuple[Path, int]:
    with urlopen(request) as response:
        response_status = response.status
        output_path.write_bytes(response.read())
    return output_path, response_status


def _fetch_sync(request: Request) -> tuple[bytes, int, object]:
    with urlopen(request) as response:
        return response.read(), response.status, response.headers


async def direct_download(
    file_id: str,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
    prefer_header_name: bool = False,
) -> tuple[Path, int]:
    """Download a Google Doc file directly as docx and return its path and status without blocking the event loop.

    When ``prefer_header_name`` is set, the filename comes from the HTTP
    ``Content-Disposition`` header (falling back to ``document_name``).
    """
    if not file_id or not file_id.strip():
        raise ValueError("fileID must be a non-empty string")

    endpoint = f"https://docs.google.com/document/export?format=docx&id={file_id.strip()}&includes_info_params=true"
    request = Request(
        endpoint,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        method="GET",
    )

    destination = _resolve_output_dir(output_dir)

    if prefer_header_name:
        body, status, headers = await asyncio.to_thread(_fetch_sync, request)
        name = resolve_download_filename(
            headers,
            document_name,
            prefer_header_name=True,
            default_extension=".docx",
        )
        output_path = await reserve_output_path(
            destination, name, file_id=file_id.strip()
        )
        try:
            await asyncio.to_thread(output_path.write_bytes, body)
        except Exception:
            release_output_path(output_path)
            raise
        return output_path, status

    output_path = await reserve_output_path(
        destination, document_name, file_id=file_id.strip()
    )

    try:
        return await asyncio.to_thread(_direct_download_sync, request, output_path)
    except Exception:
        release_output_path(output_path)
        raise


def switch_url_to_AllTabsView(url: str) -> str:
    """Convert a Google Docs URL to its Google Drive viewer URL."""
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or parsed_url.hostname != "docs.google.com"
    ):
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
    path, status = asyncio.run(
        direct_download(
            "15mUH39Zw_zsABKGTIWrf__eOx_4TOUgoRAyEkhFzbXE", "test.docx"
        )
    )
    print(status)
