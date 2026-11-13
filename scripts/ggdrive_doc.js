(async function () {
  console.log("Loading script ...");

  const IMG_SELECTOR = 'img[src^="blob:"]';
  const MIN_DIMENSION = 100;
  const JPEG_QUALITY = 0.92;

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // --- Optimized scroll container lookup ---
  // Try known Google Docs preview selectors first, then fall back to a
  // limited search instead of iterating every DOM node.
  function getScrollContainer() {
    // Google Docs / Drive preview commonly uses these containers
    const knownSelectors = [
      '.kix-appview-editor',       // Docs editor view
      '.a-b-r-La',                 // Drive preview pane
      '[role="main"]',             // Generic main content
    ];

    for (const sel of knownSelectors) {
      const el = document.querySelector(sel);
      if (el && el.scrollHeight > el.clientHeight) {
        return el;
      }
    }

    // Fallback: search only direct children of body and common wrapper depths
    let bestElement = document.scrollingElement || document.documentElement;
    let maxScroll = bestElement.scrollHeight;

    const candidates = document.querySelectorAll(
      'body > *, body > * > *, body > * > * > *'
    );
    for (const el of candidates) {
      const style = window.getComputedStyle(el);
      if (
        (style.overflowY === 'auto' || style.overflowY === 'scroll' || style.overflowY === 'overlay') &&
        el.scrollHeight > el.clientHeight &&
        el.scrollHeight > maxScroll
      ) {
        maxScroll = el.scrollHeight;
        bestElement = el;
      }
    }
    return bestElement;
  }

  // --- Structural page discovery (borrowed from ggdrive_pdf.js) ---
  // If the preview uses blob images inside identically-classed page containers,
  // we can iterate them directly — no scrolling needed.
  function tryGetStructuralPages() {
    const images = [...document.querySelectorAll(IMG_SELECTOR)];
    if (images.length === 0) return null;

    const firstPage = images[0].parentElement;
    const className = firstPage?.className;
    if (!className || className.match(/\s/)) return null;

    const pages = [
      ...firstPage.parentElement.querySelectorAll(
        `:scope > .${CSS.escape(className)}`
      ),
    ];
    return pages.length > 0 ? pages : null;
  }

  // --- Wait for an image to appear & fully load (MutationObserver) ---
  function waitForImage(container) {
    return new Promise((resolve, reject) => {
      const existing = container.querySelector(IMG_SELECTOR);
      if (existing) {
        if (existing.complete) return resolve(existing);
        existing.onload = () => resolve(existing);
        existing.onerror = (e) => reject(e);
        return;
      }

      const observer = new MutationObserver((_mutations, obs) => {
        const img = container.querySelector(IMG_SELECTOR);
        if (img) {
          obs.disconnect();
          if (img.complete) return resolve(img);
          img.onload = () => resolve(img);
          img.onerror = (e) => reject(e);
        }
      });
      observer.observe(container, {
        childList: true,
        attributes: true,
        subtree: true,
      });

      // Scroll the container into view to trigger lazy-loading
      container.scrollIntoView();

      // Safety timeout — don't hang forever
      setTimeout(() => {
        observer.disconnect();
        reject(new Error("Timed out waiting for image"));
      }, 10000);
    });
  }

  // --- Capture a single element to a JPEG data URL ---
  function captureElement(el) {
    const width = el.naturalWidth || el.width || el.clientWidth;
    const height = el.naturalHeight || el.height || el.clientHeight;
    if (width < MIN_DIMENSION || height < MIN_DIMENSION) return null;

    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    canvas.width = width;
    canvas.height = height;

    try {
      ctx.drawImage(el, 0, 0, width, height);
      const imgData = canvas.toDataURL("image/jpeg", JPEG_QUALITY);
      if (imgData.length <= 100) return null;
      return { imgData, width, height };
    } catch (_e) {
      return null; // canvas taint / detached node
    }
  }

  // ========== STRATEGY 1: Structural page iteration ==========
  async function buildPdfFromStructuralPages(pages) {
    const { jsPDF } = window.jspdf;
    let pdf = null;

    for (let i = 0; i < pages.length; i++) {
      let img;
      try {
        img = await waitForImage(pages[i]);
      } catch (_e) {
        console.warn(`Skipping page ${i + 1}: image did not load`);
        continue;
      }

      const captured = captureElement(img);
      if (!captured) continue;

      const { imgData, width, height } = captured;
      const orientation = width > height ? "l" : "p";

      if (!pdf) {
        pdf = new jsPDF({
          orientation,
          unit: "px",
          format: [width, height],
        });
      } else {
        pdf.addPage([width, height], orientation);
      }

      pdf.addImage(imgData, "JPEG", 0, 0, width, height, "", "FAST");
      console.log("__PDF_SCRAPER_PAGE_SCRAPED__");

      const pct = Math.floor(((i + 1) / pages.length) * 100);
      console.log(`Processing content ${pct}%`);
    }

    return pdf;
  }

  // ========== STRATEGY 2: Scroll-scan fallback ==========
  async function buildPdfFromScrollScan() {
    const { jsPDF } = window.jspdf;

    const scrollContainer = getScrollContainer();
    const pageMap = new Map();

    function captureVisible() {
      const imgElements = document.querySelectorAll("img, canvas");
      for (const el of imgElements) {
        const width = el.naturalWidth || el.width || el.clientWidth;
        const height = el.naturalHeight || el.height || el.clientHeight;
        if (width < MIN_DIMENSION || height < MIN_DIMENSION) continue;

        const rect = el.getBoundingClientRect();
        if (
          rect.width === 0 ||
          rect.height === 0 ||
          window.getComputedStyle(el).opacity === "0"
        )
          continue;

        const currentTop = scrollContainer.scrollTop + rect.top;
        const currentLeft = scrollContainer.scrollLeft + rect.left;

        const sourceKey =
          el.tagName === "IMG"
            ? el.currentSrc || el.src || "img"
            : `canvas:${width}x${height}`;
        const pageKey = [
          sourceKey,
          Math.round(currentTop / 10),
          Math.round(currentLeft / 10),
          width,
          height,
        ].join("|");

        if (pageMap.has(pageKey)) continue;

        const captured = captureElement(el);
        if (captured) {
          pageMap.set(pageKey, {
            ...captured,
            top: Math.round(currentTop),
            left: Math.round(currentLeft),
          });
        }
      }
    }

    console.log("Scanning and scrolling content ...");
    scrollContainer.scrollTop = 0;
    await sleep(500);

    // Scroll by 80% of the viewport per step for better coverage with fewer steps
    const scrollStep = Math.max(
      Math.floor(scrollContainer.clientHeight * 0.8),
      400
    );
    let currentScroll = 0;

    while (true) {
      captureVisible();
      currentScroll += scrollStep;
      const previousScrollTop = scrollContainer.scrollTop;
      scrollContainer.scrollTop = currentScroll;
      await sleep(150); // shorter settle — images are deduped anyway

      if (scrollContainer.scrollTop <= previousScrollTop) break;
    }
    // Final capture at the bottom
    captureVisible();

    const pages = Array.from(pageMap.values()).sort(
      (a, b) => a.top - b.top || a.left - b.left
    );

    if (pages.length === 0) return null;

    let pdf = null;
    for (let i = 0; i < pages.length; i++) {
      const page = pages[i];
      const orientation = page.width > page.height ? "l" : "p";

      if (!pdf) {
        pdf = new jsPDF({
          orientation,
          unit: "px",
          format: [page.width, page.height],
        });
      } else {
        pdf.addPage([page.width, page.height], orientation);
      }

      pdf.addImage(
        page.imgData,
        "JPEG",
        0,
        0,
        page.width,
        page.height,
        "",
        "FAST"
      );
      console.log("__PDF_SCRAPER_PAGE_SCRAPED__");

      const pct = Math.floor(((i + 1) / pages.length) * 100);
      console.log(`Processing content ${pct}%`);
    }

    return pdf;
  }

  // ========== Main entry ==========
  let script = document.createElement("script");
  script.onload = async function () {
    try {
      let pdf = null;

      // Prefer structural page discovery (faster, more reliable)
      const structuralPages = tryGetStructuralPages();
      if (structuralPages) {
        console.log(
          `Found ${structuralPages.length} page containers — using structural mode`
        );
        pdf = await buildPdfFromStructuralPages(structuralPages);
      }

      // Fall back to scroll-scanning
      if (!pdf) {
        console.log("Using scroll-scan fallback ...");
        pdf = await buildPdfFromScrollScan();
      }

      if (!pdf) {
        console.log("No preview pages found.");
        document.body.removeChild(script);
        return;
      }

      let title =
        document.querySelector('meta[itemprop="name"]')?.content ||
        document.title ||
        "download.pdf";
      title = title.replace(/\.docx?$/i, "");
      if ((title.split(".").pop() || "").toLowerCase() !== "pdf") {
        title = title + ".pdf";
      }

      console.log("Downloading PDF file ...");
      await pdf.save(title, { returnPromise: true });
      document.body.removeChild(script);
      console.log("PDF downloaded!");
    } catch (e) {
      console.error("PDF generation failed:", e);
      alert(e);
    }
  };

  let scriptURL = "https://unpkg.com/jspdf@latest/dist/jspdf.umd.min.js";
  let trustedURL;
  if (window.trustedTypes && trustedTypes.createPolicy) {
    const policy = trustedTypes.createPolicy("myPolicy", {
      createScriptURL: (input) => input,
    });
    trustedURL = policy.createScriptURL(scriptURL);
  } else {
    trustedURL = scriptURL;
  }

  script.src = trustedURL;
  document.body.appendChild(script);
})();
