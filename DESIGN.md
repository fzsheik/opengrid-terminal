---
name: OpenGrid
description: The GPU market, priced. A live matrix of exact GPUs by clouds, on near-black, in one lime.
colors:
  lime: "#91c61d"
  lime-hi: "#b4e04a"
  lime-ink: "#0a0f04"
  scale-cheaper: "#91c61d"
  scale-lower: "#5f8a16"
  scale-par: "#3a4f1c"
  scale-pricier: "#2d3328"
  scale-priciest: "#1f241b"
  cell-open: "#263020"
  ground: "#070906"
  ground-2: "#0b0e09"
  ground-3: "#11160e"
  hairline: "#1b2216"
  hairline-2: "#2a3422"
  text: "#eef2e9"
  text-2: "#c2cbb9"
  muted: "#949f8b"
typography:
  display:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(46px, 5vw, 80px)"
    fontWeight: 820
    lineHeight: 0.94
    letterSpacing: "-0.035em"
    fontVariation: "'wdth' 118"
  headline:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontSize: "clamp(32px, 3.6vw, 52px)"
    fontWeight: 800
    lineHeight: 1.02
    letterSpacing: "-0.03em"
    fontVariation: "'wdth' 112"
  title:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Archivo, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 80"
  figure:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "clamp(48px, 5vw, 76px)"
    fontWeight: 600
    lineHeight: 1
    letterSpacing: "-0.04em"
    fontFeature: "'tnum' 1"
  data:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.55
    fontFeature: "'tnum' 1"
  code:
    fontFamily: "JetBrains Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.7
rounded:
  cell: "1px"
  control: "2px"
spacing:
  gap: "3px"
  gap-mobile: "2px"
  cell: "16px"
  cell-mobile: "13px"
  page: "clamp(16px, 4vw, 56px)"
  section: "clamp(64px, 8vw, 112px)"
components:
  button-primary:
    backgroundColor: "{colors.lime}"
    textColor: "{colors.lime-ink}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.lime-hi}"
    textColor: "{colors.lime-ink}"
  button-primary-big:
    backgroundColor: "{colors.lime}"
    textColor: "{colors.lime-ink}"
    rounded: "{rounded.control}"
    padding: "0 24px"
    height: "52px"
  button-primary-disabled:
    backgroundColor: "{colors.ground-3}"
    textColor: "{colors.muted}"
  button-line:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "44px"
  input-search:
    backgroundColor: "{colors.ground-2}"
    textColor: "{colors.text}"
    typography: "{typography.data}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "50px"
  panel-key:
    backgroundColor: "{colors.ground-2}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "26px"
  tooltip:
    backgroundColor: "{colors.ground-3}"
    textColor: "{colors.text-2}"
    rounded: "{rounded.control}"
    padding: "10px 12px"
  grid-cell:
    backgroundColor: "{colors.scale-cheaper}"
    rounded: "{rounded.cell}"
    size: "{spacing.cell}"
  tab:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    padding: "10px 14px"
    height: "44px"
  tab-selected:
    textColor: "{colors.text}"
---

# Design System: OpenGrid

## Overview

**Creative North Star: "The Mark Is a Piece of the Market"**

The OpenGrid mark, a 2×2 of lime squares with the top-right square left open, is not decoration placed on top of the product; it is a four-cell crop of the product's own price grid. Every surface in this world is built from that same material: square cells, 1px hairlines, a near-black ground with a green cast, and one lime that means "cheaper than the rest" or "do this". Filled square, dim square, open outline: those three states carry price, status, and list bullets alike.

Density is high where data lives and generous where reading happens. The live grid packs cells three pixels apart; prose sections breathe on wide section padding and narrow measures (36 to 72ch). Type does the hierarchy work through Archivo's width axis: headlines run expanded and heavy, labels run condensed and quiet, and JetBrains Mono carries every number a reader might compare. Type size also carries magnitude: in the H100 spread sentence the high price is set larger in proportion to how far apart the prices are.

The world is dark only. Premiums fade toward a dim grey-green and never turn red; there is no alarm colour because a higher listed price is information, not danger.

**Key Characteristics:**
- Near-black green-cast ground, one chroma (lime), no red anywhere.
- Square cells and 1px hairlines; no corner radius above 2px.
- Filled / dim / open-outline squares as the single state vocabulary.
- Archivo variable, width axis wide for headlines and narrow for labels; JetBrains Mono only for prices, tickers and code.
- Flat: depth by tonal steps of the ground, one soft shadow for the floating tooltip only.

## Colors

One green-cast neutral family and one lime, with a stepped price scale between them.

### Primary
- **Grid Lime** (lime): the brand green. Fills the cheapest cells, the low end of a price range, falling index changes, "new low" events, the "Live" status square, and the primary action button. Also marks interaction: focus ring, text selection, link underline on hover, hover colour on table links and the search submit.
- **Lit Lime** (lime-hi): hover state of the primary button only.
- **Lime Ink** (lime-ink): text on lime surfaces (buttons, selection, skip link).

