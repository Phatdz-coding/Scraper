"""Stage 2b: Playwright page-based runtime DocumentType classifier."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from html.parser import HTMLParser
from typing import overload
from urllib.parse import urlparse

from playwright.async_api import Page as AsyncPage
from playwright.sync_api import Page

from classifiers.models import ClassifiedLink, DocumentType, WebsiteType


class _TitleParser(HTMLParser):
    """Lightweight HTML parser to extract text inside the first <title> element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_title = False
        self._title_found = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title" and not self._inside_title and not self._title_found:
            self._inside_title = True

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._title_found = True
            self._inside_title = False


def _extract_page_title(page: Page) -> str:
    """Extract the <title> from a Playwright Page using page.title() or HTMLParser fallback."""
    try:
        title = page.title()
        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception:
        pass

    try:
        html = page.content()
        if isinstance(html, str) and html:
            parser = _TitleParser()
            parser.feed(html)
            return "".join(parser.title_parts).strip()
    except Exception:
        pass

    return ""


async def _extract_page_title_async(page: AsyncPage) -> str:
    """Extract the <title> from an async Playwright Page using await page.title() or HTMLParser fallback."""
    try:
        title = await page.title()
        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception:
        pass

    try:
        html = await page.content()
        if isinstance(html, str) and html:
            parser = _TitleParser()
            parser.feed(html)
            return "".join(parser.title_parts).strip()
    except Exception:
        pass

    return ""


class PageDocumentClassifierStrategy(ABC):
    """Strategy interface for resolving DocumentType from a loaded Playwright Page."""

    @property
    @abstractmethod
    def website_type(self) -> WebsiteType:
        """The WebsiteType handled by this strategy."""

    @abstractmethod
    def classify_page(self, page: Page) -> DocumentType:
        """Inspect the Playwright Page and return its resolved DocumentType."""

    async def classify_page_async(self, page: AsyncPage) -> DocumentType:
        """Inspect an async Playwright Page and return its resolved DocumentType."""
        return self.classify_page(page)  # type: ignore[arg-type]


class GoogleDrivePageDocumentStrategy(PageDocumentClassifierStrategy):
    """Playwright page-based DocumentType strategy for Google Drive pages."""

    _EXTENSION_MAP: dict[str, DocumentType] = {
        "pdf": DocumentType.PDF,
        "pptx": DocumentType.PPTX,
        "ppt": DocumentType.PPTX,
        "doc": DocumentType.MICROSOFT_WORD,
        "docx": DocumentType.MICROSOFT_WORD,
        "xlsx": DocumentType.XLSX,
        "xls": DocumentType.XLSX,
        "png": DocumentType.IMAGE,
        "jpg": DocumentType.IMAGE,
        "jpeg": DocumentType.IMAGE,
        "zip": DocumentType.ZIP_FOLDER,
    }

    _GOOGLE_DOC_LOGO_SELECTOR: str = (
        'svg[height="24"][width="24"][viewBox="3 3 18 18"]'
        '[fill="none"][xmlns="http://www.w3.org/2000/svg"]'
        ':has(> g > rect[x="3"][y="3"][width="18"][height="18"]'
        '[rx="3"][fill="#3186FF"])'
        ':has(> g > rect[x="7"][y="15"][width="6"][height="2"]'
        '[rx="1"][fill="white"])'
        ':has(> g > rect[x="7"][y="11"][width="10"][height="2"]'
        '[rx="1"][fill="white"])'
    )

    def __init__(self, logo_timeout_ms: int = 1_500) -> None:
        self._logo_timeout_ms = logo_timeout_ms

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DRIVE

    def classify_page(self, page: Page) -> DocumentType:
        page_url = page.url or ""
        if "folder" in page_url.lower():
            return DocumentType.FOLDER

        title: str | None = None
        try:
            evaluated = page.evaluate("() => window.viewerData?.config?.title")
            if isinstance(evaluated, str) and evaluated.strip():
                title = evaluated.strip()
        except Exception:
            pass

        if not title:
            try:
                html = page.content()
                if isinstance(html, str):
                    match = re.search(
                        r"""window\.viewerData.*?['"]title['"]\s*:\s*['"]([^'"]+)['"]""",
                        html,
                        re.DOTALL,
                    )
                    if match:
                        title = match.group(1)
            except Exception:
                pass

        if title and "." in title:
            ext = title.rsplit(".", 1)[-1].lower()
            if ext in self._EXTENSION_MAP:
                return self._EXTENSION_MAP[ext]

        if "/file/" in urlparse(page_url).path:
            try:
                logo = page.locator(self._GOOGLE_DOC_LOGO_SELECTOR)
                logo.first.wait_for(state="attached", timeout=self._logo_timeout_ms)
                return DocumentType.GOOGLE_DOC
            except Exception:
                pass

        return DocumentType.UNSUPPORTED_FORMAT

    async def classify_page_async(self, page: AsyncPage) -> DocumentType:
        page_url = page.url or ""
        if "folder" in page_url.lower():
            return DocumentType.FOLDER

        title: str | None = None
        try:
            evaluated = await page.evaluate("() => window.viewerData?.config?.title")
            if isinstance(evaluated, str) and evaluated.strip():
                title = evaluated.strip()
        except Exception:
            pass

        if not title:
            try:
                html = await page.content()
                if isinstance(html, str):
                    match = re.search(
                        r"""window\.viewerData.*?['"]title['"]\s*:\s*['"]([^'"]+)['"]""",
                        html,
                        re.DOTALL,
                    )
                    if match:
                        title = match.group(1)
            except Exception:
                pass

        if title and "." in title:
            ext = title.rsplit(".", 1)[-1].lower()
            if ext in self._EXTENSION_MAP:
                return self._EXTENSION_MAP[ext]

        if "/file/" in urlparse(page_url).path:
            try:
                logo = page.locator(self._GOOGLE_DOC_LOGO_SELECTOR)
                await logo.first.wait_for(state="attached", timeout=self._logo_timeout_ms)
                return DocumentType.GOOGLE_DOC
            except Exception:
                pass

        return DocumentType.UNSUPPORTED_FORMAT


