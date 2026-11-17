"""Stage 2a: Pre-browser URL-based DocumentType classifier."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from urllib.parse import urlparse

from classifiers.models import ClassifiedLink, DocumentType, WebsiteType


class UrlDocumentClassifierStrategy(ABC):
    """Strategy interface for resolving DocumentType purely from a URL."""

    @property
    @abstractmethod
    def website_type(self) -> WebsiteType:
        """The WebsiteType handled by this strategy."""

    @abstractmethod
    def classify_url(self, url: str) -> DocumentType | None:
        """Return the DocumentType if unambiguously encoded in the URL, else None."""


class GoogleDriveUrlDocumentStrategy(UrlDocumentClassifierStrategy):
    """URL-based DocumentType strategy for Google Drive links.

    - ``/folders/`` in URL path -> ``DocumentType.FOLDER``
    - File links (``/file/d/...``, ``/open?id=...``) -> ``None`` (requires Playwright page check)
    """

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DRIVE

    def classify_url(self, url: str) -> DocumentType | None:
        path = urlparse(url).path.lower()
        segments = [seg for seg in path.split("/") if seg]
        if "/folders/" in path or "folders" in segments:
            return DocumentType.FOLDER
        return None


class GoogleDocUrlDocumentStrategy(UrlDocumentClassifierStrategy):
    """URL-based DocumentType strategy for Google Docs/Sheets/Slides links.

    - ``/spreadsheets/`` in URL path -> ``DocumentType.XLSX``
    - ``/presentation/`` in URL path -> ``DocumentType.PPTX``
    - ``/document/`` in URL path -> ``None`` (requires Playwright to distinguish GOOGLE_DOC vs MICROSOFT_WORD)
    """

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DOC

    def classify_url(self, url: str) -> DocumentType | None:
        path = urlparse(url).path.lower()
        segments = [seg for seg in path.split("/") if seg]
        if "/spreadsheets/" in path or "spreadsheets" in segments:
            return DocumentType.XLSX
        if "/presentation/" in path or "presentation" in segments:
            return DocumentType.PPTX
        if "/document/" in path or "document" in segments:
            return None
        return None


class ScribdUrlDocumentStrategy(UrlDocumentClassifierStrategy):
    """URL-based DocumentType strategy for Scribd links (always PDF)."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.SCRIBD

    def classify_url(self, url: str) -> DocumentType | None:
        return DocumentType.PDF


class UrlDocumentClassifier:
    """Stage 2a classifier that resolves DocumentType from URL structure where unambiguous."""

    def __init__(
        self,
        strategies: Mapping[WebsiteType, UrlDocumentClassifierStrategy]
        | Iterable[UrlDocumentClassifierStrategy]
        | None = None,
    ) -> None:
        if strategies is None:
            default_strategies: list[UrlDocumentClassifierStrategy] = [
                GoogleDriveUrlDocumentStrategy(),
                GoogleDocUrlDocumentStrategy(),
                ScribdUrlDocumentStrategy(),
            ]
            self._registry: dict[WebsiteType, UrlDocumentClassifierStrategy] = {
                s.website_type: s for s in default_strategies
            }
        elif isinstance(strategies, Mapping):
            self._registry = dict(strategies)
        else:
            self._registry = {s.website_type: s for s in strategies}

    def register_strategy(self, strategy: UrlDocumentClassifierStrategy) -> None:
        """Register or replace a URL document classification strategy for a WebsiteType."""
        self._registry[strategy.website_type] = strategy

    def resolve_document_type(
        self, url: str, website_type: WebsiteType
    ) -> DocumentType | None:
        """Determine DocumentType from a URL and WebsiteType without modifying state."""
        strategy = self._registry.get(website_type)
        if strategy is None:
            return None
        return strategy.classify_url(url)

    def classify_link(
        self, link: ClassifiedLink, *, overwrite: bool = False
    ) -> ClassifiedLink:
        """Update link.document_type in-place if URL classification resolves a DocumentType."""
        if link.document_type is not None and not overwrite:
            return link
        doc_type = self.resolve_document_type(link.url, link.website_type)
        if doc_type is not None:
            link.set_document_type(doc_type)
        return link

    def classify(
        self, links: Iterable[ClassifiedLink], *, overwrite: bool = False
    ) -> list[ClassifiedLink]:
        """Classify a collection of ClassifiedLink objects in-place and return them as a list."""
        result = list(links)
        for link in result:
            self.classify_link(link, overwrite=overwrite)
        return result


def classify_document_by_url(
    link: ClassifiedLink, *, overwrite: bool = False
) -> ClassifiedLink:
    """Convenience function to run Stage 2a URL-based document classification on a single link."""
    return UrlDocumentClassifier().classify_link(link, overwrite=overwrite)


def classify_documents_by_url(
    links: Iterable[ClassifiedLink], *, overwrite: bool = False
) -> list[ClassifiedLink]:
    """Convenience function to run Stage 2a URL-based document classification on a list of links."""
    return UrlDocumentClassifier().classify(links, overwrite=overwrite)