### The Price Scale
The grid's five-step scale, brightest is cheapest relative to the other clouds (thresholds in the build: at least 25% under the others, 8 to 25% under, within ±8%, 8 to 60% over, more than 60% over).
- **Cheaper** (scale-cheaper): same value as Grid Lime.
- **Lower** (scale-lower): a mid moss green; also the filled bullet square and the outline of the "being tested" status square.
- **At Par** (scale-par): dark olive; "about the same" and the fill of the "being tested" status square.
- **Pricier** (scale-pricier) and **Priciest** (scale-priciest): grey-greens that sink toward the ground. Premiums fade out; they never warm up.
- **Open Cell** (cell-open): the 1px inset outline of a cloud that lists nothing.

### Neutral
- **Ground** (ground): page background, sticky row labels, and the fill behind outlined index-strip points.
- **Ground 2** (ground-2): the key panel, the search field, table row hover.
- **Ground 3** (ground-3): tooltip surface and disabled button.
- **Hairline** (hairline): section rules, table rows, top bar and footer borders.
- **Hairline 2** (hairline-2): control borders (outline button, search, key panel, tabs rail), step rules, scrollbar thumb.
- **Text** (text): headings, figures, primary copy.
- **Text 2** (text-2): ledes, body paragraphs, nav links, row labels.
- **Muted** (muted): captions, legend, column labels, placeholders, timestamps.

### Named Rules
**The One Chroma Rule.** Lime is the only hue with chroma. It appears where something is cheaper than the market or where the reader can act (the primary button, focus, hover). Everything else is the green-grey neutral family.

**The No-Red Rule.** A higher price fades toward the ground; it never turns red, orange or warm. The world has no alarm colour.

## Typography

**Display Font:** Archivo variable (weight 100 to 900, width 62% to 125%), with ui-sans-serif, system-ui fallback
**Body Font:** Archivo at normal width
**Label/Mono Font:** JetBrains Mono variable (400 to 700), with ui-monospace, SF Mono, Menlo fallback

**Character:** One grotesque stretched across its width axis does all the voice work: expanded and heavy it shouts the headline, condensed it whispers the axis labels. The mono is the ledger: anything a reader would compare or copy is set in it, with tabular figures.

### Hierarchy
- **Display** (820, clamp(46px, 5vw, 80px), 0.94, width 118%): the single H1 only.
- **Headline** (800, clamp(32px, 3.6vw, 52px), 1.02, width 112%): section heads, written as short declarative sentences ending in a full stop. The closing statement scales up to clamp(36px, 5vw, 72px) at 14ch.
- **Title** (700, 18px, width 100%): step names, panel titles (20px in the key panel).
- **Body** (400, 17px, 1.55; 16px under 640px): paragraphs at 36 to 72ch, ledes at 18px in Text 2.
- **Label** (500, 12 to 13px, width 78 to 88%): grid column and row labels, index-strip labels, event types. Sentence case, never uppercase-tracked.
- **Figure** (mono 600, clamp(48px, 5vw, 76px), -0.04em, tabular): the composite index level. The hero spread uses mono 600 at clamp(24px, 2vw, 30px).
- **Data** (mono, tabular): table prices (19px 600 for the index column), tooltip price (18px), event timestamps (13px), the search input (16px).
- **Code** (mono 13.5px, 1.7): code samples, with keys in Text and comments in Muted.

### Named Rules
**The Width Axis Rule.** Hierarchy comes from Archivo's width axis as much as from size: wide (112 to 118%) for H1 and H2, normal (100%) for text, narrow (78 to 88%) for dense labels. Do not reach for a second display family.

**The Ledger Rule.** JetBrains Mono is for prices, tickers, timestamps, step numerals and code, always with tabular figures. Prose and headings never use it.

## Layout

A full-bleed page with a fluid side gutter (clamp(16px, 4vw, 56px)) and no centred max-width container; content blocks cap themselves by measure instead (section heads at 720px, prose at 60 to 72ch). Sections stack on clamp(64px, 8vw, 112px) top padding and are often separated by full-width hairlines.

The first viewport is a two-column grid, copy at about one third (minmax(300px, 0.78fr)) and the market grid at two thirds (1.5fr). Secondary two-up blocks (index level beside its chart, key panel beside code) use the same narrow-left, wide-right split (0.6 to 0.8fr against 1.4fr).

Lists of facts are hairline-ruled rows, not cards: index table, market events (92px time / 150px type / text), the status list (200px term / description), and auto-fit steps (min 210px) each topped by a rule.

Responsive: at 1100px and below every two-up collapses to one column, and the hero reorders so the grid follows the spread sentence and precedes the actions and search. At 640px and below the cell shrinks to 13px with a 2px gap, nav links wrap to a full-width row under a hairline, events and status rows stack, and wide table columns and sparklines drop. The grid scrolls sideways inside its own frame with sticky row labels; it never shrinks cells below 13px.

