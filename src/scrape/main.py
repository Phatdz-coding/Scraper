"""Backwards-compatible facade for the async batch scraper and CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
import sys
import types

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright

from scrape import batch as _batch_pkg
from scrape.batch import (
    BatchReporter, BatchScrapeResult, BatchScraper, BrowserFactory,
    BrowserLauncher, BrowserModePolicy, BrowserSession, ClassifierLinkFilterer,
    DriveFolderExpander, FixedBrowserSession, FolderExpander, HeadedBrowserPolicy,
    LinkFilterer, LinkScraper, NullBatchReporter, OptionalValueOptionCommand,
    PlaywrightAsyncLinkScraper, RelaunchableBrowserSession, RichBatchReporter,
    ScrapeItemResult, ScrapePass, app, console, main, open_shared_browser,
    resolve_cli_source,
)
from scrape.batch import (
    adapters as _adapters_mod,
    browser as _browser_mod,
    orchestrator as _orchestrator_mod,
    reporters as _reporters_mod,
)
from scrape.config import BatchConfig, BrowserConfig
from scrape.router import start_scraping
from scrape.tracker import suppress_live_progress

__all__ = [
    "BatchConfig", "BatchReporter", "BatchScrapeResult", "BatchScraper",
    "BrowserConfig", "BrowserFactory", "BrowserLauncher", "BrowserModePolicy",
    "BrowserSession", "ClassifierLinkFilterer", "DriveFolderExpander",
    "FixedBrowserSession", "FolderExpander", "HeadedBrowserPolicy",
    "LinkFilterer", "LinkScraper", "NullBatchReporter",
    "OptionalValueOptionCommand", "PlaywrightAsyncLinkScraper",
    "RelaunchableBrowserSession", "RichBatchReporter", "ScrapeItemResult",
    "ScrapePass", "app", "console", "main", "open_shared_browser",
    "resolve_cli_source",
]

_PATCH_TARGETS: dict[str, tuple[types.ModuleType, ...]] = {
    "async_playwright": (_browser_mod,),
    "open_shared_browser": (_browser_mod, _batch_pkg),
    "suppress_live_progress": (_orchestrator_mod,),
    "console": (_reporters_mod, _batch_pkg),
    "start_scraping": (_adapters_mod,),
}


class _FacadeModule(types.ModuleType):
    """Forwards legacy unittest.mock.patch('scrape.main.<attr>') calls to owning modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in _PATCH_TARGETS.get(name, ()):
            setattr(target, name, value)


if __name__ != "__main__":
    sys.modules[__name__].__class__ = _FacadeModule

if __name__ == "__main__":
    app()
