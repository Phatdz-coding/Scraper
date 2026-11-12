import base64
import struct
from pathlib import Path

from playwright.sync_api import Page


def jpeg_dimensions(image: bytes) -> tuple[int, int]:
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


def convert_image_to_jpeg(page: Page, image: bytes, content_type: str) -> bytes:
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
        {"dataUrl": f"data:{content_type};base64,{encoded_image}"},
    )
    if not isinstance(jpeg_data, str):
        raise ValueError("Browser did not return converted image data")
    return base64.b64decode(jpeg_data)


def fetch_image_batch(page: Page, endpoints: list[str]) -> list[dict[str, object]]:
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


def write_jpegs_as_pdf(images: list[bytes], output_path: Path) -> None:
    objects: list[bytes] = []
    page_ids: list[int] = []

    def add_object(value: bytes) -> int:
        objects.append(value)
        return len(objects)

    pages_id = add_object(b"")
    for image in images:
        width, height = jpeg_dimensions(image)
        image_id = add_object(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                f"/Length {len(image)} >>\nstream\n"
            ).encode()
            + image
            + b"\nendstream"
        )
        content = f"q\n{width} 0 0 {height} 0 0 cm\n/Im{image_id} Do\nQ\n".encode()
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