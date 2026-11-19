# Scribd Document Scraper Sequence Diagram

This document illustrates the execution lifecycle and interactions in [`scribd_scraper.py`](file:///media/New%20Volume/projects/Python/Scraper/scribd_scraper.py) for the `download_scribd_as_pdf()` function.

---

## 1. High-Level Sequence Diagram

The diagram below details the entire flow from initial document navigation and automated lazy-loading to per-page DOM isolation, screenshot capture, and final PDF generation.

```mermaid
sequenceDiagram
    autonumber
    actor Caller as "Caller / User"
    participant Scraper as "scribd_scraper.py"
    participant Browser as "Playwright Chromium"
    participant DOM as "Scribd Page DOM"
    participant Scripts as "scripts/scribd.js"
    participant FS as "Local Filesystem / img2pdf"

    Caller->>Scraper: download_scribd_as_pdf(url, output_pdf)
    activate Scraper

    %% Phase 1: Browser Launch & Navigation
    Note over Scraper, Browser: Phase 1: Browser Launch and Page Navigation
    Scraper->>Browser: Launch Chromium (headless=False, viewport 1280x900)
    Browser-->>Scraper: browser and page context
    Scraper->>DOM: page.goto(url)
    Scraper->>DOM: page.add_style_tag(cleanup CSS)
    Note over DOM: Suppresses headers, footers, promos, modals, and ads

    %% Phase 2: Lazy Loading
    Note over Scraper, DOM: Phase 2: Force Lazy-Loading via Auto-Scroll
    loop While scrollOffset < scrollHeight and not reached bottom
        Scraper->>DOM: page.evaluate(scrollBy 800px)
        Scraper->>DOM: Check scrollHeight vs (innerHeight + pageYOffset)
    end
    Scraper->>DOM: page.evaluate(scrollTo bottom)

    %% Phase 3: Discovery & Isolation Setup
    Note over Scraper, Scripts: Phase 3: Page Discovery and Isolation Injection
    Scraper->>DOM: Locate page containers (.outer_page, .page, div page selector)
    DOM-->>Scraper: Matched page locator and count N
    Scraper->>DOM: page.add_style_tag(ISOLATE_CSS)
    Scraper->>Scripts: Read scripts/scribd.js (INSTALL_JS)
    Scripts-->>Scraper: JS source code
    Scraper->>DOM: page.evaluate(INSTALL_JS, page_selector)
    Note over DOM: Registers window.__sxHide, __sxIsolate, __sxOverlays
    Scraper->>FS: os.makedirs(temp_scribd_images, exist_ok=True)

    %% Phase 4: Capture Loop
    Note over Scraper, FS: Phase 4: Per-Page Isolation and Screenshot Loop
    loop For each page element i from 0 to N-1
        Scraper->>DOM: el.scroll_into_view_if_needed()
        Scraper->>DOM: page.evaluate(__sxHide)
        DOM-->>Scraper: Hidden fixed or sticky element descriptors
        Scraper->>DOM: el.evaluate(__sxIsolate)
        Note over DOM: Marks target element with data-sx-target and adds sx-isolate to body
        Scraper->>DOM: el.evaluate(__sxOverlays)
        alt Overlays Detected
            DOM-->>Scraper: List of overlapping elements
            Scraper->>FS: page.screenshot(debug_overlay_XXX.png)
        else No Overlays
            DOM-->>Scraper: Empty list (no overlays)
        end
        Scraper->>DOM: el.screenshot(temp_scribd_images/page_XXX.png)
        DOM-->>FS: Write image file
    end

    %% Phase 5: PDF Merging & Cleanup
    Note over Scraper, FS: Phase 5: Compilation and Cleanup
    Scraper->>Browser: browser.close()
    Scraper->>FS: img2pdf.convert(image_paths)
    FS-->>FS: Write output_pdf
    Scraper->>FS: Delete temporary images and remove temp directory

    Scraper-->>Caller: PDF generated successfully
    deactivate Scraper
```

---

## 2. Component Responsibilities & Phase Descriptions

### Phase 1: Browser Launch & Navigation
- **Playwright Chromium**: Launches in non-headless mode to avoid bot-detection heuristics and accommodate optional authentication.
- **Initial CSS Cleanup**: Injects an initial style tag that hides global site headers, footers, promo bars, overlays, and ads with `display: none !important`.

### Phase 2: Lazy-Load Forcing
- Scribd virtualizes its document viewer, rendering pages only when scrolled into proximity.
- An iterative scroll loop scrolls the viewport down by 800px increments until `window.innerHeight + window.pageYOffset >= document.body.scrollHeight`.

### Phase 3: Page Discovery & Isolation Setup
- **Page Selectors**: Probes known container selectors (`.outer_page`, `.page`, `div[id^='page_']`) to identify individual pages.
- **`ISOLATE_CSS`**: Hides all elements on `body.sx-isolate` with `visibility: hidden !important` unless they are marked with `[data-sx-target]`. Using `visibility: hidden` maintains page geometry and prevents reflow.
- **`scripts/scribd.js`**: Injects three client-side helper utilities:
  1. `window.__sxHide()`: Suppresses floating sticky/fixed nodes.
  2. `window.__sxIsolate(el)`: Targets exactly one page and activates the isolation mode on `body`.
  3. `window.__sxOverlays(el)`: Performs grid sampling using `document.elementFromPoint` to verify no elements overlap the page.

### Phase 4: Per-Page Isolation & Screenshot Capture
- Iterates over each discovered page element:
  - Scrolls the page into view.
  - Hides background elements and floating toolbars.
  - Verifies that no overlaying nodes obstruct the view.
  - Captures an isolated screenshot to `temp_scribd_images/page_{i:03d}.png`.

### Phase 5: PDF Merging & Cleanup
- Closes the Chromium browser context.
- Aggregates all temporary `.png` screenshots into a unified PDF file via `img2pdf`.
- Removes all temporary screenshot files and deletes the staging folder.
