"""Multi-stage link, website, and document type classification package."""

from __future__ import annotations

from classifiers.drive_dom import (
    _get_icon_data,
    _matches_icon_geometry,
    _matches_icon_structure,
    _normalize_path,
    classify_element,
    generate_classified_url,
    generate_url,
    is_folder,
    is_google_doc,
    is_image,
    is_microsoft_word,
    is_pdf,
    is_pptx,
    is_xlsx,
    is_zip_folder,
    matches_vector,
)
from classifiers.input_parser import (
    InputParser,
    LinkInputParser,
    parse_links_input,
)
from classifiers.models import (
    ClassifiedLink,
    DocumentType,
    DriveElement,
    WebsiteType,
)
from classifiers.page_doc_classifier import (
    GoogleDocPageDocumentStrategy,
    GoogleDrivePageDocumentStrategy,
    PageDocumentClassifier,
    PageDocumentClassifierStrategy,
    ScribdPageDocumentStrategy,
    classify_document_by_page,
    classify_document_by_page_async,
)
from classifiers.pipeline import LinkClassificationPipeline
from classifiers.url_doc_classifier import (
    GoogleDocUrlDocumentStrategy,
    GoogleDriveUrlDocumentStrategy,
    ScribdUrlDocumentStrategy,
    UrlDocumentClassifier,
    UrlDocumentClassifierStrategy,
    classify_document_by_url,
    classify_documents_by_url,
)
from classifiers.website_classifier import (
    GoogleDocWebsiteStrategy,
    GoogleDriveWebsiteStrategy,
    ScribdWebsiteStrategy,
    WebsiteClassifier,
    WebsiteMatchingStrategy,
    classify_website,
    classify_websites,
    log_classification_warning,
)

__all__ = [
    # Enums & Data Models
    "DocumentType",
    "WebsiteType",
    "DriveElement",
    "ClassifiedLink",
    # Legacy Google Drive DOM Classifiers
    "matches_vector",
    "is_pdf",
    "is_folder",
    "is_microsoft_word",
    "is_image",
    "is_google_doc",
    "is_xlsx",
    "is_pptx",
    "is_zip_folder",
    "classify_element",
    "generate_url",
    "generate_classified_url",
    # Input Parsing
    "InputParser",
    "LinkInputParser",
    "parse_links_input",
    # Stage 1: Website Classification
    "WebsiteMatchingStrategy",
    "GoogleDriveWebsiteStrategy",
    "GoogleDocWebsiteStrategy",
    "ScribdWebsiteStrategy",
    "WebsiteClassifier",
    "classify_website",
    "classify_websites",
    "log_classification_warning",
    # Stage 2a: URL-Based Document Classification
    "UrlDocumentClassifierStrategy",
    "GoogleDriveUrlDocumentStrategy",
    "GoogleDocUrlDocumentStrategy",
    "ScribdUrlDocumentStrategy",
    "UrlDocumentClassifier",
    "classify_document_by_url",
    "classify_documents_by_url",
    # Stage 2b: Playwright Page-Based Document Classification
    "PageDocumentClassifierStrategy",
    "GoogleDrivePageDocumentStrategy",
    "GoogleDocPageDocumentStrategy",
    "ScribdPageDocumentStrategy",
    "PageDocumentClassifier",
    "classify_document_by_page",
    "classify_document_by_page_async",
    # Pipeline Coordinator
    "LinkClassificationPipeline",
]
