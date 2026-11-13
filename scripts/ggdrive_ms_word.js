/* 
NOTE:
This script only works if the page url is:
"https://drive.google.com/file/d/{file_id}/view"
*/

(function() {
  // Page placeholders: any <div> whose style attribute contains "padding-bottom:"
  const PAGE_SELECTOR = 'div[style*="padding-bottom:"]';
  const LOAD_TIMEOUT_MS = 15000;
  const POLL_MS = 30;          // how often to check whether a page has loaded
  const MAX_PENDING = 4;       // max pages being encoded in the background at once
  const JPEG_QUALITY = 0.92;   // 1.0 is much slower and gives huge files for almost no visible gain

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // Group matching divs by parent and take the parent holding the most of them.
  const getPages = function() {
    const matches = [...document.querySelectorAll(PAGE_SELECTOR)];
    if (matches.length === 0) {
      throw new Error('No page placeholders found (div with style containing "padding-bottom:")');
    }

    const groups = new Map();
    for (const el of matches) {
      const parent = el.parentElement;
      if (!groups.has(parent)) groups.set(parent, []);
      groups.get(parent).push(el);
    }

    let best = [];
    for (const list of groups.values()) {
      if (list.length > best.length) best = list;
    }
    return best;
  };

  // ---- Finding the rendered content inside a page <div> -------------------

  const largest = (items, areaOf) =>
    items.reduce((best, el) => (!best || areaOf(el) > areaOf(best) ? el : best), null);

  // Cheap checks first (<img>, <canvas>); the expensive getComputedStyle scan
  // for CSS background images only runs when nothing else was found.
  const findMedia = function(page) {
    const img = largest(
      [...page.querySelectorAll('img')].filter(i => i.src && i.complete && i.naturalWidth > 0),
      i => i.naturalWidth * i.naturalHeight
    );
    if (img) return { type: 'img', el: img };

    const canvas = largest(
      [...page.querySelectorAll('canvas')].filter(c => c.width > 0 && c.height > 0),
      c => c.width * c.height
    );
    if (canvas) return { type: 'canvas', el: canvas };

    for (const el of [page, ...page.querySelectorAll('*')]) {
      const bg = getComputedStyle(el).backgroundImage;
      const match = bg && bg.match(/url\(["']?(.*?)["']?\)/);
      if (match) return { type: 'bg', url: match[1] };
    }
    return null;
  };

  const loadImageFromUrl = (url) => new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = url;
  });

  const toDrawable = async function(media) {
    if (media.type === 'canvas') {
      await sleep(50); // give the viewer a moment to finish painting
      return { source: media.el, width: media.el.width, height: media.el.height, url: null };
    }
    if (media.type === 'img') {
      const img = media.el;
      return { source: img, width: img.naturalWidth, height: img.naturalHeight, url: img.src };
    }
    const img = await loadImageFromUrl(media.url);
    return { source: img, width: img.naturalWidth, height: img.naturalHeight, url: media.url };
  };

  const loadPage = async function(page, index) {
    // Already rendered? Then there is no need to scroll at all.
    let media = findMedia(page);
    if (!media) {
      page.scrollIntoView({ block: 'start', behavior: 'instant' });
      const start = Date.now();
      while (!media && Date.now() - start < LOAD_TIMEOUT_MS) {
        await sleep(POLL_MS);
        media = findMedia(page);
      }
    }
    if (!media) {
      throw new Error(`Page ${index + 1} did not load (no <img>, <canvas> or background-image found inside it)`);
    }
    return toDrawable(media);
  };

  // ---- Encoding (runs in the background while we move on to the next page) -

  const blobToDataURL = (blob) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });

  const drawToCanvas = function(source, width, height) {
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d').drawImage(source, 0, 0, width, height);
    return canvas;
  };

  const canvasToBlob = (canvas) => new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('Canvas encoding failed'))),
      'image/jpeg',
      JPEG_QUALITY
    );
  });

  const encodePage = function(drawable) {
    const { width, height } = drawable;

    // Fast path: if the page image is already a JPEG, reuse its bytes as they
    // are instead of decoding and re-encoding.
    // The fetch must start right now, before the viewer can revoke the blob URL.
    const rawJpeg = drawable.url
      ? fetch(drawable.url).then(r => r.blob()).then(b => (b.type === 'image/jpeg' ? b : null)).catch(() => null)
      : Promise.resolve(null);

    // Canvas sources can be cleared/reused by the viewer, so copy them right away.
    let canvas = drawable.url ? null : drawToCanvas(drawable.source, width, height);

    return (async () => {
      const jpeg = await rawJpeg;
      if (jpeg) {
        return { dataUrl: await blobToDataURL(jpeg), width, height };
      }
      if (!canvas) canvas = drawToCanvas(drawable.source, width, height);
      const blob = await canvasToBlob(canvas);
      return { dataUrl: await blobToDataURL(blob), width, height };
    })();
  };

  // ---- Main ---------------------------------------------------------------

  const buildPDF = async function() {
    const pages = getPages();
    const jobs = [];
    const pending = new Set();

    for (let i = 0; i < pages.length; i++) {
      const drawable = await loadPage(pages[i], i);

      const job = encodePage(drawable);
      jobs.push(job);

      // Bound memory use: don't let too many pages queue up for encoding.
      const tracked = job.catch(() => {}).then(() => pending.delete(tracked));
      pending.add(tracked);
      if (pending.size >= MAX_PENDING) await Promise.race(pending);

      console.log("__PDF_SCRAPER_PAGE_SCRAPED__");
    }

    const results = await Promise.all(jobs);

    let pdf;
    for (const { dataUrl, width, height } of results) {
      if (!pdf) {
        pdf = new jsPDF({ unit: 'px', format: [width, height] });
      } else {
        pdf.addPage([width, height]);
      }
      pdf.addImage(dataUrl, 'JPEG', 0, 0, width, height);
    }
    return pdf;
  };

  const downloadPDF = function() {
    buildPDF()
      .then(pdf => {
        pdf.save("download.pdf");
      })
      .catch((e) => {
        console.log(e);
        alert(e);
      });
  };

  let trustedURL;
  if (window.trustedTypes && trustedTypes.createPolicy) {
      const policy = trustedTypes.createPolicy('myPolicy', {
          createScriptURL: (input) => {
              return input;
          }
      });
      trustedURL = policy.createScriptURL('https://cdnjs.cloudflare.com/ajax/libs/jspdf/1.3.2/jspdf.min.js');
  } else {
      trustedURL = 'https://cdnjs.cloudflare.com/ajax/libs/jspdf/1.3.2/jspdf.min.js';
  }

  // Load the jsPDF library using the trusted URL.
  const jspdf = document.createElement("script");
  jspdf.onload = downloadPDF;
  jspdf.src = trustedURL;
  document.body.appendChild(jspdf);
})();