from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Protocol, runtime_checkable

import img2pdf
from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
from playwright.sync_api import sync_playwright

from scrape import config
from scrape.tracker import (
    ProgressReporter,
    get_active_output_dir,
    get_active_progress,
    release_output_path,
    reserve_output_path,
    scrape_progress,
)

logger = logging.getLogger(__name__)

# Initial cleanup CSS: removes Scribd chrome, ads, modals, and restores body scrolling.
CLEANUP_CSS = """
    .promo_header, .between_page_ads, .page_missing_banner,
    header, footer, .modal_overlay, .dimmer, .adaptive_banner {
        display: none !important;
        visibility: hidden !important;
    }
    body { overflow: auto !important; background-color: white !important; }
"""

# Page isolation CSS: when <body> has the `sx-isolate` class, EVERYTHING is hidden (visibility: hidden)
# except the page being captured (data-sx-target) and its descendant elements.
# visibility does not affect layout, so the page won't shift; headers/toolbars/banners, etc.,
# regardless of whether they are fixed, sticky, absolute, or anywhere else, will vanish from screenshots.
ISOLATE_CSS = """
    body.sx-isolate * { visibility: hidden !important; }
    body.sx-isolate [data-sx-target],
    body.sx-isolate [data-sx-target] * { visibility: visible !important; }

    /* Floating elements marked as hidden (even if located inside the target page) */
    body.sx-isolate [data-sx-target] [data-sx-hidden],
    body.sx-isolate [data-sx-target] [data-sx-hidden] *,
    [data-sx-hidden], [data-sx-hidden] * { visibility: hidden !important; }
"""

PAGE_SELECTORS: tuple[str, ...] = (".outer_page", ".page", "div[id^='page_']")

# JavaScript helper functions injected into the page (loaded from scripts/scribd.js)
INSTALL_JS = (Path(__file__).resolve().parent / "scripts" / "scribd.js").read_text(
    encoding="utf-8"
)


def _resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    if output_dir is not None:
        return Path(output_dir)
    return get_active_output_dir() or Path(config.OUTPUT_DIR)


def _resolve_progress(progress: ProgressReporter | None = None) -> ProgressReporter:
    return progress or get_active_progress() or scrape_progress


def _ensure_pdf_filename(document_name: str) -> str:
    cleaned = document_name.strip() or "document"
    if not cleaned.lower().endswith(".pdf"):
        return f"{cleaned}.pdf"
    return cleaned


@dataclass(frozen=True)
class ScribdScrapeConfig:
    """Immutable configuration controlling Scribd DOM discovery, scrolling, and capture."""

    page_selectors: tuple[str, ...] = PAGE_SELECTORS
    scroll_step_px: int = 800
    scroll_pause_seconds: float = 0.0
    bottom_settle_seconds: float = 0.5
    page_settle_seconds: float = 1.0
    isolate_settle_seconds: float = 0.3
    max_scroll_steps: int = 2000
    selector_wait_timeout_ms: int = 15_000
    save_debug_overlays: bool = True
    debug_overlay_dir: Path | None = None

    def __post_init__(self) -> None:
        if not self.page_selectors:
            raise ValueError("page_selectors must contain at least one selector")
        if self.scroll_step_px <= 0:
            raise ValueError("scroll_step_px must be greater than zero")
        if self.max_scroll_steps <= 0:
            raise ValueError("max_scroll_steps must be greater than zero")
        if self.selector_wait_timeout_ms < 0:
            raise ValueError("selector_wait_timeout_ms cannot be negative")
        for field_name in (
            "scroll_pause_seconds",
            "bottom_settle_seconds",
            "page_settle_seconds",
            "isolate_settle_seconds",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} cannot be negative")


@runtime_checkable
class PdfCompiler(Protocol):
    """Port for compiling captured page image files into a single PDF file."""

    def compile(self, image_paths: Sequence[Path], output_path: Path) -> None: ...


class Img2PdfCompiler:
    """Default PdfCompiler implementation backed by img2pdf."""

    def compile(self, image_paths: Sequence[Path], output_path: Path) -> None:
        if not image_paths:
            raise ValueError("At least one image path is required to compile a PDF")
        pdf_bytes = img2pdf.convert([str(path) for path in image_paths])
        if pdf_bytes is None:
            raise RuntimeError("img2pdf returned empty output")
        output_path.write_bytes(pdf_bytes)


