"""Async Batch Scraper Module.

Coordinates multi-stage link filtering/pre-classification (Phase 1),
Google Drive folder expansion (Phase 2), and bounded concurrent document
scraping sharing a single async Chromium browser (Phase 3).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from typing import Protocol, runtime_checkable

from playwright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
    async_playwright,
)
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
import typer

from classifiers import (
    ClassifiedLink,
    DocumentType,
    InputParser,
    LinkClassificationPipeline,
    LinkInputParser,
    WebsiteType,
)
import config
import folder_scraper
import ggdoc_scraper
from simple_scrape import start_scraping
from template_info import Template
from tracker import (
    OutputFilenameResolver,
    suppress_live_progress,
    task_execution_context,
)

console = Console()


# ---------------------------------------------------------------------------
# Data Models (DTOs)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BatchConfig:
    """Immutable configuration for a batch scraping run."""

    output_dir: Path = field(default_factory=lambda: Path(config.OUTPUT_DIR))
    concurrency: int = field(
        default_factory=lambda: int(getattr(config, "CONCURRENT_SCRAPER", 5))
    )
    scrape_type: str = field(
        default_factory=lambda: str(getattr(config, "SCRAPE_TYPE", "d"))
    )
    headless: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.output_dir, Path):
            object.__setattr__(self, "output_dir", Path(self.output_dir))
        if int(self.concurrency) < 1:
            raise ValueError("concurrency must be at least 1")
        object.__setattr__(self, "concurrency", int(self.concurrency))

        normalized_type = str(self.scrape_type).strip().lower()
        if normalized_type not in {"d", "s"}:
            raise ValueError(
                f"Invalid scrape_type {self.scrape_type!r}. Must be 'd' or 's'."
            )
        object.__setattr__(self, "scrape_type", normalized_type)

    @classmethod
    def from_config(
        cls,
        *,
        output_dir: Path | str | None = None,
        concurrency: int | None = None,
        scrape_type: str | None = None,
        headless: bool = True,
    ) -> BatchConfig:
        """Construct a BatchConfig with optional overrides on top of config.py defaults."""
        return cls(
            output_dir=Path(output_dir)
            if output_dir is not None
            else Path(config.OUTPUT_DIR),
            concurrency=concurrency
            if concurrency is not None
            else int(getattr(config, "CONCURRENT_SCRAPER", 5)),
            scrape_type=scrape_type
            if scrape_type is not None
            else str(getattr(config, "SCRAPE_TYPE", "d")),
            headless=headless,
        )


@dataclass(frozen=True)
class ScrapeItemResult:
    """Outcome of scraping a single ClassifiedLink."""

    link: ClassifiedLink
    output_path: Path | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return True when the link was scraped without error and produced an output path."""
        return self.error is None and self.output_path is not None


@dataclass(frozen=True)
class BatchScrapeResult:
    """Aggregate result of a 3-phase batch scrape."""

    raw_urls: list[str]
    classified_links: list[ClassifiedLink]
    results: list[ScrapeItemResult]

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if not item.succeeded)


# ---------------------------------------------------------------------------
# Ports / Abstractions (ISP & DIP)
# ---------------------------------------------------------------------------
@runtime_checkable
class LinkFilterer(Protocol):
    """Phase 1 port: parses raw input and pre-classifies links without a browser."""

    def filter_and_classify(
        self, source: Path | str | Sequence[str]
    ) -> tuple[list[str], list[ClassifiedLink]]: ...


@runtime_checkable
class FolderExpander(Protocol):
    """Phase 2 port: expands DocumentType.FOLDER links into contained file links."""

    async def expand(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None = None,
    ) -> list[ClassifiedLink]: ...


@runtime_checkable
class LinkScraper(Protocol):
    """Phase 3 port: scrapes a single ClassifiedLink using the shared AsyncBrowser."""

    async def scrape_one(
        self,
        link: ClassifiedLink,
        browser: AsyncBrowser,
        config: BatchConfig,
    ) -> ScrapeItemResult: ...


@runtime_checkable
class BatchReporter(Protocol):
    """Port for reporting batch progress and final summary."""

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None: ...

    def on_phase2_start(self, folder_count: int) -> None: ...

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None: ...

    def on_phase3_start(self, total: int) -> None: ...

    def on_item_start(self, link: ClassifiedLink) -> None: ...

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None: ...

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None: ...


