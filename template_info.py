"""
this is a common component to track document/page info: page title, file/document name & total pages
"""

from playwright.sync_api import Browser, ConsoleMessage, Page, sync_playwright
from html.parser import HTMLParser
from enum import Enum
import re
from urllib.parse import urlparse


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title" and not self._inside_title:
            self._inside_title = True

    def handle_data(self, data: str) -> None:
        if self._inside_title:
            self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False


class DocumentType(Enum):
    PDF = 1
    DOC = 2
    PPTX = 3
    JPG = PNG = JPEG = 4
    XLSX = 5


class WebsiteType(Enum):
    GOOGLE_DRIVE = 1
    SCRIBD = 2


class Template:
    def __init__(self, page: Page, url: str | None = None):
        self.page = page
        self.website = self.__get_website_type(url=url)
        self.fileID = self.__get_fileID(url=url)

    def __get_fileID(self, url: str | None = None) -> str | None:
        """Extract a Google Drive file ID from a URL."""
        if not url:
            return None

        path_parts = [part for part in urlparse(url).path.split("/") if part]
        try:
            id_index = path_parts.index("d") + 1
        except ValueError:
            return None

        return path_parts[id_index] if id_index < len(path_parts) else None

    def __get_website_type(self, url: str | None = None) -> WebsiteType:
        """Determine the website type based on URL domain or HTML metadata in <head>.

        If *url* is provided the domain is checked first (fast path).
        Otherwise the method falls back to ``self.page.url`` and, as a last
        resort, inspects the raw HTML for characteristic metadata.
        """
        # --- fast path: explicit URL ------------------------------------------
        if url is not None:
            website = self.__match_domain(url)
            if website is not None:
                return website

        # --- fallback: page URL -----------------------------------------------
        page_url = self.page.url
        if page_url:
            website = self.__match_domain(page_url)
            if website is not None:
                return website

        # --- last resort: inspect HTML metadata --------------------------------
        html = self.page.content()

        # Google Drive: <meta property="og:url" content="https://drive.google.com/file/d/....../">
        if "drive.google.com" in html:
            return WebsiteType.GOOGLE_DRIVE

        # Scribd: <link rel="search" href="https://www.scribd.com/opensearch.xml" ...>
        if "scribd.com" in html:
            return WebsiteType.SCRIBD

        raise ValueError(
            "Unable to determine website type: page is neither Google Drive nor Scribd"
        )

    @staticmethod
    def __match_domain(url: str) -> WebsiteType | None:
        """Return the WebsiteType matching *url*'s domain, or None."""
        domain = urlparse(url).netloc.lower()
        if "drive.google.com" in domain:
            return WebsiteType.GOOGLE_DRIVE
        if "scribd.com" in domain:
            return WebsiteType.SCRIBD
        return None

    # ------------------------------------------------------------------
    # Page title
    # ------------------------------------------------------------------

    def get_page_title(self) -> str:
        """Extract the <title> text from the page's <head>.

        Uses Playwright's built-in ``page.title()`` for performance.
        """
        title = self.page.title()
        if not title:
            raise ValueError("Page does not contain a non-empty <title>")
        return title

    # ------------------------------------------------------------------
    # File name
    # ------------------------------------------------------------------

    def get_file_name(self, html_template: str | None = None) -> str:
        """Extract the document file name from the page title.

        If *html_template* is provided the title is parsed from the raw HTML
        string (using ``_TitleParser``).  Otherwise the live Playwright page
        title is used.

        Google Drive – strips the `` - Google Drive`` suffix and returns the
        bare file name (preserving the original extension).

        Scribd – takes the first pipe-delimited segment of the title (the
        document name) and appends ``.pdf``.
        """
        if html_template is not None:
            parser = _TitleParser()
            parser.feed(html_template)
            title = "".join(parser.title_parts).strip()
            if not title:
                raise ValueError(
                    "HTML template does not contain a non-empty <title>"
                )
        else:
            title = self.get_page_title()

        if self.website == WebsiteType.GOOGLE_DRIVE:
            # e.g. "document.pdf - Google Drive" → "document.pdf"
            return title.removesuffix(" - Google Drive").strip()

        if self.website == WebsiteType.SCRIBD:
            # e.g. "Doc Name | PDF | Tag1 | Tag2" → "Doc Name.pdf"
            name = title.split("|")[0].strip()
            return f"{name}.pdf"

        raise ValueError(f"Unsupported website type: {self.website}")

    # ------------------------------------------------------------------
    # Total pages
    # ------------------------------------------------------------------

    def get_total_pages(self) -> int:
        """Extract the total page count from the document viewer.

        Google Drive – reads the ``<span jsname="Dt5gRb">`` element that
        displays the total page count (works for .doc, .pptx, .pdf viewers).

        Scribd – reads the ``<div data-e2e="total-pages">`` element.
        """
        if self.website == WebsiteType.GOOGLE_DRIVE:
            return self.__get_total_pages_gd()

        if self.website == WebsiteType.SCRIBD:
            return self.__get_total_pages_scribd()

        raise ValueError(f"Unsupported website type: {self.website}")

    def __get_total_pages_gd(self) -> int:
        """Extract total pages from the Google Drive document viewer.

        The viewer toolbar contains::

            <span>/</span><span jsname="Dt5gRb">8</span>

        Uses a Playwright locator with a wait so the element is guaranteed to
        be present in the DOM before reading.
        """
        locator = self.page.locator('span[jsname="Dt5gRb"]')
        return self.__read_positive_page_count(
            locator, 'span[jsname="Dt5gRb"]', "Google Drive viewer"
        )

    def __get_total_pages_scribd(self) -> int:
        """Extract total pages from the Scribd document viewer.

        The viewer contains::

            <div data-e2e="total-pages" class="_3dwpfv">5</div>

        Uses a Playwright locator with a wait so the element is guaranteed to
        be present in the DOM before reading.
        """
        locator = self.page.locator('div[data-e2e="total-pages"]')
        return self.__read_positive_page_count(
            locator, 'div[data-e2e="total-pages"]', "Scribd viewer"
        )

    def __read_positive_page_count(
        self, locator, selector: str, viewer_name: str
    ) -> int:
        """Wait for a viewer's page-count element to contain a valid number."""
        locator.wait_for(state="attached", timeout=30_000)
        self.page.wait_for_function(
            """selector => [...document.querySelectorAll(selector)].some(
                element => /^\\s*\\d+\\s*$/.test(element.textContent || '') &&
                    Number(element.textContent) > 0
            )""",
            arg=selector,
            timeout=30_000,
        )

        for text in locator.all_text_contents():
            match = re.fullmatch(r"\s*(\d+)\s*", text)
            if match and int(match.group(1)) > 0:
                return int(match.group(1))

        raise ValueError(
            f"Could not extract a positive total page count from {viewer_name}"
        )
