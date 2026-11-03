// Load every page in headless Chrome. Prints one line per finding:
//   <page>\tERR <JS error | console error | HTTP status of a failed request>
//   <page>\tCOUNT prices=<$ amounts> rows=<table rows> bad_words=<"unavailable", "failed", ...>
// Args: base_url. Needs Google Chrome and `npm install` in this folder (puppeteer-core only drives it).
const puppeteer = require("puppeteer-core");
const base = process.argv[2];
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PAGES = ["/", "/markets", "/gpus", "/gpu/h100-80gb-sxm5", "/gpu/h100", "/providers", "/provider/lambda", "/provider/runpod",
  "/compare", "/compare/h100-80gb-sxm5-vs-h200-141gb-sxm5", "/explorer", "/indices", "/indices/h100-80gb-sxm5",
  "/opportunities", "/events", "/reserved", "/cost", "/capacity", "/uptime", "/probes", "/news", "/heatmaps",
  "/find", "/route", "/watchlists", "/api", "/developers", "/methodology", "/methodology/dispersion"];
(async () => {
  const browser = await puppeteer.launch({ executablePath: CHROME, headless: true });
  for (const path of PAGES) {
    const page = await browser.newPage();
    const errs = new Set();
    page.on("pageerror", e => errs.add("JS " + String(e.message).split("\n")[0]));
    page.on("console", m => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errs.add("console " + m.text().slice(0, 160)); });
    page.on("response", r => { const u = new URL(r.url()); if (r.status() >= 400 && !u.pathname.startsWith("/static")) errs.add(`${r.status()} ${u.pathname}${u.search}`); });
    try { await page.goto(base + path, { waitUntil: "networkidle0", timeout: 20000 }); } catch (e) { errs.add("NAV " + e.message.split("\n")[0]); }
    await new Promise(r => setTimeout(r, 1500));
    const t = await page.evaluate(() => document.body.innerText).catch(() => "");
    const rows = await page.evaluate(() => document.querySelectorAll("tbody tr").length).catch(() => 0);
    for (const e of [...errs].sort()) console.log(`${path}\tERR ${e}`);
    console.log(`${path}\tCOUNT prices=${(t.match(/\$\d+\.\d+/g) || []).length} rows=${rows} bad_words=${(t.match(/unavailable|went wrong|could not|couldn't|failed/gi) || []).length}`);
    await page.close();
  }
  await browser.close();
})();
