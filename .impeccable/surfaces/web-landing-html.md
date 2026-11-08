---
version: 1
slug: "web-landing-html"
primary_target: "web/landing.html"
related_targets: ["web/landing.css","web/landing.js"]
---

# Surface brief: homepage (/)

Scope: the public homepage at `/` (web/landing.html, landing.css, landing.js). Mode: Persuade.
Audience: ML engineers and AI startups first (what should I pay, where, from code); infra and finance buyers second (is this benchmark credible).
Job: in one viewport, show that OpenGrid measures the whole GPU compute market, live, and let the visitor look up a GPU and get a key.
Proof: real live data only: GPU x cloud price matrix (/internal/heatmaps/gpu-provider-premium), indices (/v1/indices), board (/internal/overview), events (/v1/events), fixings ledger (/v1/fixings). No invented customers, logos, or numbers.
Constraints: listed prices, not trades; execution is being validated, not live; market data is free; instant key is data:read.
Memorable moment: the opening grid lighting up, every square a real cloud's price.

## Direction contract

THESIS: The logo is a piece of the market. The homepage is the OpenGrid grid itself: a live matrix of exact GPUs by clouds, each square a real listing, lime where a cloud is cheaper than the rest, open outline where it lists nothing. It refuses both the centred AI-search hero and the dashboard-screenshot hero.
OWN-WORLD: Near-black with a green cast (#070a06 ground), brand lime #91C61D as the only chroma and only for cheaper-than-market data and the one primary action; dimmer greens step the scale; premiums fade toward a cool grey, never red. Square cells with 2px gaps, no radius above 2px, 1px hairlines. Archivo, expanded and heavy for headlines, normal width for text; JetBrains Mono only for prices, tickers, and code.
STORY: The visitor sees the market spread (one GPU, many prices), reads that OpenGrid prices it fairly (index, worked example, verifiable fixings), and takes a key or opens the markets.
FIRST VIEWPORT: Top bar with the four-square mark and wordmark, four links, "Get API key". Left third: H1 "The GPU market, priced." and a live sentence with the H100 range, index, and update time; a GPU search that filters the grid; key and markets actions. Right two thirds: the grid, full height, column labels for clouds, row labels for GPUs, a legend.
FORM: Grounded candidate 1 of 7 (the brand mark as live matrix); user pinned "what you think is best", so the pick overrides the roll's index. seed:19917ab7668c8686. Raises: darkroom (show the key's scope and limits before minting), Massin (type size carries magnitude in the live sentence and index), warm app (lime only where data is cheaper or something can be done), zine (explain how one index number is made, step by step, with its real constituents).
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
