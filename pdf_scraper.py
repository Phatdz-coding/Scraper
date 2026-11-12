import config
import base64
from html.parser import HTMLParser
from pathlib import Path
from functools import partial
import struct
from urllib.parse import urlparse, parse_qs
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


def scrape_in_url_by_script(URL: str) -> Path:
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


def scrape_in_browser_by_script(browser: Browser, document_name: str) -> Path:
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


def scrape_in_page_by_script(page: Page, document_name: str) -> Path:
    """Save the PDF from the provided page and return its path."""
    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    total_pages = Template(page).get_total_pages()
    output_path = destination / document_name
    _download_pdf(page, output_path, total_pages)
    return output_path


def _jpeg_dimensions(image: bytes) -> tuple[int, int]:
    if image[:2] != b"\xff\xd8":
        raise ValueError("Viewer response is not a JPEG image")

    position = 2
    while position + 9 < len(image):
        if image[position] != 0xFF:
            position += 1
            continue

        marker = image[position + 1]
        position += 2
        if marker in {0xD8, 0xD9}:
            continue
        if position + 2 > len(image):
            break

        segment_length = struct.unpack(">H", image[position : position + 2])[0]
        if marker in set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(
            range(0xC9, 0xCC)
        ) | set(range(0xCD, 0xD0)):
            if position + 7 > len(image):
                break
            height, width = struct.unpack(">HH", image[position + 3 : position + 7])
            return width, height
        position += segment_length

    raise ValueError("Could not determine JPEG dimensions")


def _convert_image_to_jpeg(page: Page, image: bytes, content_type: str) -> bytes:
    encoded_image = base64.b64encode(image).decode("ascii")
    jpeg_data = page.evaluate(
        """
        async ({dataUrl}) => {
            const image = new Image();
            image.src = dataUrl;
            await image.decode();

            const canvas = document.createElement("canvas");
            canvas.width = image.naturalWidth;
            canvas.height = image.naturalHeight;
            const context = canvas.getContext("2d");
            context.fillStyle = "#ffffff";
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.drawImage(image, 0, 0);
            return canvas.toDataURL("image/jpeg", 0.95).split(",", 2)[1];
        }
        """,
        {
            "dataUrl": f"data:{content_type};base64,{encoded_image}",
        },
    )
    if not isinstance(jpeg_data, str):
        raise ValueError("Browser did not return converted image data")
    return base64.b64decode(jpeg_data)


def _fetch_image_batch(page: Page, endpoints: list[str]) -> list[dict[str, object]]:
    return page.evaluate(
        """
        async (urls) => {
            const encodeBody = (bytes) => {
                let binary = "";
                const chunkSize = 0x8000;
                for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                    binary += String.fromCharCode(
                        ...bytes.subarray(offset, offset + chunkSize)
                    );
                }
                return btoa(binary);
            };

            return Promise.all(urls.map(async (url) => {
                const response = await fetch(url, { credentials: "include" });
                const body = await response.arrayBuffer();
                return {
                    status: response.status,
                    contentType: response.headers.get("content-type") || "image/*",
                    body: encodeBody(new Uint8Array(body)),
                };
            }));
        }
        """,
        endpoints,
    )


def _write_jpegs_as_pdf(images: list[bytes], output_path: Path) -> None:
    objects: list[bytes] = []
    page_ids: list[int] = []

    def add_object(value: bytes) -> int:
        objects.append(value)
        return len(objects)

    pages_id = add_object(b"")
    for image in images:
        width, height = _jpeg_dimensions(image)
        image_id = add_object(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(image)} >>\nstream\n"
            ).encode()
            + image
            + b"\nendstream"
        )
        content = (
            f"q\n{width} 0 0 {height} 0 0 cm\n/Im{image_id} Do\nQ\n"
        ).encode()
        content_id = add_object(
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"endstream"
        )
        page_ids.append(
            add_object(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 "
                    f"{width} {height}] /Resources << /XObject << /Im{image_id} "
                    f"{image_id} 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode()
            )
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"
    ).encode()
    catalog_id = add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, value in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode())
        pdf.extend(value)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    output_path.write_bytes(pdf)


def scrape_inpage_by_api(page: Page, document_name: str) -> Path:
    """Download viewer images from the provided page and assemble them as a PDF."""
    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    scrape_progress.reset()
    total_pages = Template(page).get_total_pages()
    output_path = destination / document_name

    viewer_params: dict[str, str] = {}

    def handle_request(request):
        if "/viewer/img" in request.url:
            parsed_url = urlparse(request.url)
            query_params = parse_qs(parsed_url.query)
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
            page.wait_for_timeout(2_000)
        finally:
            page.remove_listener("request", handle_request)

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
        images: list[bytes] = []

        endpoints = []
        for page_number in range(first_page, first_page + total_pages):
            request_params = dict(viewer_params)
            request_params.update(
                {
                    "page": str(page_number),
                    "w": str(width),
                    "webp": "true",
                }
            )
            endpoints.append(
                "https://drive.google.com/viewer/img?" + urlencode(request_params)
            )

        for batch_start in range(0, len(endpoints), 6):
            batch_endpoints = endpoints[batch_start : batch_start + 6]
            responses = _fetch_image_batch(page, batch_endpoints)
            stop_after_batch = False

            for response in responses:
                status = response["status"]
                if status in {400, 404}:
                    stop_after_batch = True
                    break
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise RuntimeError(
                        f"Viewer API request failed with HTTP {status}"
                    )

                body = response["body"]
                content_type = response["contentType"]
                if not isinstance(body, str) or not isinstance(content_type, str):
                    raise RuntimeError("Viewer API returned an invalid image response")

                image = base64.b64decode(body)
                if image[:2] != b"\xff\xd8":
                    image = _convert_image_to_jpeg(
                        page, image, content_type.split(";", 1)[0]
                    )
                images.append(image)
                scrape_progress.increment(total_pages)

            if stop_after_batch:
                break

        if not images:
            raise ValueError("Viewer API returned no PDF page images")
        _write_jpegs_as_pdf(images, output_path)
        return output_path
    finally:
        scrape_progress.finish()

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
    scrape_inpage_by_api(
        "https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view"
    )
