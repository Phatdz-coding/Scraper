"""Async Batch Scraper Package Facade.

Coordinates multi-stage link filtering/pre-classification (Phase 1),
Google Drive folder expansion (Phase 2), and bounded concurrent document
scraping sharing a single async Chromium browser (Phase 3).
"""

from __future__ import annotations

from scrape.batch.adapters import (
    ClassifierLinkFilterer,
    DriveFolderExpander,
    PlaywrightAsyncLinkScraper,
)
from scrape.batch.browser import (
    FixedBrowserSession,
    HeadedBrowserPolicy,
    RelaunchableBrowserSession,
    open_shared_browser,
)
from scrape.batch.cli import (
    OptionalValueOptionCommand,
    app,
    main,
    resolve_cli_source,
)
from scrape.batch.models import (
    BatchScrapeResult,
    ScrapeItemResult,
    ScrapePass,
)
from scrape.batch.orchestrator import BatchScraper
from scrape.batch.ports import (
    BatchReporter,
    BrowserFactory,
    BrowserLauncher,
    BrowserModePolicy,
    BrowserSession,
    FolderExpander,
    LinkFilterer,
    LinkScraper,
)
from scrape.batch.reporters import (
    NullBatchReporter,
    RichBatchReporter,
    console,
)

__all__ = [
    # Models (DTOs)
    "BatchScrapeResult",
    "ScrapeItemResult",
    "ScrapePass",
    # Ports / Protocols
    "BatchReporter",
    "BrowserFactory",
    "BrowserLauncher",
    "BrowserModePolicy",
    "BrowserSession",
    "FolderExpander",
    "LinkFilterer",
    "LinkScraper",
    # Browser Lifecycle & Policy
    "FixedBrowserSession",
    "HeadedBrowserPolicy",
    "RelaunchableBrowserSession",
    "open_shared_browser",
    # Reporters
    "NullBatchReporter",
    "RichBatchReporter",
    "console",
    # Concrete Adapters
    "ClassifierLinkFilterer",
    "DriveFolderExpander",
    "PlaywrightAsyncLinkScraper",
    # Orchestrator
    "BatchScraper",
    # CLI
    "OptionalValueOptionCommand",
    "app",
    "main",
    "resolve_cli_source",
]
