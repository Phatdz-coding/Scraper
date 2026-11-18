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
from template_info import Template
from tracker import (
    get_active_output_dir,
    get_active_scrape_type,
    is_live_progress_enabled,
)

URL = "https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view"
# URL = "https://drive.google.com/file/d/1EJoTKt46BQEUCcPzq6Lr8qeReSwVnmdS/view"
# URL = "https://docs.google.com/document/d/1ooiZX0ZdLv9TtSYz2jWddFpjvEDO3BjU7H6NHAJ_wZM/edit?usp=drive_link"
# URL = "https://docs.google.com/document/d/11yeLYINhjmqYRksCBH9S8p8W52nHw8UQ/edit"
# URL = "https://www.scribd.com/document/524804738/Math-With-Bad-Drawings?_gl=1*134viz*_up*MQ..*_ga*MTc4MzIwMzM4LjE3OTQ0MjA4MDQ.*_ga_Z4ZC50DED6*czE3OTQ0MjA4MDMkbzEkZzEkdDE3OTQ0MjA4MDMkajYwJGwwJGgw*_ga_8KZ8BV0P5W*czE3OTQ0MjA4MDMkbzEkZzEkdDE3OTQ0MjA4MDMkajYwJGwwJGgw"
# URL = "https://docs.google.com/spreadsheets/d/1v6I-Xo8MW7ArQe41mYa23jGZSQ3mJwDIXq3UMIdhWW8/edit?usp=drive_link"


def _log_console(message: str) -> None:
    if is_live_progress_enabled():
        Console().print(message)


async def start_scraping(
    page: Page,
    template_info: Template,
    scribd_scraper: Callable[[Page, str], Path | Awaitable[Path]] | None = None,
    *,
    output_dir: Path | None = None,
    scrape_type: str | None = None,
) -> Path:
    """
    base on the template's info and the config.py
    the scraping initializations are as follow:

    1/ if the website_type is WebsiteType.GOOGLE_DRIVE
    and the document_type is DocumentType.PDF
    then call the function direct_download first
    if the function return status code != 200, typically 400 or 404 --> it is the view-only document
    then call the function scrape_inpage_by_api()

    2/
    2.1/if the website type is WebsiteType.GOOGLE_DOC
    and the document_type is DocumentType.GOOGLE_DOC
    then try to call the direct_download() function
    if the function return status_code != 200, typically 400 or 404 --> it is the view-only document
    then call the function scrape_inpage_by_api(), open the new url in the current page and pass it to the function
    2.2/ if the website type is WebsiteType.GOOGLE_DOC
    and the document_type is DocumentType.MICROSOFT_WORD
    then try to call the direct_download() function
    if the function return status_code != 200, typically 400 or 404 --> it is the view-only document
    keep the current page and call scrape_inpage_by_script()

    3/ if the website type is WebsiteType.SCRIBD
    design a pseudocode
    because i don't have the unique scraper for this type of website

    If config.SCRAPE_TYPE is "s", skip direct downloads and use the viewer
    scraper. Otherwise, try direct download before falling back to the viewer.
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
    page_html = await page.content()
    document_name = await template_info.get_file_name(page_html)

    if template_info.website_type == WebsiteType.SCRIBD:
        if scribd_scraper is None:
            raise NotImplementedError(
                "Scribd scraping is not implemented; provide scribd_scraper"
            )
        result = scribd_scraper(page, document_name)
        if inspect.isawaitable(result):
            return await result
        return result

    if file_id is None:
        raise ValueError("A file ID is required for Google Drive and Google Docs")

    async def try_direct_download(
        downloader: Callable[..., tuple[Path | None, int] | Awaitable[tuple[Path | None, int]]],
        name: str,
    ) -> Path | None:
        if active_scrape_type == "s":
            return None

        async def _invoke_downloader() -> tuple[Path | None, int]:
            if active_output_dir is not None:
                try:
                    sig = inspect.signature(downloader)
                    if "output_dir" in sig.parameters:
                        res = downloader(file_id, name, output_dir=active_output_dir)
                    else:
                        res = downloader(file_id, name)
                except (ValueError, TypeError):
                    res = downloader(file_id, name)
            else:
                res = downloader(file_id, name)

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
        if active_output_dir is not None:
            try:
                sig = inspect.signature(scraper_fn)
                if "output_dir" in sig.parameters:
                    res = scraper_fn(page, document_name, output_dir=active_output_dir)
                else:
                    res = scraper_fn(page, document_name)
            except (ValueError, TypeError):
                res = scraper_fn(page, document_name)
        else:
            res = scraper_fn(page, document_name)

        if inspect.isawaitable(res):
            return await res
        return res

    if template_info.website_type == WebsiteType.GOOGLE_DRIVE:
        if template_info.document_type == DocumentType.PDF:
            direct_path = await try_direct_download(
                pdf_scraper.direct_download, document_name
            )
            if direct_path is not None:
                return direct_path
            return await _call_inpage_scraper(pdf_scraper.scrape_inpage_by_api)

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


async def _main(url: str = URL) -> Path:
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
