from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse

from playwright.async_api import Page, async_playwright
from rich.console import Console
import typer

from classifiers import DocumentType, WebsiteType
import config
import ggdoc_scraper
import pdf_scraper
import image_scraper
import scribd_scraper as scribd_module
import zip_downloader
from template_info import Template
from tracker import (
    get_active_output_dir,
    get_active_scrape_type,
    is_live_progress_enabled,
)


def _log_console(message: str) -> None:
    if is_live_progress_enabled():
        Console().print(message)


def _is_document_type_resolved(document_type: DocumentType | None) -> bool:
    return document_type is not None and document_type not in {
        DocumentType.SHORTCUT,
        DocumentType.UNKNOWN,
    }


async def start_scraping(
    page: Page | None,
    template_info: Template,
    scribd_scraper: Callable[[Page, str], Path | Awaitable[Path]] | None = None,
    *,
    output_dir: Path | None = None,
    scrape_type: str | None = None,
    page_factory: Callable[[], Awaitable[Page]] | None = None,
    template_factory: Callable[..., Awaitable[Template]] | None = None,
) -> Path:
    """Route and execute document scraping based on template metadata and configuration.

    The scraping workflow follows a two-phase architecture:

    1. **Phase 1: Direct Download (when ``scrape_type != "s"``)**
       Attempts an HTTP direct download first without requiring or launching a browser
       page. If successful (HTTP 200), the downloaded file path is returned immediately.
       If the request fails with a 4xx client error (typically indicating export/download
       restrictions or a view-only document), execution falls through to Phase 2.
       When ``scrape_type == "s"`` (viewer scraper mode), this phase is skipped entirely.

    2. **Phase 2: In-Page / Viewer Scraping (Lazy Browser Initialization)**
       Lazily initializes or acquires the Playwright ``Page`` (via ``page`` or
       ``page_factory``). If the ``DocumentType`` is unresolved (``None``, ``UNKNOWN``,
       or ``SHORTCUT``), it is dynamically resolved from the DOM (using ``template_factory``
       or ``Template.create``). Document title is resolved from page content, then routed
       based on ``WebsiteType`` and ``DocumentType``:

       - **WebsiteType.GOOGLE_DRIVE**:
         - ``DocumentType.PDF`` / ``DocumentType.PPTX``:
           Tries ``pdf_scraper.direct_download``; falls back to ``pdf_scraper.scrape_inpage_by_api``.
         - ``DocumentType.IMAGE``:
           Tries ``pdf_scraper.direct_download``; falls back to ``image_scraper.scrape_inpage_by_api``.
         - ``DocumentType.MICROSOFT_WORD``:
           Tries ``ggdoc_scraper.direct_download`` (.docx); falls back to ``ggdoc_scraper.scrape_inpage_by_script``.
         - ``DocumentType.GOOGLE_DOC``:
           Tries ``ggdoc_scraper.direct_download`` (.docx); falls back to ``ggdoc_scraper.scrape_inpage_by_api``.
         - ``DocumentType.ZIP_FOLDER``:
           Tries ``pdf_scraper.direct_download``; falls back to ``zip_downloader.download_inpage``.

       - **WebsiteType.GOOGLE_DOC**:
         - ``DocumentType.GOOGLE_DOC``:
           Tries ``ggdoc_scraper.direct_download`` (.docx); falls back to ``ggdoc_scraper.scrape_inpage_by_api``.
         - ``DocumentType.MICROSOFT_WORD``:
           Tries ``ggdoc_scraper.direct_download`` (.docx); falls back to ``ggdoc_scraper.scrape_inpage_by_script``.

       - **WebsiteType.SCRIBD**:
         - Executes ``scribd_scraper`` if provided, otherwise defaults to
           ``scribd_scraper.scrape_inpage(active_page, document_name)``.

    Args:
        page: Optional active Playwright ``Page``. If ``None``, lazily created via ``page_factory``.
        template_info: ``Template`` metadata object containing URL, website type, document type, and file ID.
        scribd_scraper: Optional override callback for scraping Scribd documents.
            Defaults to ``scribd_scraper.scrape_inpage``.
        output_dir: Target directory where output files are stored (defaults to active output dir or config).
        scrape_type: Scraping strategy: ``"d"`` (direct download first, fallback to viewer) or ``"s"`` (viewer only).
        page_factory: Async factory callable to create a Playwright ``Page`` on demand.
        template_factory: Optional async factory callable to re-resolve ``Template`` metadata from DOM.

    Returns:
        Path: Path to the downloaded or scraped output file.

    Raises:
        ValueError: If file ID is missing for Google Drive or Google Docs.
        NotImplementedError: If the website or document type is unsupported.
        RuntimeError: If direct download fails with an unexpected HTTP status, or if browser page
            creation fails.
    """
    if (
        template_info.fileID is None
        and template_info.website_type != WebsiteType.SCRIBD
    ):
        raise ValueError("A file ID is required for Google Drive and Google Docs")

    active_output_dir = output_dir or get_active_output_dir()
    active_scrape_type = (
        scrape_type or get_active_scrape_type() or config.SCRAPE_TYPE
    ).lower()

    file_id = template_info.fileID
    direct_attempted = False

    async def _ensure_page() -> Page:
        nonlocal page
        if page is None:
            if page_factory is None:
                raise RuntimeError("A page or page_factory is required for scraping")
            page = await page_factory()
        template_info.page = page
        return page

    async def _resolve_document_name() -> str:
        active_page = await _ensure_page()
        return await template_info.get_file_name(await active_page.content())

    async def _invoke_inpage_scraper(
        scraper_fn: Callable[..., Path | Awaitable[Path]],
        target_page: Page,
        target_name: str,
    ) -> Path:
        if active_output_dir is not None:
            try:
                sig = inspect.signature(scraper_fn)
                if "output_dir" in sig.parameters:
                    res = scraper_fn(
                        target_page, target_name, output_dir=active_output_dir
                    )
                else:
                    res = scraper_fn(target_page, target_name)
            except (ValueError, TypeError):
                res = scraper_fn(target_page, target_name)
        else:
            res = scraper_fn(target_page, target_name)

        if inspect.isawaitable(res):
            return await res
        return res

    if template_info.website_type == WebsiteType.SCRIBD:
        active_page = await _ensure_page()
        document_name = await _resolve_document_name()
        active_scribd_scraper = scribd_scraper or scribd_module.scrape_inpage
        return await _invoke_inpage_scraper(
            active_scribd_scraper, active_page, document_name
        )

    if file_id is None:
        raise ValueError("A file ID is required for Google Drive and Google Docs")

    async def try_direct_download(
        downloader: Callable[
            ..., tuple[Path | None, int] | Awaitable[tuple[Path | None, int]]
        ],
        name: str,
        *,
        prefer_header_name: bool = False,
    ) -> Path | None:
        nonlocal direct_attempted
        if active_scrape_type == "s" or direct_attempted:
            return None
        direct_attempted = True

        async def _invoke_downloader() -> tuple[Path | None, int]:
            kwargs: dict[str, object] = {}
            try:
                params = inspect.signature(downloader).parameters
                if active_output_dir is not None and "output_dir" in params:
                    kwargs["output_dir"] = active_output_dir
                if prefer_header_name and "prefer_header_name" in params:
                    kwargs["prefer_header_name"] = True
            except (ValueError, TypeError):
                pass
            res = downloader(file_id, name, **kwargs)

            if inspect.isawaitable(res):
                return await res
            return res

        try:
            if is_live_progress_enabled():
                with Console().status(
                    f"[bold green]Downloading {name} directly [/bold green]",
                    spinner="dots",
                ):
                    output_path, status = await _invoke_downloader()
            else:
                output_path, status = await _invoke_downloader()
        except HTTPError as error:
            if 400 <= error.code < 500:
                _log_console("[bold red]Failed to download directly[/bold red]")
                return None
            _log_console("[bold red]Failed to download directly[/bold red]")
            raise

        if status == 200:
            _log_console("[bold green]Download successfully[/bold green]")
            return output_path
        if 400 <= status < 500:
            _log_console("[bold red]Failed to download directly[/bold red]")
            return None
        _log_console("[bold red]Failed to download directly[/bold red]")
        raise RuntimeError(f"Direct download failed with HTTP {status}")

    async def _call_inpage_scraper(
        scraper_fn: Callable[..., Path | Awaitable[Path]],
    ) -> Path:
        assert page is not None
        return await _invoke_inpage_scraper(scraper_fn, page, document_name)

    # ---- Phase 1 (-t d): HTTP direct download first, no browser needed ----
    if active_scrape_type != "s":
        known_name: str | None = None
        if page is not None:
            known_name = await template_info.get_file_name(await page.content())
        doc_type = template_info.document_type
        use_ggdoc = False
        direct_candidate = False
        if template_info.website_type == WebsiteType.GOOGLE_DRIVE:
            direct_candidate = True
            use_ggdoc = doc_type in (
                DocumentType.GOOGLE_DOC,
                DocumentType.MICROSOFT_WORD,
            )
        elif template_info.website_type == WebsiteType.GOOGLE_DOC:
            direct_candidate = doc_type not in (DocumentType.XLSX, DocumentType.PPTX)
            use_ggdoc = True
        if direct_candidate:
            if use_ggdoc:
                base = known_name or file_id
                direct_name = base if base.lower().endswith(".docx") else f"{base}.docx"
                direct_path = await try_direct_download(
                    ggdoc_scraper.direct_download,
                    direct_name,
                    prefer_header_name=known_name is None,
                )
            else:
                direct_path = await try_direct_download(
                    pdf_scraper.direct_download,
                    known_name or file_id,
                    prefer_header_name=known_name is None,
                )
            if direct_path is not None:
                return direct_path

    # ---- Phase 2: lazily open the page and resolve the DocumentType if needed ----
    page = await _ensure_page()
    if not _is_document_type_resolved(template_info.document_type):
        if template_factory is not None:
            resolved = await template_factory(page, template_info.url)
        else:
            resolved = await Template.create(
                page,
                template_info.url,
                website_type=template_info.website_type,
            )
        template_info.document_type = resolved.document_type
        if template_info.fileID is None:
            template_info.fileID = resolved.fileID
        template_info.page = page
    document_name = await _resolve_document_name()

    if template_info.website_type == WebsiteType.GOOGLE_DRIVE:
        if template_info.document_type in (DocumentType.PDF, DocumentType.PPTX):
            direct_path = await try_direct_download(
                pdf_scraper.direct_download, document_name
            )
            if direct_path is not None:
                return direct_path
            return await _call_inpage_scraper(pdf_scraper.scrape_inpage_by_api)

        if template_info.document_type == DocumentType.IMAGE:
            direct_path = await try_direct_download(
                pdf_scraper.direct_download, document_name
            )
            if direct_path is not None:
                return direct_path
            return await _call_inpage_scraper(image_scraper.scrape_inpage_by_api)

        if template_info.document_type == DocumentType.MICROSOFT_WORD:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = await try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return await _call_inpage_scraper(ggdoc_scraper.scrape_inpage_by_script)

        if template_info.document_type == DocumentType.GOOGLE_DOC:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = await try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return await _call_inpage_scraper(ggdoc_scraper.scrape_inpage_by_api)

        if template_info.document_type == DocumentType.ZIP_FOLDER:
            direct_path = await try_direct_download(
                pdf_scraper.direct_download, document_name
            )
            if direct_path is not None:
                return direct_path
            return await _call_inpage_scraper(zip_downloader.download_inpage)

        doc_type_name = (
            template_info.document_type.name
            if template_info.document_type is not None
            else "UNKNOWN"
        )
        raise NotImplementedError(
            f"Google Drive scraping is not implemented for {doc_type_name}"
        )

    if template_info.website_type == WebsiteType.GOOGLE_DOC:
        if template_info.document_type == DocumentType.GOOGLE_DOC:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = await try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return await _call_inpage_scraper(ggdoc_scraper.scrape_inpage_by_api)

        if template_info.document_type == DocumentType.MICROSOFT_WORD:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = await try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return await _call_inpage_scraper(ggdoc_scraper.scrape_inpage_by_script)

        doc_type_name = (
            template_info.document_type.name
            if template_info.document_type is not None
            else "UNKNOWN"
        )
        raise NotImplementedError(
            f"Google Docs scraping is not implemented for {doc_type_name}"
        )

    raise NotImplementedError(
        f"Scraping is not implemented for {template_info.website_type.name}"
    )


