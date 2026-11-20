"""Concrete Phase 1, Phase 2, and Phase 3 adapters implementing batch ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
import inspect
from pathlib import Path

from playwright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Page as AsyncPage,
)

from scrape import config
from scrape import folder_scraper
from scrape import ggdoc_scraper
from scrape.batch.models import ScrapeItemResult
from scrape.batch.ports import FolderExpander, LinkFilterer, LinkScraper
from scrape.classifiers import (
    ClassifiedLink,
    DocumentType,
    InputParser,
    LinkClassificationPipeline,
    LinkInputParser,
    WebsiteType,
)
from scrape.config import BatchConfig, BrowserConfig
from scrape.router import start_scraping
from scrape.template_info import Template
from scrape.tracker import OutputFilenameResolver, task_execution_context


class ClassifierLinkFilterer(LinkFilterer):
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


class DriveFolderExpander(FolderExpander):
    """Phase 2 adapter: seeds all folder links into DriveFolderScraper.queue and expands them."""

    def __init__(
        self,
        folder_scraper_fn: Callable[..., Awaitable[list[ClassifiedLink]]] | None = None,
    ) -> None:
        self._folder_scraper_fn = folder_scraper_fn

    @staticmethod
    def _partition_links(
        links: Sequence[ClassifiedLink],
    ) -> tuple[list[ClassifiedLink], list[str]]:
        non_folder_links: list[ClassifiedLink] = []
        folder_urls: list[str] = []
        for link in links:
            if link.document_type == DocumentType.FOLDER:
                folder_urls.append(link.url)
            else:
                non_folder_links.append(link)
        return non_folder_links, folder_urls

    async def _scrape_folders(
        self,
        folder_urls: list[str],
        browser: AsyncBrowser | None,
    ) -> list[ClassifiedLink]:
        active_fn = self._folder_scraper_fn or folder_scraper.scrape_many_classified
        try:
            sig = inspect.signature(active_fn)
            if "browser" in sig.parameters:
                return await active_fn(folder_urls, browser=browser)
            return await active_fn(folder_urls)
        except Exception as exc:
            folder_scraper.log_error(
                f"Error expanding folders {folder_urls}: {exc}. Continuing with non-folder links."
            )
            return []

    @staticmethod
    def _merge_deduplicated_links(
        non_folder_links: Sequence[ClassifiedLink],
        discovered_links: Sequence[ClassifiedLink],
    ) -> list[ClassifiedLink]:
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

    async def expand(
        self,
        links: Sequence[ClassifiedLink],
        browser: AsyncBrowser | None = None,
    ) -> list[ClassifiedLink]:
        non_folder_links, folder_urls = self._partition_links(links)
        discovered_links = (
            await self._scrape_folders(folder_urls, browser) if folder_urls else []
        )
        return self._merge_deduplicated_links(non_folder_links, discovered_links)


class PlaywrightAsyncLinkScraper(LinkScraper):
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
    async def _close_quietly(resource: object | None) -> None:
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

    @staticmethod
    def _resolve_navigation_url(link: ClassifiedLink) -> str:
        """Convert Google Docs document URLs to Drive AllTabsView URLs just like scrape.py."""
        url = link.url
        lowered = url.lower()
        is_google_doc = (
            link.website_type == WebsiteType.GOOGLE_DOC
            and "spreadsheet" not in lowered
            and "presentation" not in lowered
        )
        if not is_google_doc:
            return url
        try:
            return ggdoc_scraper.switch_url_to_AllTabsView(url)
        except ValueError:
            return url

    @classmethod
    async def _open_isolated_page(
        cls,
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
                await cls._close_quietly(context)
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

    async def _build_eager_template(
        self,
        link: ClassifiedLink,
        ready_page: AsyncPage,
        target_url: str,
    ) -> Template:
        if self._has_resolved_document_type(link):
            template_info = Template(
                ready_page,
                target_url,
                document_type=link.document_type,
            )
            if template_info.website_type is None:
                template_info.website_type = link.website_type
            return template_info

        active_template_factory = self._template_factory or Template.create
        template_info = await active_template_factory(ready_page, target_url)
        if getattr(template_info, "document_type", None) is not None:
            link.set_document_type(template_info.document_type)
        return template_info

    @staticmethod
    def _build_lazy_template(link: ClassifiedLink, target_url: str) -> Template:
        return Template(
            None,
            target_url,
            website_type=link.website_type,
            document_type=link.document_type,
            file_id=getattr(link, "file_id", None),
        )

    async def _invoke_injected_scrape_fn(
        self,
        ready_page: AsyncPage,
        template_info: Template,
    ) -> Path | None:
        assert self._scrape_fn is not None
        if self._scribd_scraper is not None:
            raw_res = self._scrape_fn(ready_page, template_info, self._scribd_scraper)
        else:
            raw_res = self._scrape_fn(ready_page, template_info)
        return await raw_res if inspect.isawaitable(raw_res) else raw_res

    async def _invoke_default_scrape_fn(
        self,
        template_info: Template,
        page_factory: Callable[[], Awaitable[AsyncPage]],
    ) -> Path | None:
        raw_res = start_scraping(
            None,
            template_info,
            self._scribd_scraper,
            page_factory=page_factory,
            template_factory=self._template_factory,
        )
        return await raw_res if inspect.isawaitable(raw_res) else raw_res

    async def _reserve_untracked_output(
        self,
        output_path: Path | str,
        template_info: Template,
        default_dir: Path,
    ) -> Path:
        resolved = Path(output_path)
        target_dir = (
            resolved.parent
            if str(resolved.parent) not in {"", "."}
            else default_dir
        )
        return await self._filename_resolver.reserve(
            target_dir,
            resolved.name,
            file_id=getattr(template_info, "fileID", None),
        )

    @staticmethod
    def _sync_resolved_document_type(
        link: ClassifiedLink, template_info: Template
    ) -> None:
        resolved_type = getattr(template_info, "document_type", None)
        if resolved_type is not None and resolved_type != link.document_type:
            link.set_document_type(resolved_type)

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
            browser_cfg = (
                getattr(config, "browser", None) or BrowserConfig.from_config()
            )
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

            ready_page: AsyncPage | None = None
            if self._scrape_fn is not None:
                ready_page = await page_factory()
                template_info = await self._build_eager_template(
                    link, ready_page, target_url
                )
            else:
                template_info = self._build_lazy_template(link, target_url)

            with task_execution_context(
                output_dir=config.output_dir,
                scrape_type=config.scrape_type,
                filename_resolver=self._filename_resolver,
                enable_live=False,
            ) as task_ctx:
                if self._scrape_fn is not None:
                    assert ready_page is not None
                    output_path = await self._invoke_injected_scrape_fn(
                        ready_page, template_info
                    )
                else:
                    output_path = await self._invoke_default_scrape_fn(
                        template_info, page_factory
                    )

                if output_path is not None and task_ctx.reserved_path is None:
                    output_path = await self._reserve_untracked_output(
                        output_path, template_info, config.output_dir
                    )

            self._sync_resolved_document_type(link, template_info)
            return ScrapeItemResult(link=link, output_path=output_path, error=None)
        except Exception as exc:
            return ScrapeItemResult.failure(link, exc)
        finally:
            await self._close_quietly(page)
            await self._close_quietly(context)