# ---------------------------------------------------------------------------
# Concrete Implementations (SRP)
# ---------------------------------------------------------------------------
class ClassifierLinkFilterer:
    """Phase 1 adapter: parses URLs with LinkInputParser and pre-classifies via LinkClassificationPipeline."""

    def __init__(
        self,
        input_parser: InputParser | None = None,
        pipeline: LinkClassificationPipeline | None = None,
    ) -> None:
        self._parser: InputParser = input_parser or LinkInputParser()
        self._pipeline: LinkClassificationPipeline = (
            pipeline or LinkClassificationPipeline()
        )

    def filter_and_classify(
        self, source: Path | str | Sequence[str]
    ) -> tuple[list[str], list[ClassifiedLink]]:
        raw_urls = self._parser.parse(source)
        if not raw_urls:
            return [], []
        classified_links = self._pipeline.preclassify(raw_urls)
        return raw_urls, classified_links


class DriveFolderExpander:
    """Phase 2 adapter: seeds all folder links into DriveFolderScraper.queue and expands them."""

    def __init__(
        self,
        folder_scraper_fn: Callable[..., Awaitable[list[ClassifiedLink]]] | None = None,
    ) -> None:
        self._folder_scraper_fn = folder_scraper_fn

    async def expand(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None = None,
    ) -> list[ClassifiedLink]:
        non_folder_links: list[ClassifiedLink] = []
        folder_urls: list[str] = []

        for link in links:
            if link.document_type == DocumentType.FOLDER:
                folder_urls.append(link.url)
            else:
                non_folder_links.append(link)

        discovered_links: list[ClassifiedLink] = []
        if folder_urls:
            active_fn = (
                self._folder_scraper_fn or folder_scraper.scrape_many_classified
            )
            try:
                sig = inspect.signature(active_fn)
                if "browser" in sig.parameters:
                    discovered_links = await active_fn(
                        folder_urls, browser=browser
                    )
                else:
                    discovered_links = await active_fn(folder_urls)
            except Exception as exc:
                folder_scraper.log_error(
                    f"Error expanding folders {folder_urls}: {exc}. Continuing with non-folder links."
                )
                discovered_links = []

        merged: list[ClassifiedLink] = []
        seen_by_url: dict[str, ClassifiedLink] = {}
        for link in [*non_folder_links, *discovered_links]:
            existing = seen_by_url.get(link.url)
            if existing is None:
                seen_by_url[link.url] = link
                merged.append(link)
            elif existing.document_type is None and link.document_type is not None:
                existing.set_document_type(link.document_type)

        return merged


async def _close_resource_quietly(resource: object | None) -> None:
    if resource is None:
        return
    close_fn = getattr(resource, "close", None)
    if close_fn is None:
        return
    try:
        res = close_fn()
        if inspect.isawaitable(res):
            await res
    except Exception:
        pass


