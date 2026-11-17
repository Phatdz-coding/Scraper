"""Coordinator pipeline for multi-stage link and document classification."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import overload

from playwright.sync_api import Page

from classifiers.models import ClassifiedLink, DocumentType, WebsiteType
from classifiers.page_doc_classifier import PageDocumentClassifier
from classifiers.url_doc_classifier import UrlDocumentClassifier
from classifiers.website_classifier import WebsiteClassifier


class LinkClassificationPipeline:
    """Coordinates Stage 1 (WebsiteType), Stage 2a (URL DocumentType), and Stage 2b (Page DocumentType)."""

    def __init__(
        self,
        website_classifier: WebsiteClassifier | None = None,
        url_doc_classifier: UrlDocumentClassifier | None = None,
        page_doc_classifier: PageDocumentClassifier | None = None,
    ) -> None:
        self.website_classifier = website_classifier or WebsiteClassifier()
        self.url_doc_classifier = url_doc_classifier or UrlDocumentClassifier()
        self.page_doc_classifier = page_doc_classifier or PageDocumentClassifier()

    def classify_websites(
        self, source: Path | str | Sequence[str]
    ) -> list[ClassifiedLink]:
        """Run Stage 1: parse input source and classify WebsiteType (document_type=None)."""
        return self.website_classifier.classify(source)

    def classify_by_url(
        self, links: Iterable[ClassifiedLink], *, overwrite: bool = False
    ) -> list[ClassifiedLink]:
        """Run Stage 2a: resolve unambiguous DocumentType values from URLs without Playwright."""
        return self.url_doc_classifier.classify(links, overwrite=overwrite)

    def preclassify(
        self, source: Path | str | Sequence[str]
    ) -> list[ClassifiedLink]:
        """Run Stage 1 followed by Stage 2a (pre-browser fast path)."""
        links = self.classify_websites(source)
        return self.classify_by_url(links)

    @overload
    def classify_by_page(
        self, page: Page, target: ClassifiedLink, *, overwrite: bool = False
    ) -> ClassifiedLink: ...

    @overload
    def classify_by_page(
        self, page: Page, target: WebsiteType, *, overwrite: bool = False
    ) -> DocumentType: ...

    def classify_by_page(
        self,
        page: Page,
        target: ClassifiedLink | WebsiteType,
        *,
        overwrite: bool = False,
    ) -> ClassifiedLink | DocumentType:
        """Run Stage 2b: resolve DocumentType from an open Playwright Page."""
        return self.page_doc_classifier.classify(page, target, overwrite=overwrite)

    @staticmethod
    def to_tags(links: Iterable[ClassifiedLink]) -> list[str]:
        """Format a sequence of ClassifiedLink instances into their tagged string representations."""
        return [link.to_tag() for link in links]