@dataclass(frozen=True)
class _CaptureSession:
    """Internal immutable context shared across per-page screenshot capture steps."""

    page: Page
    page_elements: Locator
    total_pages: int
    temp_dir: Path
    debug_dir: Path
    debug_prefix: str
    progress: ProgressReporter


class ScribdPageScraper:
    """Async Scribd in-page scraper implementing DOM cleanup, lazy-load scroll, isolation, and PDF merge."""

    def __init__(
        self,
        *,
        scrape_config: ScribdScrapeConfig | None = None,
        pdf_compiler: PdfCompiler | None = None,
        progress: ProgressReporter | None = None,
    ) -> None:
        self._config = scrape_config or ScribdScrapeConfig()
        self._pdf_compiler: PdfCompiler = pdf_compiler or Img2PdfCompiler()
        self._progress = progress

    async def scrape(
        self,
        page: Page,
        document_name: str,
        *,
        output_dir: Path | str | None = None,
    ) -> Path:
        """Capture all document pages from an already-open Scribd Page and merge them into a PDF."""
        destination = _resolve_output_dir(output_dir)
        destination.mkdir(parents=True, exist_ok=True)

        active_progress = _resolve_progress(self._progress)
        active_progress.reset()

        pdf_filename = _ensure_pdf_filename(document_name)
        output_path = await reserve_output_path(destination, pdf_filename)
        debug_dir = self._config.debug_overlay_dir or destination

        try:
            await self._prepare_page(page)
            await self._scroll_to_load_pages(page)
            page_selector, page_elements, total_pages = await self._discover_pages(page)
            await self._install_isolation_helpers(page, page_selector)

            with tempfile.TemporaryDirectory(prefix="scribd_pages_") as temp_dir_name:
                session = _CaptureSession(
                    page=page,
                    page_elements=page_elements,
                    total_pages=total_pages,
                    temp_dir=Path(temp_dir_name),
                    debug_dir=debug_dir,
                    debug_prefix=output_path.stem,
                    progress=active_progress,
                )
                image_paths = await self._capture_all_pages(session)
                if not image_paths:
                    raise RuntimeError("No Scribd document pages were captured")
                await self._compile_pdf(image_paths, output_path)

            return output_path
        except Exception:
            if output_path.exists():
                try:
                    output_path.unlink()
                except OSError:
                    pass
            release_output_path(output_path)
            raise
        finally:
            active_progress.finish()

    async def _prepare_page(self, page: Page) -> None:
        """Phase 1: inject cleanup CSS and wait briefly for viewer page containers to mount."""
        await page.add_style_tag(content=CLEANUP_CSS)
        if self._config.selector_wait_timeout_ms <= 0:
            return
        combined_selector = ", ".join(self._config.page_selectors)
        try:
            await page.wait_for_selector(
                combined_selector,
                state="attached",
                timeout=self._config.selector_wait_timeout_ms,
            )
        except (TimeoutError, PlaywrightTimeoutError):
            logger.debug(
                "Timed out waiting for Scribd selectors (%s); continuing to scroll.",
                combined_selector,
            )

    async def _scroll_to_load_pages(self, page: Page) -> None:
        """Phase 2: incrementally scroll from top to bottom to trigger lazy-loaded Scribd pages."""
        last_height = await page.evaluate("document.body.scrollHeight")
        last_scroll = -1

        for _ in range(self._config.max_scroll_steps):
            await page.evaluate(f"window.scrollBy(0, {self._config.scroll_step_px});")
            if self._config.scroll_pause_seconds > 0:
                await asyncio.sleep(self._config.scroll_pause_seconds)

            new_height = await page.evaluate("document.body.scrollHeight")
            current_scroll = await page.evaluate(
                "window.innerHeight + window.pageYOffset"
            )

            reached_bottom = current_scroll >= new_height
            stalled = current_scroll <= last_scroll and new_height == last_height
            if reached_bottom or stalled:
                break

            last_height = new_height
            last_scroll = current_scroll

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        if self._config.bottom_settle_seconds > 0:
            await asyncio.sleep(self._config.bottom_settle_seconds)

    async def _discover_pages(self, page: Page) -> tuple[str, Locator, int]:
        """Phase 3a: probe known Scribd page selectors and return the first matching locator."""
        for selector in self._config.page_selectors:
            locator = page.locator(selector)
            count = await locator.count()
            if count > 0:
                return selector, locator, count
        raise RuntimeError(
            "Could not find standard Scribd page selector on the page "
            f"(tried: {', '.join(self._config.page_selectors)})"
        )

    async def _install_isolation_helpers(
        self, page: Page, page_selector: str
    ) -> None:
        """Phase 3b: inject ISOLATE_CSS and register scripts/scribd.js DOM helpers."""
        await page.add_style_tag(content=ISOLATE_CSS)
        await page.evaluate(INSTALL_JS, page_selector)

    async def _capture_all_pages(self, session: _CaptureSession) -> list[Path]:
        """Phase 4: isolate and capture every discovered page element to PNG."""
        image_paths: list[Path] = []
        for index in range(session.total_pages):
            try:
                img_path = await self._capture_single_page(session, index)
                image_paths.append(img_path)
                session.progress.increment(session.total_pages)
            except Exception as exc:
                logger.warning(
                    "Error capturing Scribd page %d/%d: %s",
                    index + 1,
                    session.total_pages,
                    exc,
                )
        return image_paths

    async def _capture_single_page(
        self, session: _CaptureSession, index: int
    ) -> Path:
        """Isolate a single page element, check for overlays, and write its PNG screenshot."""
        element = session.page_elements.nth(index)
        await element.scroll_into_view_if_needed()
        if self._config.page_settle_seconds > 0:
            await asyncio.sleep(self._config.page_settle_seconds)

        hidden_nodes = await session.page.evaluate("window.__sxHide()")
        if hidden_nodes:
            logger.debug("Hidden floating Scribd nodes on page %d: %s", index + 1, hidden_nodes)

        await element.evaluate("n => window.__sxIsolate(n)")
        if self._config.isolate_settle_seconds > 0:
            await asyncio.sleep(self._config.isolate_settle_seconds)

        await self._inspect_overlays(session, element, index)

        img_path = session.temp_dir / f"page_{index + 1:03d}.png"
        await element.screenshot(path=str(img_path), animations="disabled")
        return img_path

    async def _inspect_overlays(
        self, session: _CaptureSession, element: Locator, index: int
    ) -> None:
        """Detect elements overlapping the isolated page and optionally capture a debug screenshot."""
        overlays = await element.evaluate("n => window.__sxOverlays(n)")
        if not overlays:
            return
        logger.warning("Scribd page %d has overlay elements: %s", index + 1, overlays)
        if not self._config.save_debug_overlays:
            return
        session.debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = (
            session.debug_dir
            / f"{session.debug_prefix}_debug_overlay_{index + 1:03d}.png"
        )
        await session.page.screenshot(path=str(debug_path))

    async def _compile_pdf(
        self, image_paths: Sequence[Path], output_path: Path
    ) -> None:
        """Phase 5: merge captured PNG screenshots into the target PDF off the event loop."""
        await asyncio.to_thread(self._pdf_compiler.compile, image_paths, output_path)