class PlaywrightAsyncLinkScraper:
    """Phase 3 adapter: scrapes a single link in an isolated BrowserContext of the shared Browser."""

    def __init__(
        self,
        *,
        template_factory: Callable[..., Awaitable[Template]] | None = None,
        scrape_fn: Callable[..., Awaitable[Path] | Path] | None = None,
        scribd_scraper: Callable[[AsyncPage, str], Awaitable[Path] | Path] | None = None,
        filename_resolver: OutputFilenameResolver | None = None,
    ) -> None:
        self._template_factory = template_factory
        self._scrape_fn = scrape_fn
        self._scribd_scraper = scribd_scraper
        self._filename_resolver = filename_resolver or OutputFilenameResolver()

    @property
    def filename_resolver(self) -> OutputFilenameResolver:
        return self._filename_resolver

    @staticmethod
    def _resolve_navigation_url(link: ClassifiedLink) -> str:
        """Convert Google Docs document URLs to Drive AllTabsView URLs just like scrape.py."""
        url = link.url
        lowered = url.lower()
        if (
            link.website_type == WebsiteType.GOOGLE_DOC
            and "spreadsheet" not in lowered
            and "presentation" not in lowered
        ):
            try:
                return ggdoc_scraper.switch_url_to_AllTabsView(url)
            except ValueError:
                return url
        return url

    @staticmethod
    async def _open_isolated_page(
        browser: AsyncBrowser,
    ) -> tuple[AsyncBrowserContext | None, AsyncPage]:
        """Open an isolated BrowserContext (accept_downloads=True) and Page on the shared Browser."""
        new_context = getattr(browser, "new_context", None)
        if new_context is not None:
            context = await new_context(accept_downloads=True)
            try:
                page = await context.new_page()
                return context, page
            except Exception:
                await _close_resource_quietly(context)
                raise

        page = await browser.new_page(accept_downloads=True)
        return None, page

    async def scrape_one(
        self,
        link: ClassifiedLink,
        browser: AsyncBrowser,
        config: BatchConfig,
    ) -> ScrapeItemResult:
        context: AsyncBrowserContext | None = None
        page: AsyncPage | None = None
        try:
            config.output_dir.mkdir(parents=True, exist_ok=True)
            context, page = await self._open_isolated_page(browser)
            target_url = self._resolve_navigation_url(link)
            await page.goto(target_url, wait_until="domcontentloaded")

            active_template_factory = self._template_factory or Template.create
            template_info = await active_template_factory(page, target_url)
            if getattr(template_info, "document_type", None) is not None:
                link.set_document_type(template_info.document_type)

            active_scrape_fn = self._scrape_fn or start_scraping
            with task_execution_context(
                output_dir=config.output_dir,
                scrape_type=config.scrape_type,
                filename_resolver=self._filename_resolver,
                enable_live=False,
            ) as task_ctx:
                if self._scribd_scraper is not None:
                    raw_res = active_scrape_fn(
                        page, template_info, self._scribd_scraper
                    )
                else:
                    raw_res = active_scrape_fn(page, template_info)

                output_path = (
                    await raw_res if inspect.isawaitable(raw_res) else raw_res
                )

                # If a custom/mocked scrape_fn returned a path without reserving via
                # the task's OutputFilenameResolver, deduplicate and reserve it now.
                if output_path is not None and task_ctx.reserved_path is None:
                    output_path = Path(output_path)
                    target_dir = (
                        output_path.parent
                        if str(output_path.parent) not in {"", "."}
                        else config.output_dir
                    )
                    output_path = await self._filename_resolver.reserve(
                        target_dir,
                        output_path.name,
                        file_id=getattr(template_info, "fileID", None),
                    )

            return ScrapeItemResult(link=link, output_path=output_path, error=None)
        except Exception as exc:
            error_msg = str(exc).strip() or type(exc).__name__
            return ScrapeItemResult(link=link, output_path=None, error=error_msg)
        finally:
            await _close_resource_quietly(page)
            await _close_resource_quietly(context)


# ---------------------------------------------------------------------------
# Batch Progress UI (Rich)
# ---------------------------------------------------------------------------
class NullBatchReporter:
    """No-op reporter for silent programmatic or unit-test runs."""

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None:
        pass

    def on_phase2_start(self, folder_count: int) -> None:
        pass

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None:
        pass

    def on_phase3_start(self, total: int) -> None:
        pass

    def on_item_start(self, link: ClassifiedLink) -> None:
        pass

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None:
        pass

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None:
        pass


