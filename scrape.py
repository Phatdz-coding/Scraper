import pdf_scraper
import ggdoc_scraper
import re
import typer
from classifiers import DocumentType, WebsiteType
from template_info import Template
import config
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from collections.abc import Callable
from rich.console import Console

# from pdf_scraper import _download_pdf
from playwright.sync_api import sync_playwright, Page


URL = "https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view"
# URL = "https://drive.google.com/file/d/1EJoTKt46BQEUCcPzq6Lr8qeReSwVnmdS/view"
# URL = "https://docs.google.com/document/d/1ooiZX0ZdLv9TtSYz2jWddFpjvEDO3BjU7H6NHAJ_wZM/edit?usp=drive_link"
# URL = "https://docs.google.com/document/d/11yeLYINhjmqYRksCBH9S8p8W52nHw8UQ/edit"
# URL = "https://www.scribd.com/document/524804738/Math-With-Bad-Drawings?_gl=1*134viz*_up*MQ..*_ga*MTc4MzIwMzM4LjE3OTQ0MjA4MDQ.*_ga_Z4ZC50DED6*czE3OTQ0MjA4MDMkbzEkZzEkdDE3OTQ0MjA4MDMkajYwJGwwJGgw*_ga_8KZ8BV0P5W*czE3OTQ0MjA4MDMkbzEkZzEkdDE3OTQ0MjA4MDMkajYwJGwwJGgw"
# URL = "https://docs.google.com/spreadsheets/d/1v6I-Xo8MW7ArQe41mYa23jGZSQ3mJwDIXq3UMIdhWW8/edit?usp=drive_link"

# open browser
parsed_url = urlparse(URL)
if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
    raise ValueError("URL must be an absolute HTTP or HTTPS URL")
destination = Path(config.OUTPUT_DIR)
destination.mkdir(parents=True, exist_ok=True)


def start_scraping(
    page: Page,
    template_info: Template,
    scribd_scraper: Callable[[Page, str], Path] | None = None,
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

    file_id = template_info.fileID
    document_name = template_info.get_file_name(page.content())

    if template_info.website_type == WebsiteType.SCRIBD:
        if scribd_scraper is None:
            raise NotImplementedError(
                "Scribd scraping is not implemented; provide scribd_scraper"
            )
        return scribd_scraper(page, document_name)

    if file_id is None:
        raise ValueError("A file ID is required for Google Drive and Google Docs")

    def try_direct_download(
        downloader: Callable[[str, str], tuple[Path | None, int]],
        name: str,
    ) -> Path | None:
        if config.SCRAPE_TYPE.lower() == "s":
            return None

        try:
            with Console().status(
                f"[bold green]Downloading {name} directly [/bold green]",
                spinner="dots",
            ):
                output_path, status = downloader(file_id, name)
        except HTTPError as error:
            if 400 <= error.code < 500:
                Console().print("[bold red]Failed to download directly[/bold red]")
                return None
            Console().print("[bold red]Failed to download directly[/bold red]")
            raise

        if status == 200:
            Console().print("[bold green]Download successfully[/bold green]")
            return output_path
        if 400 <= status < 500:
            Console().print("[bold red]Failed to download directly[/bold red]")
            return None
        Console().print("[bold red]Failed to download directly[/bold red]")
        raise RuntimeError(f"Direct download failed with HTTP {status}")

    if template_info.website_type == WebsiteType.GOOGLE_DRIVE:
        if template_info.document_type == DocumentType.PDF:
            direct_path = try_direct_download(
                pdf_scraper.direct_download, document_name
            )
            if direct_path is not None:
                return direct_path
            return pdf_scraper.scrape_inpage_by_api(page, document_name)

        if template_info.document_type == DocumentType.MICROSOFT_WORD:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return ggdoc_scraper.scrape_inpage_by_script(page, document_name)

        if template_info.document_type == DocumentType.GOOGLE_DOC:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return ggdoc_scraper.scrape_inpage_by_api(page, document_name)

        raise NotImplementedError(
            f"Google Drive scraping is not implemented for {template_info.document_type.name}"
        )

    if template_info.website_type == WebsiteType.GOOGLE_DOC:
        if template_info.document_type == DocumentType.GOOGLE_DOC:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return ggdoc_scraper.scrape_inpage_by_api(page, document_name)

        if template_info.document_type == DocumentType.MICROSOFT_WORD:
            direct_name = (
                document_name
                if document_name.lower().endswith(".docx")
                else f"{document_name}.docx"
            )
            direct_path = try_direct_download(
                ggdoc_scraper.direct_download, direct_name
            )
            if direct_path is not None:
                return direct_path

            return ggdoc_scraper.scrape_inpage_by_script(page, document_name)

        raise NotImplementedError(
            f"Google Docs scraping is not implemented for {template_info.document_type.name}"
        )

    raise NotImplementedError(
        f"Scraping is not implemented for {template_info.website_type.name}"
    )


# open url in new page
with sync_playwright() as playwright:
    console = Console()
    with console.status("[green]Initiating browser [/green]", spinner="dots"):
        browser = playwright.chromium.launch()
    console.print("[bold green]Browser initialized[/bold green]")
    try:
        with console.status("[green]Opening browser page [/green]", spinner="dots"):
            page = browser.new_page(accept_downloads=True)
        console.print("[bold green]Browser page opened[/bold green]")
        source_url = URL
        with console.status(
            f"[green]Opening URL:[/green] [bold white]{source_url}[/bold white]",
            spinner="dots",
        ):
            page.goto(source_url, wait_until="domcontentloaded")
        console.print("[bold green]URL opened[/bold green]")
        template_info = Template(page, source_url)
        webtype = template_info.get_website_type(source_url)
        if (
            webtype == WebsiteType.GOOGLE_DOC
            and "spreadsheet" not in source_url.lower()
        ):
            URL = ggdoc_scraper.switch_url_to_AllTabsView(source_url)
            with console.status(
                f"[green]Opening Google Docs view: {URL} [/green]",
                spinner="dots",
            ):
                page.goto(URL, wait_until="domcontentloaded")
            console.print("[bold green]Google Docs view opened[/bold green]")
            template_info = Template(page, URL)

        file_name = template_info.get_file_name()
        typer.secho(f"Source URL: {source_url}", fg=typer.colors.BRIGHT_WHITE)
        typer.secho(f"File name: {file_name}", fg=typer.colors.BRIGHT_WHITE)

        # start to scrape
        start_scraping(page, template_info)

    finally:
        browser.close()
