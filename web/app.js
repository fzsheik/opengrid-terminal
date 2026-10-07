/* Superseded: the first web UI's market grid and GPU detail now live in web/pages/market.js and web/pages/gpu.js
   on the web/core.js framework. This file is kept so old bookmarks of /static/app.js still resolve; loaded
   on its own it sends old hash URLs (#/market, #/market/<gpu>) to their new paths. */
(() => {
  if (window.OG && window.OG.start) return;            // the new shell handles legacy hashes itself
  const h = location.hash.replace(/^#\/?/, "");
  if (!h) return;
  const parts = h.split("/");
  const slug = n => n.replace(/^(NVIDIA|AMD Instinct|AMD|Intel)\s+/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const to = parts[0] === "market" ? (parts[1] ? "/gpu/" + slug(decodeURIComponent(parts.slice(1).join("/"))) : "/gpus")
    : parts[0] === "routing" ? "/route" : parts[0] === "deploy" ? "/deployments" : null;
  if (to) location.replace(to);
})();
