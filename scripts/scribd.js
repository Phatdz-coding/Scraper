(pageSel) => {
    const describe = (node, cs) => {
        const r = node.getBoundingClientRect();
        const cls = (typeof node.className === 'string' && node.className.trim())
            ? '.' + node.className.trim().split(/\s+/).join('.') : '';
        return `${node.tagName.toLowerCase()}${node.id ? '#' + node.id : ''}${cls}` +
               ` [${cs ? cs.position : '?'}, top=${Math.round(r.top)}, h=${Math.round(r.height)}]`;
    };

    // Safeguard layer 1: mark and hide all fixed/sticky elements (except containers holding pages)
    window.__sxHide = () => {
        const pages = Array.from(document.querySelectorAll(pageSel));
        const hidden = [];
        for (const node of document.body.querySelectorAll('*')) {
            if (node.hasAttribute('data-sx-hidden')) continue;
            const cs = getComputedStyle(node);
            if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
            if (pages.some(p => node.contains(p))) continue;
            node.setAttribute('data-sx-hidden', '1');
            hidden.push(describe(node, cs));
        }
        return hidden;
    };

    // Main layer: make only the target page element `el` visible
    window.__sxIsolate = (el) => {
        document.querySelectorAll('[data-sx-target]').forEach(n => n.removeAttribute('data-sx-target'));
        el.setAttribute('data-sx-target', '1');
        document.body.classList.add('sx-isolate');
    };

    // Check: are there any remaining elements (outside the page) overlaying the page?
    window.__sxOverlays = (el) => {
        const r = el.getBoundingClientRect();
        const x0 = Math.max(r.left, 0), x1 = Math.min(r.right, window.innerWidth);
        const y0 = Math.max(r.top, 0),  y1 = Math.min(r.bottom, window.innerHeight);
        const found = new Set();
        for (let y = y0 + 2; y < y1; y += 30) {
            for (let x = x0 + 2; x < x1; x += 120) {
                const top = document.elementFromPoint(x, y);
                if (top && !el.contains(top) && !top.contains(el)) {
                    found.add(describe(top, getComputedStyle(top)));
                }
            }
        }
        return Array.from(found);
    };
};
