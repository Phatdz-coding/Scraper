from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

import config
from process_images import fetch_image_batch
from tracker import (
    ProgressReporter,
    get_active_output_dir,
    get_active_progress,
    release_output_path,
    reserve_output_path,
    scrape_progress,
)

DRIVE_VIEWER_PREFIX = "https://drive.google.com/drive-viewer/"
MAX_ATTEMPTS = 3
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif"}
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


def _resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    destination = (
        Path(output_dir)
        if output_dir is not None
        else (get_active_output_dir() or Path(config.OUTPUT_DIR))
    )
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _extract_key(url: str) -> str | None:
    """Return the {key} from ``drive-viewer/{key}=w...-h...?...``."""
    if not url.startswith(DRIVE_VIEWER_PREFIX):
        return None
    tail = urlparse(url).path.removeprefix("/drive-viewer/")
    key = tail.split("=", 1)[0]
    return key or None


def _detect_extension(data: bytes, content_type: str) -> str:
    """Detect the image extension from magic bytes, falling back to Content-Type."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[4:8] == b"ftyp":
        return ".heic" if data[8:12] in HEIC_BRANDS else ".heif"
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/gif": ".gif",
    }
    ext = mapping.get(content_type.split(";", 1)[0].strip().lower())
    if ext is None:
        raise RuntimeError(f"Unsupported image format: {content_type or 'unknown'}")
    return ext


async def scrape_inpage_by_api(
    page: Page,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
    progress: ProgressReporter | None = None,
) -> Path:
    """Download the full-size image of a Drive viewer page via the drive-viewer API."""
    destination = _resolve_output_dir(output_dir)
    active_progress = progress or get_active_progress() or scrape_progress
    active_progress.reset()

    keys: list[str] = []

    def handle_request(request) -> None:
        key = _extract_key(request.url)
        if key and not keys:
            keys.append(key)

    output_path: Path | None = None
    try:
        page.on("request", handle_request)
        try:
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2_000)
        finally:
            page.remove_listener("request", handle_request)

        if not keys:
            raise ValueError("Could not capture the drive-viewer key from the page")
        endpoint = f"{DRIVE_VIEWER_PREFIX}{keys[0]}=w15000-h15000"

        image: bytes | None = None
        content_type = ""
        last_error: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                response = (await fetch_image_batch(page, [endpoint]))[0]
                status = response["status"]
                if isinstance(status, int) and 400 <= status < 500:
                    # 4xx: stop immediately, no retry.
                    raise RuntimeError(f"Drive viewer request failed with HTTP {status}")
                if not isinstance(status, int) or not 200 <= status < 300:
                    last_error = RuntimeError(
                        f"Drive viewer request failed with HTTP {status}"
                    )
                    continue
                body = response["body"]
                content_type = str(response["contentType"])
                if not isinstance(body, str) or not body:
                    last_error = RuntimeError("Drive viewer returned an empty response")
                    continue
                image = base64.b64decode(body)
                break
            except PlaywrightError as exc:
                last_error = exc
        if image is None:
            raise last_error or RuntimeError("Drive viewer request failed")

        extension = _detect_extension(image, content_type)
        name = Path(document_name)
        stem = name.stem if name.suffix.lower() in IMAGE_SUFFIXES else name.name
        output_path = await reserve_output_path(destination, f"{stem}{extension}")
        await asyncio.to_thread(output_path.write_bytes, image)
        active_progress.increment(1)
        return output_path
    except Exception:
        release_output_path(output_path)
        raise
    finally:
        active_progress.finish()
