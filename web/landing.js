/* The landing page at "/" (web/landing.html).
   1. The headline types "Cheapest GPUs", erases it, types "OpenGrid", and repeats. The grid mark beside it draws
      its lines in step with the letters and erases with them.
   2. "Generate API key" calls POST /v1/instant-key (api/instant_key.py) and shows the key in the bar; the round
      button then copies it, and "Learn more" goes into the app (/get-started). */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const PHRASES = ["Cheapest GPUs", "OpenGrid"];
  const TYPE_MS = 85, ERASE_MS = 40, HOLD_MS = 2200, GAP_MS = 450;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function headline() {
    const out = $("lp-type"), caret = document.querySelector(".lp-caret"), mark = $("lp-mark");
    const lines = [...mark.querySelectorAll("path")].map(p => {
      const len = p.getTotalLength();
      p.style.strokeDasharray = String(len);
      p.style.strokeDashoffset = String(len);
      return { p, len };
    });
    // progress 0..1 across the phrase -> each of the six lines draws in turn
    const draw = f => {
      const n = lines.length;
      lines.forEach(({ p, len }, i) => {
        const part = Math.min(1, Math.max(0, f * n - i));
        p.style.strokeDashoffset = String(len * (1 - part));
      });
      mark.classList.toggle("on", f >= 1);
    };
    if (still) { out.textContent = PHRASES[0]; draw(1); return; }

    const wait = ms => new Promise(r => setTimeout(r, ms));
    let k = 0;
    (async function loop() {
      out.textContent = "";
      draw(0);
      await wait(300);
      for (;;) {
        const word = PHRASES[k % PHRASES.length];
        caret.classList.add("typing");
        for (let i = 1; i <= word.length; i++) {
          out.textContent = word.slice(0, i);
          draw(i / word.length);
          await wait(TYPE_MS + (Math.random() * 40 - 20));
        }
        caret.classList.remove("typing");
        await wait(HOLD_MS);
        caret.classList.add("typing");
        for (let i = word.length - 1; i >= 0; i--) {
          out.textContent = word.slice(0, i);
          draw(i / word.length);
          await wait(ERASE_MS);
        }
        caret.classList.remove("typing");
        await wait(GAP_MS);
        k++;
      }
    })();
  }

  function keyBar() {
    const bar = $("lp-bar"), gen = $("lp-gen"), go = $("lp-go"), keyEl = $("lp-key");
    const err = $("lp-err"), sub = $("lp-sub"), after = $("lp-after"), curl = $("lp-curl"), live = $("lp-live");
    let secret = null;

    const state = s => { bar.dataset.state = s; };

    async function generate() {
      if (bar.dataset.state === "busy") return;
      state("busy");
      err.hidden = true;
      gen.textContent = "Generating…";
      try {
        const r = await fetch("/v1/instant-key", {
          method: "POST",
          headers: { "X-OpenGrid-Request": "1", "Accept": "application/json" },
          credentials: "same-origin",
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.detail || body.message || (body.error && body.error.message) || "Couldn't make a key right now. Try again.");
        secret = body.data && body.data.secret;
        if (!secret) throw new Error("Couldn't make a key right now. Try again.");
      } catch (e) {
        state("error");
        gen.textContent = "Generate API key";
        err.textContent = e.message || "Couldn't make a key right now. Try again.";
        err.hidden = false;
        return;
      }
      gen.hidden = true;
      keyEl.hidden = false;
      keyEl.textContent = secret;
      go.setAttribute("aria-label", "Copy API key");
      sub.textContent = "Your key. Access every GPU cloud.";
      curl.textContent = `curl ${location.origin}/v1/gpus \\\n  -H "Authorization: Bearer ${secret}"`;
      after.hidden = false;
      state("done");
      live.textContent = "Your API key is ready. Press the copy button to copy it.";
      copy(true);
    }

    async function copy(quiet) {
      if (!secret) return;
      try {
        await navigator.clipboard.writeText(secret);
      } catch (_) {
        // no clipboard permission: select the key so the visitor can copy it by hand
        const sel = getSelection(), range = document.createRange();
        range.selectNodeContents(keyEl);
        sel.removeAllRanges();
        sel.addRange(range);
        if (quiet) return;
      }
      state("copied");
      go.setAttribute("aria-label", "Copied");
      if (!quiet) live.textContent = "Copied.";
      clearTimeout(copy.t);
      copy.t = setTimeout(() => { state("done"); go.setAttribute("aria-label", "Copy API key"); }, 1600);
    }

    gen.addEventListener("click", generate);
    go.addEventListener("click", () => (secret ? copy(false) : generate()));
    bar.addEventListener("click", e => { if (!secret && e.target === bar) generate(); });
  }

  headline();
  keyBar();
})();
