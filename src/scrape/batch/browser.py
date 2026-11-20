"""Browser lifecycle management, channel fallback, mode policies, and sessions."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
import inspect
import logging
from pathlib import Path
from typing import Any, TypeVar

from playwright.async_api import (
    Browser as AsyncBrowser,
    BrowserContext as AsyncBrowserContext,
    Error as PlaywrightError,
    Playwright,
    async_playwright,
)

from scrape import config
from scrape.batch.models import ScrapePass
from scrape.batch.ports import BrowserModePolicy, BrowserSession
from scrape.classifiers import ClassifiedLink, WebsiteType
from scrape.config import BrowserConfig

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Low-level Cleanup & Channel Fallback Helpers
# ---------------------------------------------------------------------------
async def _close_resource_quietly(resource: object | None) -> None:
    """Close a sync or async browser resource, ignoring cleanup errors."""
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


def _resolve_browser_channels(
    configured_channel: str | None,
    *,
    clean: bool,
) -> tuple[str | None, ...]:
    """Build the ordered, deduplicated Playwright channel fallback chain.

    Tries system Google Chrome (``"chrome"``) first, then system Microsoft Edge
    (``"msedge"``), and finally falls back to Playwright's bundled Chromium.
    If ``configured_channel`` explicitly requests another channel (e.g.,
    ``"chrome-beta"`` or ``"msedge"``), it is tried first before falling back.
    """
    candidates: list[str | None] = []
    if configured_channel not in (None, "chromium"):
        candidates.append(configured_channel)

    for channel in config.BROWSER_CHANNEL_FALLBACKS:
        if channel == "chromium" and (clean or configured_channel is None):
            continue
        candidates.append(channel)

    candidates.append(None)
    return tuple(dict.fromkeys(candidates))


def _format_launch_error(exc: Exception) -> str:
    """Return a concise single-line summary for a browser launch failure."""
    message = str(exc).strip()
    if not message:
        return type(exc).__name__
    return message.splitlines()[0]


async def _launch_with_channel_fallback(
    launcher: Callable[..., Awaitable[_T]],
    base_kwargs: dict[str, Any],
    channels: Sequence[str | None],
) -> _T:
    """Try launching Playwright across candidate channels until one succeeds."""
    failures: list[tuple[str, Exception]] = []
    for channel in channels:
        launch_kwargs = (
            {**base_kwargs, "channel": channel}
            if channel is not None
            else dict(base_kwargs)
        )
        try:
            return await launcher(**launch_kwargs)
        except (PlaywrightError, OSError) as exc:
            label = channel if channel is not None else "chromium (bundled)"
            failures.append((label, exc))
            logger.debug(
                "Browser launch failed for channel %r: %s",
                label,
                _format_launch_error(exc),
            )

    details = "\n".join(
        f"- {label}: {_format_launch_error(exc)}" for label, exc in failures
    )
    last_error = failures[-1][1] if failures else None
    raise RuntimeError(
        "Failed to launch any supported Chromium-based browser.\n"
        f"Tried channels:\n{details}\n"
        "Install Google Chrome, Microsoft Edge, or run 'playwright install chromium'."
    ) from last_error


# ---------------------------------------------------------------------------
# Shared Browser Context Manager Factory
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _open_clean_context(
    playwright: Playwright,
    effective_config: BrowserConfig,
    channels: Sequence[str | None],
) -> AsyncGenerator[AsyncBrowserContext, None]:
    """Launch an ephemeral browser with a single fresh BrowserContext."""
    browser = await _launch_with_channel_fallback(
        playwright.chromium.launch,
        {"headless": effective_config.headless},
        channels,
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


@asynccontextmanager
async def _open_persistent_context(
    playwright: Playwright,
    effective_config: BrowserConfig,
    channels: Sequence[str | None],
) -> AsyncGenerator[AsyncBrowserContext, None]:
    """Launch a persistent stealth BrowserContext backed by user_data_dir."""
    effective_config.user_data_dir.mkdir(parents=True, exist_ok=True)
    persistent_kwargs = effective_config.to_persistent_context_kwargs()
    persistent_kwargs.pop("channel", None)
    context = await _launch_with_channel_fallback(
        playwright.chromium.launch_persistent_context,
        persistent_kwargs,
        channels,
    )
    try:
        if effective_config.stealth_init_script:
            await context.add_init_script(effective_config.stealth_init_script)
        yield context
    finally:
        await _close_resource_quietly(context)


@asynccontextmanager
async def open_shared_browser(
    headless: bool | None = None,
    user_data_dir: Path | str | None = None,
    *,
    browser_config: BrowserConfig | None = None,
    clean: bool = False,
) -> AsyncGenerator[AsyncBrowser | AsyncBrowserContext, None]:
    """Launch a shared Chromium-based browser with Chrome -> Edge -> Chromium fallback.

    Tries system Google Chrome (``"chrome"``) first, then system Microsoft Edge
    (``"msedge"``), and falls back to Playwright's bundled Chromium when neither
    system browser is available.

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
    channels = _resolve_browser_channels(effective_config.channel, clean=clean)

    async with async_playwright() as playwright:
        context_cm = (
            _open_clean_context(playwright, effective_config, channels)
            if clean
            else _open_persistent_context(playwright, effective_config, channels)
        )
        async with context_cm as context:
            yield context


# ---------------------------------------------------------------------------
# Strategy: Browser Mode Planning (headless vs. headed passes)
# ---------------------------------------------------------------------------
class HeadedBrowserPolicy(BrowserModePolicy):
    """Decides which websites need a headed browser and plans ordered scrape passes.

    Websites that block headless Chromium (Scribd by default) are stably sorted to
    the end of the batch so they can run in a single trailing clean headed pass.
    """

    DEFAULT_HEADED_SITES: frozenset[WebsiteType] = frozenset({WebsiteType.SCRIBD})

    def __init__(self, headed_sites: frozenset[WebsiteType] | None = None) -> None:
        self._headed_sites = (
            self.DEFAULT_HEADED_SITES
            if headed_sites is None
            else frozenset(headed_sites)
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
        passes: list[ScrapePass] = []
        if headless_links:
            passes.append(ScrapePass(headless_links, headless=True, clean=False))
        if headed_links:
            passes.append(ScrapePass(headed_links, headless=False, clean=True))
        return passes


# ---------------------------------------------------------------------------
# Browser Sessions (owned/relaunchable vs. externally provided)
# ---------------------------------------------------------------------------
class FixedBrowserSession(BrowserSession):
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

    async def acquire(
        self, headless: bool, *, clean: bool = False
    ) -> AsyncBrowser:
        return self._browser


class RelaunchableBrowserSession(BrowserSession):
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

    async def acquire(
        self, headless: bool, *, clean: bool = False
    ) -> AsyncBrowser:
        requested_mode = (headless, clean)
        if self._browser is not None and self._mode == requested_mode:
            return self._browser
        await self.close()
        return await self._launch(headless, clean)

    async def _launch(self, headless: bool, clean: bool) -> AsyncBrowser:
        exit_stack = AsyncExitStack()
        try:
            browser = await exit_stack.enter_async_context(
                self._launcher(headless, clean)
            )
        except BaseException:
            await exit_stack.aclose()
            raise
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