class RichBatchReporter:
    """Rich batch-level progress bar and final OK / FAIL summary table."""

    def __init__(
        self,
        rich_console: Console | None = None,
        *,
        enable_live_bar: bool = True,
    ) -> None:
        self.console = rich_console or console
        self._enable_live_bar = enable_live_bar
        self._progress: Progress | None = None
        self._task_id: int | None = None
        self._active_urls: list[str] = []
        self.summary_printed: bool = False

    def _format_active(self) -> str:
        if not self._active_urls:
            return "Idle"
        latest = self._active_urls[-1]
        short = latest if len(latest) <= 48 else f"{latest[:45]}..."
        if len(self._active_urls) == 1:
            return f"Active: {short}"
        return f"Active ({len(self._active_urls)}): {short}"

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None:
        self.console.print(
            f"[bold cyan][Phase 1][/bold cyan] Parsed {len(raw_urls)} raw URL(s) -> "
            f"{len(classified_links)} valid classified link(s)."
        )

    def on_phase2_start(self, folder_count: int) -> None:
        if folder_count > 0:
            self.console.print(
                f"[bold cyan][Phase 2][/bold cyan] Expanding {folder_count} Google Drive folder(s)..."
            )

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None:
        self.console.print(
            f"[bold cyan][Phase 2][/bold cyan] Ready to scrape {len(expanded_links)} unique document link(s)."
        )

    def on_phase3_start(self, total: int) -> None:
        self._active_urls.clear()
        if self._enable_live_bar and total > 0:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TextColumn("[dim]{task.fields[active_info]}"),
                console=self.console,
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task(
                "Scraping batch", total=total, active_info="Starting..."
            )

    def on_item_start(self, link: ClassifiedLink) -> None:
        self._active_urls.append(link.url)
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                active_info=self._format_active(),
            )

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None:
        if result.link.url in self._active_urls:
            self._active_urls.remove(result.link.url)
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                completed=completed,
                active_info=self._format_active(),
            )

    def _stop_progress(self) -> None:
        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:
                pass
            self._progress = None
            self._task_id = None

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None:
        self._stop_progress()
        self.print_summary(batch_result)
        self.summary_printed = True

    def print_summary(self, batch_result: BatchScrapeResult) -> None:
        table = Table(title="Batch Scrape Summary", show_lines=False)
        table.add_column("#", justify="right", style="dim")
        table.add_column("Status", justify="center")
        table.add_column("Website / Type", style="cyan")
        table.add_column("URL", overflow="fold")
        table.add_column("Output / Error", overflow="fold")

        for idx, item in enumerate(batch_result.results, start=1):
            status_badge = (
                "[bold green]OK[/bold green]"
                if item.succeeded
                else "[bold red]FAIL[/bold red]"
            )
            doc_type = (
                item.link.document_type.name
                if item.link.document_type is not None
                else "UNKNOWN"
            )
            type_label = f"{item.link.website_type.name}.{doc_type}"
            detail = (
                str(item.output_path)
                if item.succeeded and item.output_path is not None
                else (item.error or "Unknown error")
            )
            table.add_row(str(idx), status_badge, type_label, item.link.url, detail)

        self.console.print(table)
        self.console.print(
            f"[bold]Total:[/bold] {len(batch_result.results)} | "
            f"[bold green]OK:[/bold green] {batch_result.succeeded_count} | "
            f"[bold red]FAIL:[/bold red] {batch_result.failed_count}"
        )


