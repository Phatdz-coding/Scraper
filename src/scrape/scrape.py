"""Async Batch Scraper Module.

Coordinates multi-stage link filtering/pre-classification (Phase 1),
Google Drive folder expansion (Phase 2), and bounded concurrent document
scraping sharing a single async Chromium browser (Phase 3).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from playwright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
    async_playwright,
)
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
import typer

from scrape.classifiers import (
    ClassifiedLink,
    DocumentType,
    InputParser,
    LinkClassificationPipeline,
    LinkInputParser,
    WebsiteType,
)
from scrape import config
from scrape.config import BatchConfig, BrowserConfig, RuntimeConfigOverrides
from scrape import folder_scraper
from scrape import ggdoc_scraper
from scrape.router import start_scraping
from scrape.template_info import Template
from scrape.tracker import (
    BatchErrorCollector,
    OutputFilenameResolver,
    batch_error_context,
    suppress_live_progress,
    task_execution_context,
)

console = Console()


# ---------------------------------------------------------------------------
# Data Models (DTOs)
# ---------------------------------------------------------------------------
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
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if not item.succeeded)

    @property
    def error_count(self) -> int:
        return len(self.errors)


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
        browser: AsyncBrowser | AsyncBrowserContext,
        *,
        accept_downloads: bool | None = None,
    ) -> tuple[AsyncBrowserContext | None, AsyncPage]:
        """Open a Page on a shared persistent BrowserContext or an isolated BrowserContext on a Browser."""
        allow_downloads = (
            config.BROWSER_ACCEPT_DOWNLOADS
            if accept_downloads is None
            else accept_downloads
        )
        new_context = getattr(browser, "new_context", None)
        if new_context is not None:
            context = await new_context(accept_downloads=allow_downloads)
            try:
                page = await context.new_page()
                return context, page
            except Exception:
                await _close_resource_quietly(context)
                raise

        try:
            page = await browser.new_page()
        except TypeError:
            page = await browser.new_page(accept_downloads=allow_downloads)  # type: ignore[call-arg]
        return None, page

    @staticmethod
    def _has_resolved_document_type(link: ClassifiedLink) -> bool:
        """Return True when link.document_type is already resolved (not None, SHORTCUT, or UNKNOWN)."""
        return link.document_type is not None and link.document_type not in {
            DocumentType.SHORTCUT,
            DocumentType.UNKNOWN,
        }

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
            browser_cfg = getattr(config, "browser", None) or BrowserConfig.from_config()
            target_url = self._resolve_navigation_url(link)

            async def page_factory() -> AsyncPage:
                """Lazily open the isolated context/page and navigate once."""
                nonlocal context, page
                if page is not None:
                    return page
                context, page = await self._open_isolated_page(
                    browser, accept_downloads=browser_cfg.accept_downloads
                )
                await page.goto(target_url, wait_until=browser_cfg.wait_until)
                return page

            injected_scrape_fn = self._scrape_fn is not None
            if injected_scrape_fn:
                # Custom/mocked scrape_fn expects a ready page and template (legacy contract).
                ready_page = await page_factory()
                if self._has_resolved_document_type(link):
                    template_info = Template(
                        ready_page,
                        target_url,
                        document_type=link.document_type,
                    )
                    if template_info.website_type is None:
                        template_info.website_type = link.website_type
                else:
                    active_template_factory = self._template_factory or Template.create
                    template_info = await active_template_factory(
                        ready_page, target_url
                    )
                    if getattr(template_info, "document_type", None) is not None:
                        link.set_document_type(template_info.document_type)
            else:
                # Lightweight template: no browser page, no DOM inspection upfront.
                template_info = Template(
                    None,
                    target_url,
                    website_type=link.website_type,
                    document_type=link.document_type,
                    file_id=getattr(link, "file_id", None),
                )

            active_scrape_fn = self._scrape_fn or start_scraping
            with task_execution_context(
                output_dir=config.output_dir,
                scrape_type=config.scrape_type,
                filename_resolver=self._filename_resolver,
                enable_live=False,
            ) as task_ctx:
                if injected_scrape_fn:
                    if self._scribd_scraper is not None:
                        raw_res = active_scrape_fn(
                            page, template_info, self._scribd_scraper
                        )
                    else:
                        raw_res = active_scrape_fn(page, template_info)
                else:
                    raw_res = active_scrape_fn(
                        None,
                        template_info,
                        self._scribd_scraper,
                        page_factory=page_factory,
                        template_factory=self._template_factory,
                    )

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

            resolved_type = getattr(template_info, "document_type", None)
            if resolved_type is not None and resolved_type != link.document_type:
                link.set_document_type(resolved_type)

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

    def _print_errors(self, errors: Sequence[str]) -> None:
        self.console.print("[bold red]#Errors:[/bold red]")
        if not errors:
            self.console.print("None", style="dim")
            return
        for error in errors:
            self.console.print(error, markup=False, highlight=False, soft_wrap=True)

    def _print_failures(self, results: Sequence[ScrapeItemResult]) -> None:
        self.console.print("[bold red]#Failures:[/bold red]")
        failed_items = [item for item in results if not item.succeeded]
        if not failed_items:
            self.console.print("None", style="dim")
            return

        separator = "-" * 28
        self.console.print(separator, style="dim")
        for idx, item in enumerate(failed_items, start=1):
            doc_type = (
                item.link.document_type.name
                if item.link.document_type is not None
                else "UNKNOWN"
            )
            type_label = f"{item.link.website_type.name}.{doc_type}"
            detail = item.error or "Unknown error"
            self.console.print(f"[bold]No.{idx}[/bold]")
            self.console.print(
                f"[bold]URL:[/bold] {escape(item.link.url)}",
                highlight=False,
                soft_wrap=True,
            )
            self.console.print(
                f"[bold]Website/DocumentType:[/bold] {escape(type_label)}",
                highlight=False,
                soft_wrap=True,
            )
            self.console.print(
                f"[bold]Log:[/bold] {escape(detail)}",
                highlight=False,
                soft_wrap=True,
            )
            self.console.print(separator, style="dim")

    def _print_totals(self, batch_result: BatchScrapeResult) -> None:
        self.console.print(
            f"[bold]Total:[/bold] {len(batch_result.results)} | "
            f"[bold red]Fail:[/bold red] {batch_result.failed_count}"
        )

    def print_summary(self, batch_result: BatchScrapeResult) -> None:
        """Render batch errors and failed items only in plain block format."""
        self._print_errors(batch_result.errors)
        self._print_failures(batch_result.results)
        self._print_totals(batch_result)


# ---------------------------------------------------------------------------
# Shared Browser Factory (Persistent Context + Anti-Bot Stealth)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def open_shared_browser(
    headless: bool | None = None,
    user_data_dir: Path | str | None = None,
    *,
    browser_config: BrowserConfig | None = None,
    clean: bool = False,
) -> AsyncGenerator[AsyncBrowser | AsyncBrowserContext, None]:
    """Launch a shared Chromium browser.

    When ``clean=False`` (default), opens a persistent ``BrowserContext`` with
    the configured profile directory and anti-bot stealth settings.
    When ``clean=True``, launches a clean ephemeral ``Browser`` (matching
    ``playwright open``) without persistent cookies, custom User-Agent overrides,
    or stealth scripts—needed for sites like Scribd that reject the persistent
    stealth profile with ``net::ERR_HTTP_RESPONSE_CODE_FAILURE``.
    """
    effective_config = (
        browser_config or BrowserConfig.from_config()
    ).with_overrides(
        headless=headless,
        user_data_dir=user_data_dir,
    )

    async with async_playwright() as playwright:
        if clean:
            browser = await playwright.chromium.launch(
                headless=effective_config.headless
            )
            context: AsyncBrowserContext | None = None
            try:
                context = await browser.new_context(
                    accept_downloads=effective_config.accept_downloads
                )
                yield context
            finally:
                await _close_resource_quietly(context)
                await _close_resource_quietly(browser)
            return

        effective_config.user_data_dir.mkdir(parents=True, exist_ok=True)
        context = await playwright.chromium.launch_persistent_context(
            **effective_config.to_persistent_context_kwargs()
        )
        try:
            if effective_config.stealth_init_script:
                await context.add_init_script(effective_config.stealth_init_script)
            yield context
        finally:
            await _close_resource_quietly(context)


# ---------------------------------------------------------------------------
# Browser Mode Planning (headless vs. headed passes)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScrapePass:
    """A contiguous slice of the ordered batch scraped in one browser mode."""

    links: tuple[ClassifiedLink, ...]
    headless: bool
    clean: bool = False


class HeadedBrowserPolicy:
    """Decides which websites need a headed browser and plans ordered scrape passes.

    Websites that block headless Chromium (Scribd by default) are stably sorted to
    the end of the batch so they can run in a single trailing clean headed pass.
    """

    DEFAULT_HEADED_SITES: frozenset[WebsiteType] = frozenset({WebsiteType.SCRIBD})

    def __init__(self, headed_sites: frozenset[WebsiteType] | None = None) -> None:
        self._headed_sites = (
            self.DEFAULT_HEADED_SITES if headed_sites is None else frozenset(headed_sites)
        )

    def requires_headed(self, link: ClassifiedLink) -> bool:
        return link.website_type in self._headed_sites

    def order(self, links: Sequence[ClassifiedLink]) -> list[ClassifiedLink]:
        """Return a stably sorted copy: headless-capable links first, headed-only links last."""
        return sorted(links, key=self.requires_headed)

    def plan(
        self, ordered_links: Sequence[ClassifiedLink], *, headless: bool
    ) -> list[ScrapePass]:
        """Split already-ordered links into contiguous passes, preserving order."""
        if not ordered_links:
            return []
        if not headless:
            has_headed_only = any(
                self.requires_headed(link) for link in ordered_links
            )
            return [
                ScrapePass(
                    tuple(ordered_links), headless=False, clean=has_headed_only
                )
            ]

        headless_links = tuple(
            link for link in ordered_links if not self.requires_headed(link)
        )
        headed_links = tuple(
            link for link in ordered_links if self.requires_headed(link)
        )
        passes = (
            [ScrapePass(headless_links, headless=True, clean=False)]
            if headless_links
            else []
        )
        if headed_links:
            passes.append(ScrapePass(headed_links, headless=False, clean=True))
        return passes


# ---------------------------------------------------------------------------
# Browser Sessions (owned/relaunchable vs. externally provided)
# ---------------------------------------------------------------------------
@runtime_checkable
class BrowserSession(Protocol):
    """Port that hands out a browser for a requested launch mode."""

    @property
    def active_browser(self) -> AsyncBrowser | None: ...

    @property
    def can_switch_mode(self) -> bool:
        """True when acquire() honours the requested launch mode (may relaunch)."""
        ...

    async def acquire(self, headless: bool, *, clean: bool = False) -> AsyncBrowser: ...


class FixedBrowserSession:
    """Session over an externally owned browser: never closes or relaunches it.

    The requested launch mode is a preference only; the provided browser is always returned.
    """

    def __init__(self, browser: AsyncBrowser) -> None:
        self._browser = browser

    @property
    def active_browser(self) -> AsyncBrowser:
        return self._browser

    @property
    def can_switch_mode(self) -> bool:
        return False

    async def acquire(self, headless: bool, *, clean: bool = False) -> AsyncBrowser:
        return self._browser


class RelaunchableBrowserSession:
    """Lazily launches an owned browser and relaunches it when the launch mode changes.

    The previous browser is fully closed before relaunching so the persistent
    profile directory (USER_DATA_DIR) is unlocked for the next launch.
    """

    def __init__(
        self,
        launcher: Callable[[bool, bool], AbstractAsyncContextManager[AsyncBrowser]],
    ) -> None:
        self._launcher = launcher
        self._exit_stack: AsyncExitStack | None = None
        self._browser: AsyncBrowser | None = None
        self._mode: tuple[bool, bool] | None = None

    @property
    def active_browser(self) -> AsyncBrowser | None:
        return self._browser

    @property
    def can_switch_mode(self) -> bool:
        return True

    async def acquire(self, headless: bool, *, clean: bool = False) -> AsyncBrowser:
        requested_mode = (headless, clean)
        if self._browser is not None and self._mode == requested_mode:
            return self._browser
        await self.close()
        return await self._launch(headless, clean)

    async def _launch(self, headless: bool, clean: bool) -> AsyncBrowser:
        exit_stack = AsyncExitStack()
        browser = await exit_stack.enter_async_context(self._launcher(headless, clean))
        self._exit_stack = exit_stack
        self._browser = browser
        self._mode = (headless, clean)
        return browser

    async def close(self) -> None:
        exit_stack = self._exit_stack
        self._exit_stack, self._browser, self._mode = None, None, None
        if exit_stack is not None:
            await exit_stack.aclose()

    async def __aenter__(self) -> RelaunchableBrowserSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class _ProgressCounter:
    """Shared Phase 3 completion counter spanning all scrape passes."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.completed = 0

    def advance(self) -> int:
        self.completed += 1
        return self.completed


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
        headed_policy: HeadedBrowserPolicy | None = None,
    ) -> None:
        self._config = config or BatchConfig()
        self._filterer: LinkFilterer = filterer or ClassifierLinkFilterer()
        self._folder_expander: FolderExpander = (
            folder_expander or DriveFolderExpander()
        )
        self._scraper: LinkScraper = scraper or PlaywrightAsyncLinkScraper()
        self._reporter: BatchReporter = reporter or NullBatchReporter()
        self._browser_factory = browser_factory
        self._headed_policy = headed_policy or HeadedBrowserPolicy()

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
        async with open_shared_browser(
            headless=self._config.headless,
            browser_config=self._config.browser,
        ) as owned_browser:
            yield owned_browser

    def _launch_owned_browser(
        self, headless: bool, clean: bool = False
    ) -> AbstractAsyncContextManager[AsyncBrowser]:
        """Launch an owned shared browser from self._config.browser with the given launch mode."""
        return open_shared_browser(
            headless=headless,
            browser_config=self._config.browser,
            clean=clean,
        )

    @asynccontextmanager
    async def _open_browser_session(
        self, browser: AsyncBrowser | None = None
    ) -> AsyncGenerator[BrowserSession, None]:
        """Yield a fixed session for external/injected browsers, else a lazy relaunchable one."""
        if browser is not None or self._browser_factory is not None:
            async with self._acquire_browser(browser) as external_browser:
                yield FixedBrowserSession(external_browser)
            return
        async with RelaunchableBrowserSession(self._launch_owned_browser) as session:
            yield session

    @staticmethod
    def _count_folders(links: Sequence[ClassifiedLink]) -> int:
        return sum(1 for link in links if link.document_type == DocumentType.FOLDER)

    async def _browser_for_expansion(
        self,
        links: Sequence[ClassifiedLink],
        session: BrowserSession,
    ) -> AsyncBrowser | None:
        """Launch a browser for Phase 2 only when folder links need expanding."""
        if self._count_folders(links) == 0:
            return session.active_browser
        return await session.acquire(self._config.headless)

    async def _expand_links(
        self,
        classified_links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None,
    ) -> list[ClassifiedLink]:
        self._reporter.on_phase2_start(self._count_folders(classified_links))

        sig = inspect.signature(self._folder_expander.expand)
        if "browser" in sig.parameters:
            expanded = await self._folder_expander.expand(
                classified_links, browser=browser
            )
        else:
            expanded = await self._folder_expander.expand(classified_links)  # type: ignore[call-arg]

        self._reporter.on_phase2_complete(expanded)
        return expanded

    @staticmethod
    def _single_pass(
        links: Sequence[ClassifiedLink], headless: bool
    ) -> list[ScrapePass]:
        return [ScrapePass(tuple(links), headless=headless)] if links else []

    def _plan_passes(
        self,
        ordered_links: Sequence[ClassifiedLink],
        session: BrowserSession,
    ) -> list[ScrapePass]:
        """Plan headless/headed passes; non-switchable (external) sessions get one pass."""
        if not session.can_switch_mode:
            return self._single_pass(ordered_links, self._config.headless)
        return self._headed_policy.plan(
            ordered_links, headless=self._config.headless
        )

    @staticmethod
    def _failed_result(link: ClassifiedLink, exc: BaseException) -> ScrapeItemResult:
        error_msg = str(exc).strip() or type(exc).__name__
        return ScrapeItemResult(link=link, output_path=None, error=error_msg)

    def _report_item_complete(
        self, result: ScrapeItemResult, counter: _ProgressCounter
    ) -> None:
        self._reporter.on_item_complete(result, counter.advance(), counter.total)

    async def _scrape_one_safely(
        self, link: ClassifiedLink, browser: AsyncBrowser
    ) -> ScrapeItemResult:
        try:
            return await self._scraper.scrape_one(link, browser, self._config)
        except Exception as exc:
            return self._failed_result(link, exc)

    async def _scrape_links(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        """Scrape links concurrently (bounded by config.concurrency) on one browser."""
        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def _worker(link: ClassifiedLink) -> ScrapeItemResult:
            async with semaphore:
                self._reporter.on_item_start(link)
                result = await self._scrape_one_safely(link, browser)
                self._report_item_complete(result, counter)
                return result

        return list(await asyncio.gather(*(_worker(link) for link in links)))

    def _fail_pass(
        self,
        scrape_pass: ScrapePass,
        exc: BaseException,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        """Mark every link of a pass as failed when its browser could not be launched."""
        mode = "headless" if scrape_pass.headless else "headed"
        reason = str(exc).strip() or type(exc).__name__
        launch_error = RuntimeError(f"Failed to launch {mode} browser: {reason}")
        results = [self._failed_result(link, launch_error) for link in scrape_pass.links]
        for result in results:
            self._report_item_complete(result, counter)
        return results

    async def _run_pass(
        self,
        scrape_pass: ScrapePass,
        session: BrowserSession,
        counter: _ProgressCounter,
    ) -> list[ScrapeItemResult]:
        try:
            browser = await session.acquire(
                scrape_pass.headless, clean=scrape_pass.clean
            )
        except Exception as exc:
            return self._fail_pass(scrape_pass, exc, counter)
        return await self._scrape_links(scrape_pass.links, browser, counter)

    async def _scrape_passes(
        self,
        passes: Sequence[ScrapePass],
        session: BrowserSession,
    ) -> list[ScrapeItemResult]:
        """Run passes sequentially with one continuous Phase 3 progress bar."""
        counter = _ProgressCounter(sum(len(p.links) for p in passes))
        if counter.total == 0:
            return []

        self._reporter.on_phase3_start(counter.total)
        results: list[ScrapeItemResult] = []
        with suppress_live_progress():
            for scrape_pass in passes:
                results.extend(await self._run_pass(scrape_pass, session, counter))
        return results

    async def _scrape_all_concurrently(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser,
    ) -> list[ScrapeItemResult]:
        """Single-pass Phase 3 on one given browser (kept for backward compatibility)."""
        return await self._scrape_passes(
            self._single_pass(links, self._config.headless),
            FixedBrowserSession(browser),
        )

    async def _expand_and_scrape(
        self,
        preclassified: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None,
    ) -> tuple[list[ClassifiedLink], list[ScrapeItemResult]]:
        """Phases 2 & 3: expand folders, order links (headed-only last), scrape in passes."""
        async with self._open_browser_session(browser) as session:
            phase2_browser = await self._browser_for_expansion(preclassified, session)
            expanded = await self._expand_links(preclassified, phase2_browser)
            ordered_links = self._headed_policy.order(expanded)
            passes = self._plan_passes(ordered_links, session)
            results = await self._scrape_passes(passes, session)
        return ordered_links, results

    async def run(
        self,
        source: Path | str | Sequence[str],
        *,
        browser: AsyncBrowser | None = None,
    ) -> BatchScrapeResult:
        """Execute the 3-phase batch scrape asynchronously."""
        error_collector = BatchErrorCollector()
        with batch_error_context(error_collector):
            # Phase 1: Filtering & Pre-Classification (no browser launched)
            raw_urls, preclassified = self._filterer.filter_and_classify(source)
            self._reporter.on_phase1_complete(raw_urls, preclassified)

            if not preclassified:
                empty_result = BatchScrapeResult(
                    raw_urls=raw_urls,
                    classified_links=[],
                    results=[],
                    errors=error_collector.get_errors(),
                )
                self._reporter.on_batch_complete(empty_result)
                return empty_result

            # Phases 2 & 3: headless pass first, then a headed pass for Scribd links
            ordered_links, results = await self._expand_and_scrape(
                preclassified, browser
            )

        batch_result = BatchScrapeResult(
            raw_urls=raw_urls,
            classified_links=ordered_links,
            results=results,
            errors=error_collector.get_errors(),
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
class OptionalValueOptionCommand(typer.core.TyperCommand):
    """TyperCommand supporting options whose value argument is optional (e.g. --use-gg-api [<api_key>])."""

    optional_value_options: frozenset[str] = frozenset({"--use-gg-api", "-ugga"})

    def _collect_parser_metadata(
        self, ctx: typer.Context
    ) -> tuple[dict[str, int], int]:
        """Inspect registered params to find value-taking option arities and required positional count."""
        value_taking_arities: dict[str, int] = {}
        required_positionals = 0
        for param in self.get_params(ctx):
            if isinstance(param, typer.core.TyperArgument):
                if param.required:
                    required_positionals += max(1, param.nargs or 1)
                continue
            if not isinstance(param, typer.core.TyperOption):
                continue
            if param.is_flag or param.count:
                continue
            arity = max(1, param.nargs or 1)
            for opt in param.opts:
                if opt not in self.optional_value_options:
                    value_taking_arities[opt] = arity
        return value_taking_arities, required_positionals

    def _normalize_optional_value_args(
        self,
        args: Sequence[str],
        value_taking_arities: dict[str, int],
        required_positionals: int,
    ) -> list[str]:
        """Inject an empty sentinel value after bare optional-value options without stealing positional args."""
        insert_empty_after: set[int] = set()
        candidate_pairs: list[tuple[int, int]] = []
        guaranteed_positionals = 0

        idx = 0
        total = len(args)
        while idx < total:
            token = args[idx]
            if token == "--":
                guaranteed_positionals += total - idx - 1
                break

            if token in self.optional_value_options:
                next_idx = idx + 1
                if (
                    next_idx < total
                    and args[next_idx] != "--"
                    and not args[next_idx].startswith("-")
                ):
                    candidate_pairs.append((idx, next_idx))
                    idx += 2
                else:
                    insert_empty_after.add(idx)
                    idx += 1
                continue

            if "=" in token and token.split("=", 1)[0] in self.optional_value_options:
                idx += 1
                continue

            if token in value_taking_arities:
                idx += 1 + value_taking_arities[token]
                continue

            if token.startswith("-") and len(token) > 1:
                idx += 1
                continue

            guaranteed_positionals += 1
            idx += 1

        reserve_count = max(0, required_positionals - guaranteed_positionals)
        if reserve_count > 0 and candidate_pairs:
            # Preserve trailing candidate tokens for required positional arguments (e.g. `source`).
            for opt_idx, _ in candidate_pairs[-reserve_count:]:
                insert_empty_after.add(opt_idx)

        if not insert_empty_after:
            return list(args)

        normalized: list[str] = []
        for idx, token in enumerate(args):
            normalized.append(token)
            if idx in insert_empty_after:
                normalized.append("")
        return normalized

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        value_taking_arities, required_positionals = self._collect_parser_metadata(ctx)
        normalized_args = self._normalize_optional_value_args(
            args, value_taking_arities, required_positionals
        )
        return super().parse_args(ctx, normalized_args)


def _was_passed_on_cli(ctx: typer.Context, param_name: str) -> bool:
    """Return True when the option was explicitly supplied on the command line."""
    source = ctx.get_parameter_source(param_name)
    return getattr(source, "name", None) == "COMMANDLINE"


def resolve_cli_source(raw_source: str) -> Path | str:
    """Resolve a CLI source argument to either an HTTP(S) URL string or an existing file Path.

    Raises ValueError with a user-facing error message when the value is neither
    a valid HTTP(S) URL nor an existing file.
    """
    cleaned = raw_source.strip()
    if (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in {'"', "'"}
    ):
        cleaned = cleaned[1:-1].strip()

    if not cleaned:
        raise ValueError("Input source cannot be empty.")

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() in {"http", "https"}:
        if not parsed.netloc:
            raise ValueError(f"Invalid URL (missing domain): {cleaned}")
        return cleaned

    source_path = Path(cleaned)
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"Input file does not exist: {source_path}")
    return source_path


app = typer.Typer(
    name="batch-scrape",
    help="Batch scrape documents from a .txt file or a single URL using a single shared Chromium browser.",
    add_completion=False,
)


@app.command(cls=OptionalValueOptionCommand)
def main(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help="Path to a .txt file of URLs, or a single document/folder URL to scrape.",
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
    headless_disable: bool = typer.Option(
        False,
        "--headless-disable",
        "-hd",
        is_flag=True,
        help="Disable headless mode and open a visible browser window for visualization.",
    ),
    use_gg_api: str | None = typer.Option(
        None,
        "--use-gg-api",
        "-ugga",
        metavar="[<api_key>]",
        show_default=False,
        help="Enable Google Drive API for folder scraping; optionally pass an API key to override the configured key.",
    ),
    concurrent_folder: int = typer.Option(
        config.MAX_CONCURENT_FOLDER_NODE_VISIT,
        "--concurrent-folder",
        "-cf",
        min=1,
        help="Maximum number of Google Drive folders to visit and scrape concurrently.",
    ),
    folder_timeout: int = typer.Option(
        config.FOLDER_TIMEOUT,
        "--folder-timeout",
        "-ft",
        min=1,
        help="Maximum time in seconds to wait when opening a Google Drive folder (recommended >= --concurrent-folder).",
    ),
    max_folder_depth: int = typer.Option(
        config.MAX_FOLDER_DEPTH,
        "--max-folder-depth",
        "-mfd",
        min=1,
        max=100,
        help="Maximum nested subfolder depth to traverse when expanding Google Drive folders.",
    ),
    max_retry: int = typer.Option(
        config.SCRAPE_RETRY_COUNT,
        "--max-retry",
        "-mr",
        min=0,
        max=10,
        help="Maximum number of retry attempts when a scrape operation fails.",
    ),
    max_concurrent_img_api: int = typer.Option(
        config.MAX_CONCURRENT_IMG_API,
        "--max-concurrent-img-api",
        "-mcia",
        min=1,
        max=30,
        help="Maximum number of concurrent viewer image API requests per batch.",
    ),
) -> None:
    """Run the 3-phase async batch scraper from the command line."""
    cli_console = Console()
    try:
        normalized_type = config.normalize_scrape_type(scrape_type)
    except ValueError:
        cli_console.print(
            f"[bold red]Invalid --scrape-type {scrape_type!r}. Must be 'd' or 's'.[/bold red]"
        )
        raise typer.Exit(code=1)

    try:
        resolved_source = resolve_cli_source(source)
    except ValueError as exc:
        cli_console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc

    overrides = RuntimeConfigOverrides(
        headless_disable=headless_disable,
        use_google_drive_api=use_gg_api is not None,
        google_api_key=use_gg_api,
        concurrent_folder=(
            concurrent_folder
            if _was_passed_on_cli(ctx, "concurrent_folder")
            else None
        ),
        folder_timeout=(
            folder_timeout if _was_passed_on_cli(ctx, "folder_timeout") else None
        ),
        max_folder_depth=(
            max_folder_depth if _was_passed_on_cli(ctx, "max_folder_depth") else None
        ),
        max_retry=max_retry if _was_passed_on_cli(ctx, "max_retry") else None,
        max_concurrent_img_api=(
            max_concurrent_img_api
            if _was_passed_on_cli(ctx, "max_concurrent_img_api")
            else None
        ),
    )
    overrides.apply()

    batch_config = BatchConfig.from_config(
        output_dir=output,
        concurrency=concurrency,
        scrape_type=normalized_type,
    )
    reporter = RichBatchReporter(cli_console)
    batch_scraper = BatchScraper(config=batch_config, reporter=reporter)

    try:
        result = asyncio.run(batch_scraper.run(resolved_source))
    except Exception as exc:
        reporter.on_batch_complete(
            BatchScrapeResult(
                raw_urls=[],
                classified_links=[],
                results=[],
                errors=[f"Batch scrape failed: {exc}"],
            )
        )
        raise typer.Exit(code=1) from exc

    if not reporter.summary_printed:
        reporter.print_summary(result)

    if not result.classified_links or result.failed_count > 0:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
