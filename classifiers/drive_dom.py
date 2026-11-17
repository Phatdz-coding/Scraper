"""Google Drive DOM/SVG element classification helpers."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

import config
from classifiers.models import ClassifiedLink, DocumentType, DriveElement, WebsiteType

logger = logging.getLogger(__name__)

def _normalize_path(path_str: str) -> str:
    """Normalize SVG path data."""
    if not path_str:
        return ""
    return " ".join(path_str.replace(",", " ").split()).strip()


def matches_vector(d: str, expected_vector: str) -> bool:
    """Check if an SVG path matches an expected vector."""
    if not d or not expected_vector:
        return False
    return d.strip() == expected_vector.strip() or _normalize_path(
        d
    ) == _normalize_path(expected_vector)


def _get_icon_data(element: DriveElement) -> tuple[list[str], list[str]]:
    """Return paths and candidate structures scoped to the file icon."""
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
                paths = [
                    p.get("d", "")
                    for p in icon_container.find_all("path")
                    if p.get("d")
                ]
                candidates = (
                    [str(g) for g in icon_container.find_all("g")]
                    + [str(svg) for svg in icon_container.find_all("svg")]
                    + [
                        str(svg.decode_contents())
                        for svg in icon_container.find_all("svg")
                    ]
                )
                return paths, candidates
        except Exception:
            pass

    candidates = (
        list(element.g_elements) + list(element.svg_outers) + list(element.svg_inners)
    )
    if element.raw_html and not candidates:
        candidates.append(element.raw_html)
    return element.paths, candidates


def _matches_icon_structure(
    candidate_htmls: list[str], configured_element: str
) -> bool:
    """Match an icon structure using normalized text and rectangle structure."""
    if not configured_element or not candidate_htmls:
        return False

    normalize = lambda value: "".join(value.lower().split())
    expected_text = normalize(configured_element)
    if any(expected_text in normalize(html) for html in candidate_htmls):
        return True

    try:
        expected_root = ET.fromstring(configured_element)
        expected_rects = [
            {
                key.lower(): " ".join(value.lower().split())
                for key, value in child.attrib.items()
            }
            for child in expected_root
            if child.tag.endswith("rect")
        ]
        if expected_rects:
            for html in candidate_htmls:
                try:
                    root = ET.fromstring(f"<root>{html}</root>")
                    for node in root.iter():
                        if node.tag.endswith("g"):
                            rects = [
                                {
                                    key.lower(): " ".join(value.lower().split())
                                    for key, value in child.attrib.items()
                                }
                                for child in node
                                if child.tag.endswith("rect")
                            ]
                            if rects == expected_rects:
                                return True
                except Exception:
                    continue
    except Exception:
        pass

    try:
        expected_soup = BeautifulSoup(configured_element, "html.parser")
        expected_rects = [
            {
                key.lower(): " ".join(value.lower().split())
                if isinstance(value, str)
                else value
                for key, value in rect.attrs.items()
            }
            for rect in expected_soup.find_all("rect")
        ]
        if expected_rects:
            for html in candidate_htmls:
                soup = BeautifulSoup(html, "html.parser")
                for node in soup.find_all("g"):
                    rects = [
                        {
                            key.lower(): " ".join(value.lower().split())
                            if isinstance(value, str)
                            else value
                            for key, value in rect.attrs.items()
                        }
                        for rect in node.find_all("rect")
                    ]
                    if rects == expected_rects:
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
    """Match icons using stable path geometry and rectangle attributes."""
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
        required_paths = (
            expected_paths
            if required_path_count is None
            else expected_paths[:required_path_count]
        )
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
            if not require_rects or all(
                rect in candidate_rects for rect in expected_rects
            ):
                return True
    except Exception:
        return False
    return False


def is_pdf(element: DriveElement) -> bool:
    expected = getattr(config, "pdf_icon_vector", "")
    paths, _ = _get_icon_data(element)
    return len([path for path in paths if matches_vector(path, expected)]) == 1


def is_folder(element: DriveElement) -> bool:
    expected = [
        getattr(config, name, "")
        for name in ("folder_icon_vector", "folder_icon_vector2", "folder_icon_vector3")
    ]
    paths, _ = _get_icon_data(element)
    return any(
        matches_vector(path, vector) for path in paths for vector in expected if vector
    )


def is_microsoft_word(element: DriveElement) -> bool:
    expected = getattr(config, "microsoft_word_icon_vector", "")
    paths, _ = _get_icon_data(element)
    return any(matches_vector(path, expected) for path in paths)


def is_image(element: DriveElement) -> bool:
    expected = getattr(config, "png_icon_vector", "")
    paths, _ = _get_icon_data(element)
    return any(matches_vector(path, expected) for path in paths)


def is_google_doc(element: DriveElement) -> bool:
    _, candidates = _get_icon_data(element)
    return _matches_icon_structure(
        candidates, getattr(config, "google_doc_icon_element", "")
    )


def is_xlsx(element: DriveElement) -> bool:
    _, candidates = _get_icon_data(element)
    return _matches_icon_structure(candidates, getattr(config, "xlsx_icon_element", ""))


def is_pptx(element: DriveElement) -> bool:
    _, candidates = _get_icon_data(element)
    return _matches_icon_geometry(
        candidates,
        getattr(config, "pptx_icon_element", ""),
        required_path_count=2,
        require_rects=False,
    )


def is_zip_folder(element: DriveElement) -> bool:
    _, candidates = _get_icon_data(element)
    return _matches_icon_geometry(
        candidates, getattr(config, "zip_folder_icon_element", "")
    )


def classify_element(element: DriveElement) -> DocumentType:
    """Return the single supported label for an element."""
    if is_pdf(element):
        return DocumentType.PDF
    if is_google_doc(element):
        return DocumentType.GOOGLE_DOC
    if is_microsoft_word(element):
        return DocumentType.MICROSOFT_WORD
    if is_image(element):
        return DocumentType.IMAGE
    if is_xlsx(element):
        return DocumentType.XLSX
    if is_folder(element):
        return DocumentType.FOLDER
    if is_pptx(element):
        return DocumentType.PPTX
    if is_zip_folder(element):
        return DocumentType.ZIP_FOLDER
    return DocumentType.UNSUPPORTED_FORMAT


def generate_url(label: DocumentType, file_id: str) -> str | None:
    """Generate the target URL for a classified element."""
    try:
        if not file_id:
            return None
        if label in {
            DocumentType.PDF,
            DocumentType.GOOGLE_DOC,
            DocumentType.MICROSOFT_WORD,
            DocumentType.IMAGE,
            DocumentType.PPTX,
            DocumentType.ZIP_FOLDER,
        }:
            return f"https://drive.google.com/file/d/{file_id}/view"
        if label == DocumentType.XLSX:
            return f"https://docs.google.com/spreadsheets/d/{file_id}/view"
        if label == DocumentType.FOLDER:
            return f"https://drive.google.com/drive/folders/{file_id}"
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning("Unable to generate URL for file %r: %s", file_id, exc)
        return None
    return None


def generate_classified_url(
    label: DocumentType, file_id: str
) -> ClassifiedLink | None:
    """Generate a ClassifiedLink for a classified Google Drive element."""
    url = generate_url(label, file_id)
    if url is None:
        return None
    return ClassifiedLink(
        url=url,
        website_type=WebsiteType.GOOGLE_DRIVE,
        document_type=label,
    )