# ---------------------------------------------------------------------------
# Shared Browser Factory
# ---------------------------------------------------------------------------
@asynccontextmanager
async def open_shared_browser(
    headless: bool = True,
) -> AsyncGenerator[AsyncBrowser, None]:
    """Launch a single shared Chromium Browser instance and close it on exit."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            yield browser
        finally:
            await _close_resource_quietly(browser)


# ---------------------------------------------------------------------------
# Orchestrator (BatchScraper)
# ---------------------------------------------------------------------------
class BatchScraper:
    """Orchestrates Phase 1 (filter), Phase 2 (expand folders), and Phase 3 (concurrent scrape)."""

    def __init__(
        self,
        filterer: LinkFilterer | None = None,
        folder_expander: FolderExpander | None = None,
        scraper: LinkScraper | None = None,
        config: BatchConfig | None = None,
        *,
        reporter: BatchReporter | None = None,
        browser_factory: Callable[[], AbstractAsyncContextManager[AsyncBrowser]]
        | None = None,
    ) -> None:
        self._config = config or BatchConfig()
        self._filterer: LinkFilterer = filterer or ClassifierLinkFilterer()
        self._folder_expander: FolderExpander = (
            folder_expander or DriveFolderExpander()
        )
        self._scraper: LinkScraper = scraper or PlaywrightAsyncLinkScraper()
        self._reporter: BatchReporter = reporter or NullBatchReporter()
        self._browser_factory = browser_factory

    @property
    def config(self) -> BatchConfig:
        return self._config

    @asynccontextmanager
    async def _acquire_browser(
        self, browser: AsyncBrowser | None = None
    ) -> AsyncGenerator[AsyncBrowser, None]:
        if browser is not None:
            yield browser
            return
        if self._browser_factory is not None:
            async with self._browser_factory() as managed_browser:
                yield managed_browser
            return
        async with open_shared_browser(headless=self._config.headless) as owned_browser:
            yield owned_browser

    async def _expand_links(
        self,
        classified_links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
    ) -> list[ClassifiedLink]:
        folder_count = sum(
            1
            for link in classified_links
            if link.document_type == DocumentType.FOLDER
        )
        self._reporter.on_phase2_start(folder_count)

        sig = inspect.signature(self._folder_expander.expand)
        if "browser" in sig.parameters:
            expanded = await self._folder_expander.expand(
                classified_links, browser=browser
            )
        else:
            expanded = await self._folder_expander.expand(classified_links)  # type: ignore[call-arg]

        self._reporter.on_phase2_complete(expanded)
        return expanded

    async def _scrape_all_concurrently(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
    ) -> list[ScrapeItemResult]:
        total = len(links)
        if total == 0:
            return []

        semaphore = asyncio.Semaphore(self._config.concurrency)
        completed_count = 0
        self._reporter.on_phase3_start(total)

        async def _worker(link: ClassifiedLink) -> ScrapeItemResult:
            nonlocal completed_count
            async with semaphore:
                self._reporter.on_item_start(link)
                try:
                    result = await self._scraper.scrape_one(
                        link, browser, self._config
                    )
                except Exception as exc:
                    error_msg = str(exc).strip() or type(exc).__name__
                    result = ScrapeItemResult(
                        link=link, output_path=None, error=error_msg
                    )
                completed_count += 1
                self._reporter.on_item_complete(result, completed_count, total)
                return result

        with suppress_live_progress():
            results = await asyncio.gather(*(_worker(link) for link in links))
        return list(results)

    async def run(
        self,
        source: Path | str | Sequence[str],
        *,
        browser: AsyncBrowser | None = None,
    ) -> BatchScrapeResult:
        """Execute the 3-phase batch scrape asynchronously."""
        # Phase 1: Filtering & Pre-Classification (no browser launched)
        raw_urls, preclassified = self._filterer.filter_and_classify(source)
        self._reporter.on_phase1_complete(raw_urls, preclassified)

        if not preclassified:
            empty_result = BatchScrapeResult(
                raw_urls=raw_urls, classified_links=[], results=[]
            )
            self._reporter.on_batch_complete(empty_result)
            return empty_result

        # Phases 2 & 3: Share a single Chromium Browser instance
        async with self._acquire_browser(browser) as shared_browser:
            expanded_links = await self._expand_links(
                preclassified, shared_browser
            )
            results = await self._scrape_all_concurrently(
                expanded_links, shared_browser
            )

        batch_result = BatchScrapeResult(
            raw_urls=raw_urls,
            classified_links=expanded_links,
            results=results,
        )
        self._reporter.on_batch_complete(batch_result)
        return batch_result

    def run_sync(
        self,
        source: Path | str | Sequence[str],
        *,
        browser: AsyncBrowser | None = None,
    ) -> BatchScrapeResult:
        """Synchronous convenience entrypoint for running the batch scraper."""
        return asyncio.run(self.run(source, browser=browser))


# ---------------------------------------------------------------------------
# CLI Integration (typer)
# ---------------------------------------------------------------------------
app = typer.Typer(
    name="batch-scrape",
    help="Batch scrape documents from a .txt file using a single shared Chromium browser.",
    add_completion=False,
)


@app.command()
def main(
    source: Path = typer.Argument(
        ...,
        help="Path to the .txt file containing document/folder URLs to scrape.",
    ),
    concurrency: int = typer.Option(
        config.CONCURRENT_SCRAPER,
        "--concurrency",
        "-c",
        min=1,
        help="Maximum number of concurrent scraping workers sharing one browser.",
    ),
    output: Path = typer.Option(
        Path(config.OUTPUT_DIR),
        "--output",
        "-o",
        help="Output directory for scraped documents.",
    ),
    scrape_type: str = typer.Option(
        config.SCRAPE_TYPE,
        "--scrape-type",
        "-t",
        help="Scrape mode: 'd' (direct download first) or 's' (viewer screenshot/API).",
    ),
) -> None:
    """Run the 3-phase async batch scraper from the command line."""
    cli_console = Console()
    normalized_type = scrape_type.strip().lower()
    if normalized_type not in {"d", "s"}:
        cli_console.print(
            f"[bold red]Invalid --scrape-type {scrape_type!r}. Must be 'd' or 's'.[/bold red]"
        )
        raise typer.Exit(code=1)

    if not source.exists() or not source.is_file():
        cli_console.print(
            f"[bold red]Input file does not exist: {source}[/bold red]"
        )
        raise typer.Exit(code=1)

    batch_config = BatchConfig(
        output_dir=output,
        concurrency=concurrency,
        scrape_type=normalized_type,
    )
    reporter = RichBatchReporter(cli_console)
    batch_scraper = BatchScraper(config=batch_config, reporter=reporter)

    try:
        result = asyncio.run(batch_scraper.run(source))
    except Exception as exc:
        cli_console.print(f"[bold red]Batch scrape failed: {exc}[/bold red]")
        raise typer.Exit(code=1) from exc

    if not reporter.summary_printed:
        reporter.print_summary(result)

    if not result.classified_links or result.failed_count > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