async def scrape_inpage(
    page: Page,
    document_name: str,
    *,
    output_dir: Path | str | None = None,
    progress: ProgressReporter | None = None,
) -> Path:
    """Scrape an open Scribd document page into a PDF file and return its saved Path.

    Designed for direct integration with ``router.start_scraping`` and ``scrape.py``.
    The caller retains ownership of ``page`` and its parent browser/context.
    """
    scraper = ScribdPageScraper(progress=progress)
    return await scraper.scrape(page, document_name, output_dir=output_dir)


def download_scribd_as_pdf(url, output_pdf="document.pdf"):
    with sync_playwright() as p:
        # Launch browser
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        print(f"🚀 Opening document: {url}")
        page.goto(url)

        print("⏳ Please log in (if needed) in the opened browser window. Starting in 5 seconds...")
        time.sleep(0)

        # 1. Clean up initial UI elements via CSS
        print("🧹 Cleaning up Scribd interface...")
        page.add_style_tag(content=CLEANUP_CSS)

        # 2. Auto-scroll slowly from top to bottom to force Scribd to load all lazy-loaded pages
        print("📜 Auto-scrolling to load full content...")
        # scroll_pause_time = 5
        last_height = page.evaluate("document.body.scrollHeight")

        while True:
            page.evaluate("window.scrollBy(0, 800);")
            # time.sleep(scroll_pause_time)

            new_height = page.evaluate("document.body.scrollHeight")
            current_scroll = page.evaluate("window.innerHeight + window.pageYOffset")

            if current_scroll >= new_height or new_height == last_height:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.5)
                break
            last_height = new_height

        print("✅ Finished loading all content! Proceeding to capture each page...")

        # 3. Find all container elements of the Scribd document pages
        page_selector = None
        for sel in PAGE_SELECTORS:
            if page.locator(sel).count() > 0:
                page_selector = sel
                break

        if page_selector is None:
            print("⚠️ Could not find standard page selector. Please check the webpage structure.")
            browser.close()
            return

        page_elements = page.locator(page_selector)
        count = page_elements.count()
        print(f"🔍 Found a total of {count} document pages (selector: {page_selector}).")

        # Inject isolation CSS and JS helpers
        page.add_style_tag(content=ISOLATE_CSS)
        page.evaluate(INSTALL_JS, page_selector)

        image_paths = []
        os.makedirs("temp_scribd_images", exist_ok=True)

        # 4. Iterate through each page, isolate it (hide everything else), and take screenshot
        for i in range(count):
            el = page_elements.nth(i)
            try:
                el.scroll_into_view_if_needed()
                time.sleep(1)  # Wait for page layout/rendering to stabilize

                # Safeguard layer: hide floating fixed/sticky elements
                for h in page.evaluate("window.__sxHide()"):
                    print(f"   🙈 Hidden: {h}")

                # Main layer: keep only this page visible; everything else (header, toolbar...) is hidden
                el.evaluate("n => window.__sxIsolate(n)")
                time.sleep(0.3)

                # Check if anything is still overlaying the page (for easier debugging if headers overlap)
                overlays = el.evaluate("n => window.__sxOverlays(n)")
                if overlays:
                    print(f"   ⚠️ Page {i+1}: overlay elements detected:")
                    for o in overlays:
                        print(f"      - {o}")
                    page.screenshot(path=f"debug_overlay_{i+1:03d}.png")

                img_path = f"temp_scribd_images/page_{i+1:03d}.png"
                el.screenshot(path=img_path, animations="disabled")
                image_paths.append(img_path)
                print(f"📸 Captured page {i+1}/{count}")
            except Exception as e:
                print(f"❌ Error capturing page {i+1}: {e}")

        browser.close()

        # 5. Combine all captured images into a complete PDF file
        if image_paths:
            print(f"📚 Merging {len(image_paths)} images into PDF: {output_pdf}...")
            with open(output_pdf, "wb") as f:
                f.write(img2pdf.convert(image_paths))
            print(f"🎉 Success! PDF file saved at: {os.path.abspath(output_pdf)}")

            # Clean up temporary image files
            print("🧹 Cleaning up temporary image files...")
            for img in image_paths:
                try:
                    os.remove(img)
                except:
                    pass
            try:
                os.rmdir("temp_scribd_images")
            except:
                pass
        else:
            print("⚠️ No pages were captured!")


if __name__ == "__main__":
    url_input = "https://www.scribd.com/document/338391046/Maths-Olympiad-Advanced-Training?_gl=1*2rsfyw*_up*MQ..*_ga*MTY2NzM1NTk0NS4xNzk1MDM0OTU5*_ga_Z4ZC50DED6*czE3OTUwMzQ5NTgkbzEkZzEkdDE3OTUwMzQ5NTgkajYwJGwwJGgw*_ga_8KZ8BV0P5W*czE3OTUwMzQ5NTgkbzEkZzEkdDE3OTUwMzQ5NTgkajYwJGwwJGgw#page=17"
    output_name = "out.pdf"

    download_scribd_as_pdf(url_input, output_name)