## Elevation & Depth

Flat. Depth is carried by three tonal steps of the ground (ground, ground-2, ground-3) and by hairlines, not by shadow. The only shadow is the floating tooltip's, which has to separate from a dense grid beneath it. Grid cells use inset 1px box-shadows as outlines, which are borders, not elevation.

### Shadow Vocabulary
- **Tooltip float** (`box-shadow: 0 10px 30px -8px rgba(0, 0, 0, 0.7)`): the hover/focus tooltip over the grid only.

### Named Rules
**The Hairline-Not-Shadow Rule.** Separate things with a 1px hairline or a one-step change of ground. A new shadow needs the tooltip's reason: something floating over live data.

## Shapes

Square. Cells are 1px-rounded squares, controls and panels are 2px, and nothing exceeds 2px. Borders are always 1px. The recurring silhouette is the square in three states: filled (a value), dimmed (a lesser value), and open, drawn as an inset 1px outline (nothing there). The mark, legend swatches (12px), status squares (12px) and list bullets (10px) all reuse it, so the brand shape and the data shape are the same object.

Line icons, where needed, are inline SVG strokes at 2px with round caps (the search arrow); the mark's open corner is a 3px square-capped stroke.

## Components

### Buttons
Blunt, solid, almost square.
- **Shape:** near-square corners (2px), 1px border, min height 44px (52px for the big key button), Archivo 650 at 15px.
- **Primary:** lime fill with lime-ink text. One per view region: the top bar key link, the hero key action, the generate-key button, the closing action.
- **Hover / Focus:** fill lifts to lime-hi over 180ms on the expo-out ease; focus is a 2px lime outline at 2px offset.
- **Line:** transparent with a hairline-2 border and Text label; hover brightens the border to Muted. Used for the secondary action beside a primary, and for Copy.
- **Disabled:** ground-3 fill, Muted text, hairline-2 border.

### Inputs / Fields
- **Style:** a single bordered row (hairline-2, ground-2, 2px) holding a mono input at 50px and a 50px square submit cell split off by a hairline. Label above in 14px Muted.
- **Focus:** the whole row's border turns lime; the input itself draws no ring.
- **Messages:** a reserved line below in 14px Muted, announced politely.

### Cards / Containers
The key panel is the only boxed container: ground-2, hairline-2 border, 2px, 26px padding. Everything else is ruled rows on the open ground.

### Tabs
Text tabs on a hairline-2 rail; 14px Muted, selected tab in Text with a 2px Text underline that overlaps the rail. No lime: selection is not a price.

### Navigation
Top bar: the mark and lowercase wordmark (Archivo 800, 21px, width 108%) left, four text links in Text 2 (15px, 44px targets) pushed right, then the primary key button. One hairline beneath. Footer repeats the smaller brand (18px, 22px mark) with a wrapped link row in Text 2.

### The Market Grid (signature)
A table of exact GPUs (rows) by clouds (columns). Each cell is a 16px square at a 3px gap, filled from the price scale, or open-outlined when the cloud lists nothing. Column labels are condensed 12px Muted, rotated vertical, reading bottom to top; row labels are condensed 13px Text 2, right-aligned and sticky. Hover or focus rings the cell 2px in Text, scales it 1.12, and lifts its row and column labels to Text; search dims non-matching rows to 20%. On load the market switches on cloud by cloud, left to right (opacity over 500ms, staggered 22ms per column and 6ms per row); reduced motion shows it lit at once. A legend of 12px swatches sits under it with the as-of time right-aligned.

### Index Strip
A worked index drawn on its real constituents: included prices as lime dots, trimmed prices as ground-filled moss outlines, held-out prices as dashed Muted outlines, a faint lime band for the trimmed range, and the level as a 2px Text line. Mono numerals, condensed sans labels.

## Do's and Don'ts

### Do:
- **Do** use lime only for cheaper-than-market data, the primary action, and interaction feedback (focus, hover, selection).
- **Do** express state with the square vocabulary: filled, dimmed, or open 1px outline.
- **Do** set every comparable number in JetBrains Mono with tabular figures.
- **Do** carry hierarchy through Archivo's width axis: 112 to 118% for headlines, 78 to 88% for dense labels.
- **Do** separate content with 1px hairlines and ground steps; rule lists as rows.
- **Do** keep touch targets at 44px minimum, and let the grid scroll sideways rather than shrink cells below 13px.

### Don't:
- **Don't** use red, orange, or any warm hue for a higher price; premiums fade toward the ground.
- **Don't** round anything above 2px.
- **Don't** add shadows for elevation; the tooltip float is the only one.
- **Don't** introduce a second accent hue or a second display family.
- **Don't** use mono for prose or headings, or uppercase tracked labels above headings.
- **Don't** reuse the logged-in app's tokens here: the app (web/app.css, web/index.html) has its own incumbent terminal system (blue accent, system fonts, light and dark themes) that is not yet aligned with this world.