class GoogleDocPageDocumentStrategy(PageDocumentClassifierStrategy):
    """Playwright page-based DocumentType strategy for Google Docs, Sheets, and Slides pages."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.GOOGLE_DOC

    def classify_page(self, page: Page) -> DocumentType:
        page_url = page.url or ""
        path = urlparse(page_url).path.lower()

        if "/spreadsheets/" in path:
            return DocumentType.XLSX
        if "/presentation/" in path:
            return DocumentType.PPTX
        if "/document/" in path:
            title = _extract_page_title(page)
            if ".docx" in title.lower():
                return DocumentType.MICROSOFT_WORD
            return DocumentType.GOOGLE_DOC

        return DocumentType.UNSUPPORTED_FORMAT

    async def classify_page_async(self, page: AsyncPage) -> DocumentType:
        page_url = page.url or ""
        path = urlparse(page_url).path.lower()

        if "/spreadsheets/" in path:
            return DocumentType.XLSX
        if "/presentation/" in path:
            return DocumentType.PPTX
        if "/document/" in path:
            title = await _extract_page_title_async(page)
            if ".docx" in title.lower():
                return DocumentType.MICROSOFT_WORD
            return DocumentType.GOOGLE_DOC

        return DocumentType.UNSUPPORTED_FORMAT


class ScribdPageDocumentStrategy(PageDocumentClassifierStrategy):
    """Playwright page-based DocumentType strategy for Scribd pages (always PDF)."""

    @property
    def website_type(self) -> WebsiteType:
        return WebsiteType.SCRIBD

    def classify_page(self, page: Page) -> DocumentType:
        return DocumentType.PDF

    async def classify_page_async(self, page: AsyncPage) -> DocumentType:
        return DocumentType.PDF


class PageDocumentClassifier:
    """Stage 2b runtime classifier that determines DocumentType from an open Playwright Page."""

    def __init__(
        self,
        strategies: Mapping[WebsiteType, PageDocumentClassifierStrategy]
        | Iterable[PageDocumentClassifierStrategy]
        | None = None,
    ) -> None:
        if strategies is None:
            default_strategies: list[PageDocumentClassifierStrategy] = [
                GoogleDrivePageDocumentStrategy(),
                GoogleDocPageDocumentStrategy(),
                ScribdPageDocumentStrategy(),
            ]
            self._registry: dict[WebsiteType, PageDocumentClassifierStrategy] = {
                s.website_type: s for s in default_strategies
            }
        elif isinstance(strategies, Mapping):
            self._registry = dict(strategies)
        else:
            self._registry = {s.website_type: s for s in strategies}

    def register_strategy(self, strategy: PageDocumentClassifierStrategy) -> None:
        """Register or replace a page document classification strategy for a WebsiteType."""
        self._registry[strategy.website_type] = strategy

    def resolve_document_type(
        self, page: Page, website_type: WebsiteType
    ) -> DocumentType:
        """Determine DocumentType for the given Playwright Page and WebsiteType."""
        strategy = self._registry.get(website_type)
        if strategy is None:
            return DocumentType.UNSUPPORTED_FORMAT
        return strategy.classify_page(page)

    async def resolve_document_type_async(
        self, page: AsyncPage, website_type: WebsiteType
    ) -> DocumentType:
        """Determine DocumentType for the given async Playwright Page and WebsiteType."""
        strategy = self._registry.get(website_type)
        if strategy is None:
            return DocumentType.UNSUPPORTED_FORMAT
        return await strategy.classify_page_async(page)

    def classify_link(
        self, page: Page, link: ClassifiedLink, *, overwrite: bool = True
    ) -> ClassifiedLink:
        """Determine DocumentType from page and update link.document_type in-place."""
        if link.document_type is not None and not overwrite:
            return link
        doc_type = self.resolve_document_type(page, link.website_type)
        link.set_document_type(doc_type)
        return link

    async def classify_link_async(
        self, page: AsyncPage, link: ClassifiedLink, *, overwrite: bool = True
    ) -> ClassifiedLink:
        """Determine DocumentType from async page and update link.document_type in-place."""
        if link.document_type is not None and not overwrite:
            return link
        doc_type = await self.resolve_document_type_async(page, link.website_type)
        link.set_document_type(doc_type)
        return link

    @overload
    def classify(
        self, page: Page, target: ClassifiedLink, *, overwrite: bool = True
    ) -> ClassifiedLink: ...

    @overload
    def classify(
        self, page: Page, target: WebsiteType, *, overwrite: bool = True
    ) -> DocumentType: ...

    def classify(
        self,
        page: Page,
        target: ClassifiedLink | WebsiteType,
        *,
        overwrite: bool = True,
    ) -> ClassifiedLink | DocumentType:
        """Classify a Playwright Page given either a ClassifiedLink or a WebsiteType."""
        if isinstance(target, ClassifiedLink):
            return self.classify_link(page, target, overwrite=overwrite)
        if isinstance(target, WebsiteType):
            return self.resolve_document_type(page, target)
        raise TypeError(
            f"Expected ClassifiedLink or WebsiteType, got {type(target).__name__}"
        )

    @overload
    async def classify_async(
        self, page: AsyncPage, target: ClassifiedLink, *, overwrite: bool = True
    ) -> ClassifiedLink: ...

    @overload
    async def classify_async(
        self, page: AsyncPage, target: WebsiteType, *, overwrite: bool = True
    ) -> DocumentType: ...

    async def classify_async(
        self,
        page: AsyncPage,
        target: ClassifiedLink | WebsiteType,
        *,
        overwrite: bool = True,
    ) -> ClassifiedLink | DocumentType:
        """Classify an async Playwright Page given either a ClassifiedLink or a WebsiteType."""
        if isinstance(target, ClassifiedLink):
            return await self.classify_link_async(page, target, overwrite=overwrite)
        if isinstance(target, WebsiteType):
            return await self.resolve_document_type_async(page, target)
        raise TypeError(
            f"Expected ClassifiedLink or WebsiteType, got {type(target).__name__}"
        )


@overload
def classify_document_by_page(
    page: Page, target: ClassifiedLink, *, overwrite: bool = True
) -> ClassifiedLink: ...


@overload
def classify_document_by_page(
    page: Page, target: WebsiteType, *, overwrite: bool = True
) -> DocumentType: ...


def classify_document_by_page(
    page: Page,
    target: ClassifiedLink | WebsiteType,
    *,
    overwrite: bool = True,
) -> ClassifiedLink | DocumentType:
    """Convenience function to run Stage 2b Playwright page-based document classification."""
    return PageDocumentClassifier().classify(page, target, overwrite=overwrite)


@overload
async def classify_document_by_page_async(
    page: AsyncPage, target: ClassifiedLink, *, overwrite: bool = True
) -> ClassifiedLink: ...


@overload
async def classify_document_by_page_async(
    page: AsyncPage, target: WebsiteType, *, overwrite: bool = True
) -> DocumentType: ...


async def classify_document_by_page_async(
    page: AsyncPage,
    target: ClassifiedLink | WebsiteType,
    *,
    overwrite: bool = True,
) -> ClassifiedLink | DocumentType:
    """Async convenience function to run Stage 2b Playwright page-based document classification."""
    return await PageDocumentClassifier().classify_async(
        page, target, overwrite=overwrite
    )
