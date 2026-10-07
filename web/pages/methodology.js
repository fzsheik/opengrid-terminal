/* /methodology and /methodology/:name.
   Doc pages are rendered server-side from methodology/*.md (api/pages.py, api/markdown.py); the client
   reuses the #ssr block on first load (or fetches the page's #ssr on in-app navigation) and only
   decorates it: a left table of contents from the headings, anchor links, scrollable tables.
   The index groups /v1/methodology by topic; one-line descriptions are each entry's `summary` (served by
   api/pages.py from methodology_index.methodology_summaries()), falling back to the first paragraph of the
   doc's raw markdown (/v1/methodology/{name}) only for entries without one. Unknown docs land in "Other". */
(() => {
  const { h } = OG;
  const GROUPS = [
    ["Market data & indices", ["data-kinds", "indices", "historical-context", "dispersion"]],
    ["Market structure", ["provider-value", "hardware", "families", "opportunities"]],
    ["Events & news", ["events", "news"]],
    ["Data trust & quality", ["data-quality", "data-trust"]],
    ["Routing & execution", ["routing", "best-execution"]],
    ["Accounts & billing", ["billing", "alerts", "accounts"]],
  ];
  const groupOf = name => (GROUPS.find(g => g[1].includes(name)) || ["Other"])[0];

  // First prose paragraph of a markdown doc, markup stripped: the one-line description.
  function firstParagraph(md) {
    const lines = String(md || "").split(/\r?\n/);
    const para = [];
    let inFence = false;
    for (const ln of lines) {
      if (/^\s*(```|~~~)/.test(ln)) { inFence = !inFence; if (para.length) break; continue; }
      if (inFence) continue;
      if (/^\s*$/.test(ln)) { if (para.length) break; continue; }
      if (/^\s*(#|\||>|[-*+]\s|\d+[.)]\s)/.test(ln)) { if (para.length) break; continue; }
      para.push(ln.trim());
    }
    const t = para.join(" ").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/\*\*|`/g, "").replace(/\*([^*\s][^*]*)\*/g, "$1").replace(/\s+/g, " ").trim();
    return t.length > 190 ? t.slice(0, 187).replace(/\s+\S*$/, "") + "…" : t;
  }
  const descCache = new Map();
  function describe(name) {
    if (!descCache.has(name)) descCache.set(name, OG.api.soft("/v1/methodology/" + encodeURIComponent(name)).then(md => (typeof md === "string" ? firstParagraph(md) : null)));
    return descCache.get(name);
  }

  async function ssrNodes(ctx) {
    if (ctx.ssr) return [...ctx.ssr.childNodes];
    const n = await OG.fetchSSR(ctx.path);
    return n ? [...n.childNodes] : [];
  }

  /* ---------- index ---------- */
  async function mountIndex(el, params, query, ctx) {
    const root = h("div", { class: "md-ix" });
    el.append(root);
    const ssr = ctx.ssr ? [...ctx.ssr.childNodes] : null;
    root.append(OG.head("Methodology", "How OpenGrid computes every number it publishes. Every endpoint cites its document in meta.methodology.",
      h("a", { class: "btn", href: "/v1/methodology", target: "_blank" }, "JSON"), h("a", { class: "btn", href: "/api" }, "API docs")));
    const body = h("div", {}, OG.loading("Loading documents…"));
    root.append(body);
    let docs;
    try { docs = await ctx.api("/v1/methodology"); }
    catch (e) { body.replaceChildren(...(ssr && ssr.length ? [h("div", { class: "methodology" }, ssr)] : [OG.error(e)])); return; }
    const groups = new Map(GROUPS.map(g => [g[0], []]));
    for (const d of docs) { const g = groupOf(d.name); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(d); }
    const descEls = new Map();
    const sections = [...groups].filter(([, ds]) => ds.length).map(([g, ds]) => {
      const order = (GROUPS.find(x => x[0] === g) || [, []])[1];
      ds.sort((a, b) => (order.indexOf(a.name) + 1 || 99) - (order.indexOf(b.name) + 1 || 99) || a.name.localeCompare(b.name));
      return h("section", { class: "md-g" }, h("h2", { class: "sec-h" }, g, h("span", { class: "md-n" }, String(ds.length))),
        h("ul", { class: "md-docs" }, ds.map(d => {
          const de = h("span", { class: "md-desc" }, d.summary || "…");
          descEls.set(d.name, de);
          return h("li", {}, h("a", { class: "md-doc", href: "/methodology/" + d.name },
            h("span", { class: "md-t" }, d.title), h("span", { class: "md-name" }, d.name), de));
        })));
    });
    body.replaceChildren(h("div", { class: "md-grid" }, sections),
      h("p", { class: "note" }, `${docs.length} documents. Data kinds (observed / inferred / estimated / transaction) and the four price concepts are defined in `,
        h("a", { class: "lnk", href: "/methodology/data-kinds" }, "data-kinds"), "; every figure on the site carries one of those labels."));
    await Promise.all(docs.filter(d => !d.summary).map(async d => {
      const t = await describe(d.name);
      if (!ctx.alive()) return;
      const de = descEls.get(d.name);
      if (de) { de.textContent = t || ""; de.classList.toggle("dimmer", !t); }
    }));
  }

  /* ---------- one document ---------- */
  async function mountDoc(el, params, query, ctx) {
    const root = h("div", { class: "md-page" });
    el.append(root);
    const article = h("article", { class: "methodology md-article" });
    const toc = h("nav", { class: "md-toc", "aria-label": "On this page" });
    const crumbs = h("div", { class: "md-crumbs" }, h("a", { class: "lnk", href: "/methodology" }, "Methodology"), h("span", { class: "dimmer" }, " / "), h("span", { class: "mono dim" }, params.name),
      h("span", { class: "spacer" }), h("span", { class: "md-group" }, groupOf(params.name)),
      h("a", { class: "btn sm", href: "/v1/methodology/" + encodeURIComponent(params.name), target: "_blank" }, "raw markdown"));
    root.append(crumbs, h("div", { class: "md-layout" }, toc, article));
    article.append(OG.loading());
    let nodes;
    try { nodes = await ssrNodes(ctx); }
    catch (e) { if (ctx.alive()) article.replaceChildren(OG.error(e)); return; }
    if (!ctx.alive()) return;
    if (!nodes.length) { article.replaceChildren(OG.empty("No document.")); return; }
    article.replaceChildren(...nodes);
    const h1 = article.querySelector("h1");
    if (h1) ctx.setTitle(h1.textContent + " · Methodology");
    decorate(article);
    buildToc(article, toc, ctx);
    if (location.hash) { const t = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (t) t.scrollIntoView({ block: "start" }); }
  }

  function decorate(article) {
    // anchor link on every heading (ids come from api/markdown.py's slugify; make duplicates unique)
    const seen = new Map();
    article.querySelectorAll("h2, h3, h4").forEach(hd => {
      let id = hd.id || hd.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      const n = seen.get(id) || 0; seen.set(id, n + 1);
      if (n) id += "-" + n;
      hd.id = id;
      hd.append(h("a", { class: "md-anchor", href: "#" + id, "aria-label": "Link to this section", title: "Link to this section" }, "#"));
    });
    // wide tables scroll inside their own box instead of the page
    article.querySelectorAll("table").forEach(t => {
      if (t.parentNode.classList && t.parentNode.classList.contains("md-tw")) return;
      const w = h("div", { class: "md-tw" });
      t.replaceWith(w); w.append(t);
    });
    // inline code that names an endpoint gets a copy affordance via title only (no behaviour change)
    article.querySelectorAll("code").forEach(c => { if (/^(GET|POST|PUT|DELETE)\s|^\/v1\//.test(c.textContent)) c.classList.add("md-ep"); });
  }

  function buildToc(article, toc, ctx) {
    const heads = [...article.querySelectorAll("h2, h3")];
    if (heads.length < 2) { toc.replaceChildren(h("div", { class: "md-toc-h" }, "On this page"), h("div", { class: "dimmer md-toc-e" }, "single section")); return; }
    const items = heads.map(hd => {
      const label = [...hd.childNodes].filter(n => !(n.classList && n.classList.contains("md-anchor"))).map(n => n.textContent).join("").trim();
      const a = h("a", { href: "#" + hd.id, class: "md-toc-a " + hd.tagName.toLowerCase(), onclick: e => { e.preventDefault(); hd.scrollIntoView({ block: "start" }); history.replaceState(history.state, "", location.pathname + location.search + "#" + hd.id); } }, label);
      return { hd, a };
    });
    toc.replaceChildren(h("div", { class: "md-toc-h" }, "On this page"), h("div", { class: "md-toc-l" }, items.map(i => i.a)),
      h("div", { class: "md-toc-f" }, h("a", { class: "lnk", href: "/methodology" }, "← all documents")));
    const scroller = document.querySelector(".main");
    if (!scroller) return;
    const onScroll = () => {
      const top = scroller.getBoundingClientRect().top + 60;
      let cur = items[0];
      for (const it of items) { if (it.hd.getBoundingClientRect().top <= top) cur = it; else break; }
      for (const it of items) it.a.classList.toggle("on", it === cur);
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    ctx.onCleanup(() => scroller.removeEventListener("scroll", onScroll));
    onScroll();
  }

  OG.page("/methodology", { title: "Methodology", mount: mountIndex });
  OG.page("/methodology/:name", { title: p => "Methodology · " + p.name, nav: "methodology", mount: mountDoc });
})();
