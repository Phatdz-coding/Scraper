from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import Download, Error as PlaywrightError, Page

from scrape import config
from scrape.template_info import Template
from scrape.tracker import (
    get_active_output_dir,
    release_output_path,
    reserve_output_path,
)

GOOGLE_DRIVE_DOWNLOAD_ENDPOINT = "https://drive.usercontent.google.com/download"


def _resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    if output_dir is not None:
        destination = Path(output_dir)
    else:
        destination = get_active_output_dir() or Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _build_download_url(file_id: str) -> str:
    return (
        f"{GOOGLE_DRIVE_DOWNLOAD_ENDPOINT}?"
        f"{urlencode({'id': file_id.strip(), 'export': 'download', 'authuser': '0'})}"
    )


async def download_inpage(
    page: Page,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
) -> Path:
    """Download a Google Drive archive by opening its download URL in a new page.

    When Google Drive shows the virus-scan confirmation page, submits
    ``#download-form`` to trigger the browser download. Also handles direct
    downloads that begin immediately upon navigation.
    """
    file_id = Template(page, page.url).fileID
    if not file_id or not file_id.strip():
        raise ValueError("Could not extract Google Drive file ID from page URL")

    download_url = _build_download_url(file_id)
    destination = _resolve_output_dir(output_dir)
    output_path = await reserve_output_path(
        destination, document_name, file_id=file_id.strip()
    )

    new_page = await page.context.new_page()
    loop = asyncio.get_running_loop()
    download_future: asyncio.Future[Download] = loop.create_future()

    def _on_download(download: Download) -> None:
        if not download_future.done():
            download_future.set_result(download)

    new_page.on("download", _on_download)
    try:
        try:
            await new_page.goto(download_url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            # Chromium aborts navigation with "Download is starting" when the URL
            # triggers an immediate file download instead of the confirmation HTML page.
            if "Download is starting" not in str(exc) and not download_future.done():
                raise

        if not download_future.done():
            await new_page.wait_for_selector("#download-form", timeout=30_000)
            await new_page.evaluate(
                "document.getElementById('download-form').submit()"
            )

        download = await asyncio.wait_for(download_future, timeout=600)
        await download.save_as(str(output_path))
        return output_path
    except Exception:
        release_output_path(output_path)
        raise
    finally:
        new_page.remove_listener("download", _on_download)
        await new_page.close()