async def _main(url: str) -> Path:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL")
    destination = Path(config.OUTPUT_DIR)
    destination.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        console = Console()
        with console.status("[green]Initiating browser [/green]", spinner="dots"):
            browser = await playwright.chromium.launch()
        console.print("[bold green]Browser initialized[/bold green]")
        try:
            with console.status("[green]Opening browser page [/green]", spinner="dots"):
                page = await browser.new_page(accept_downloads=True)
            console.print("[bold green]Browser page opened[/bold green]")
            source_url = url
            with console.status(
                f"[green]Opening URL:[/green] [bold white]{source_url}[/bold white]",
                spinner="dots",
            ):
                await page.goto(source_url, wait_until="domcontentloaded")
            console.print("[bold green]URL opened[/bold green]")
            template_info = await Template.create(page, source_url)
            webtype = await template_info.get_website_type(source_url)
            if (
                webtype == WebsiteType.GOOGLE_DOC
                and "spreadsheet" not in source_url.lower()
            ):
                target_url = ggdoc_scraper.switch_url_to_AllTabsView(source_url)
                with console.status(
                    f"[green]Opening Google Docs view: {target_url} [/green]",
                    spinner="dots",
                ):
                    await page.goto(target_url, wait_until="domcontentloaded")
                console.print("[bold green]Google Docs view opened[/bold green]")
                template_info = await Template.create(page, target_url)

            file_name = await template_info.get_file_name()
            typer.secho(f"Source URL: {source_url}", fg=typer.colors.BRIGHT_WHITE)
            typer.secho(f"File name: {file_name}", fg=typer.colors.BRIGHT_WHITE)

            return await start_scraping(page, template_info)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(_main())
