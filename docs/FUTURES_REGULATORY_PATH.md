# GPU price futures: what OpenGrid would have to do, and with whom

Written 2026-10-13 for the founder. **This is not legal advice.** It is an engineering founder's map of the
territory, so that the first conversation with a derivatives lawyer is short and well-aimed. Every path below
needs US commodities counsel (CFTC practice, not general startup counsel) before anything is offered to anyone.
Rules, thresholds and the market change; verify each point with counsel and the primary sources in §9.

---

## 1. The short answer

- **The regulator is the CFTC, not the SEC.** A futures contract or swap on GPU rental prices is a commodity
  derivative under the Commodity Exchange Act (CEA). "Commodity" in the CEA is very broad: it covers goods
  *and services* on which futures are or will be traded. A GPU-hour price index falls under the CFTC. The
  National Futures Association (NFA) is the CFTC's self-regulatory body that registers and audits
  intermediaries. The SEC matters only in the cases in §7.
- **OpenGrid cannot simply "add futures trading" to the website.** Offering futures to the public in the US
  requires a CFTC-designated exchange (a DCM). Customer money must go through a registered broker (an FCM).
  Trades must clear through a registered clearinghouse (a DCO).
- **The realistic path is to be the index, not the exchange.** An existing exchange lists the contract and
  OpenGrid licenses the settlement price. **This is exactly what is happening in the market right now** (§2).
  It is also why the settlement-grade fixings were built (methodology/settlement.md).

## 2. The market as of October 2026 (why timing matters)

- **CME Group and Silicon Data** announced (Aug 11, 2026) two cash-settled NYMEX futures:
  - the Silicon Data H100 Rental Index futures;
  - the Silicon Data B200 Rental Index futures.

  Each contract is one month of GPU rent. Launch was planned for **October 5, 2026**. Reporting since then says
  the **CFTC extended its review by 45 days to about November 9, 2026**, citing "novel or complex" questions:
  - how easily GPU rental prices can be manipulated in a fragmented market;
  - that those prices are set largely in private, bilateral deals.
- **ICE and Ornn** announced (May 19, 2026) cash-settled GPU compute futures on the **Ornn Compute Price Index
  (OCPI)**. They cover H100, H200, B200 and RTX 5090, subject to regulatory approval. Ornn describes OCPI as
  built *only from printed transactions*.
- **Architect Financial Technologies** has reportedly announced GPU and DRAM perpetual futures.

**What this means for OpenGrid:**
1. **The category is real.** The regulator's central question is the one the index provider must answer: *is
   the reference price reflective of the cash market and not readily subject to manipulation?*
2. **Two incumbents already have exchange partners.** OpenGrid would be a third index competing on quality.
3. **Ornn's "transactions only" claim is a pointed contrast.** OpenGrid's current indices are built from
   *advertised list prices* (methodology/indices.md). That is a real weakness for settlement; IOSCO prefers
   transaction data. **OpenGrid's structural advantage** is that its router executes real launches. Each
   reconciled execution is a *transacted* price, with provider evidence, at a known time. A transaction-based
   OpenGrid index is the strongest long-term differentiator (§8, step 6).

## 3. The five paths, cheapest first

### Path A — License the OpenGrid index to an existing exchange (recommended)

OpenGrid is a **benchmark administrator**. The exchange (CME, ICE, Cboe Futures Exchange, Coinbase Derivatives, or another DCM) designs the
contract and takes on all exchange, clearing and broker obligations. OpenGrid never touches customer money or
trades.

**Who you deal with:** the exchange's new-products and index-licensing teams. The CFTC deals with the exchange,
not with OpenGrid directly. The exchange files the contract with the CFTC, either by self-certification under
CFTC Rule 40.2 or by requesting approval under Rule 40.3.

**What the exchange will need from OpenGrid.** This is the core of the filing. For a contract cash-settled to an
index, the exchange must show the CFTC that the index is:
- reflective of the underlying cash market;
- not readily subject to manipulation or distortion;
- based on a reliable and acceptable price series;
- publicly available and timely.

See 17 CFR Part 38, Core Principle 3, and Appendix C. In practice the exchange will want:
1. **A frozen, versioned methodology** with a fixing schedule, a calculation, data sufficiency rules, a fallback
   waterfall and a restatement policy. Built: methodology/settlement.md.
2. **History.** Exchanges and the CFTC look at how the index behaved over time: volatility, gaps, constituent
   counts. OpenGrid's history only starts when recording began. Every month of clean fixings counts.
3. **An IOSCO Principles for Financial Benchmarks compliance statement** (19 principles: governance, quality,
   methodology, accountability), ideally with **independent limited assurance** from an audit firm. The
   self-assessment table in methodology/settlement.md is the starting point.
4. **Governance:**
   - an **oversight committee** that includes independent members (not built yet);
   - a written conflicts-of-interest policy (OpenGrid also routes compute and earns fees);
   - a complaints process;
   - change-notice procedures;
   - staff controls showing nobody can hand-edit inputs.
5. **Surveillance and manipulation controls**:
   - concentration limits;
   - detection of new listings that appear just before a fix;
   - feed-outage rules;
   - evidence retention so every fixing can be traced to the provider's own bytes.

   Built: the fixings ledger and its flags.
6. **Data rights.** Confirm that OpenGrid may use and redistribute derived prices from each provider's API or
   website. Review each provider's terms of service. This is a real legal item: an exchange will ask.
7. **Operational resilience**: publication SLAs, a disaster recovery plan, incident runbooks, and
   calculation-agent backup.
8. **A license agreement**: fees (per contract traded and/or a flat fee), exclusivity, liability caps, and
   index-correction obligations.

**Cost and time (rough, for planning only):**
- Months, not years, once an exchange is interested.
- Main costs: derivatives counsel, an independent IOSCO assurance review, and building the governance.
- No CFTC registration for OpenGrid itself.

**Other jurisdictions:** if EU or UK users will use the index, the EU Benchmarks Regulation and the UK BMR
apply. Ask counsel whether third-country or recognition rules apply.

### Path B — Physical "price-lock" reservations (built as requests only)

Here OpenGrid sells reserved compute at a fixed price: a commercial order for real GPU-hours, delivered through
OpenGrid. This is the product behind `/reserve` (methodology/reservations.md).

**Why it may sit outside futures regulation:**
- The CEA's definition of "swap" excludes a sale of a *nonfinancial commodity* for deferred delivery, as long as
  the transaction is *intended to be physically settled* (the forward contract exclusion).
- The CFTC also has a **trade option exemption** (17 CFR 32.3) for commodity options between commercial
  parties.

**The questions for counsel:**
1. Is a GPU-hour a "nonfinancial commodity" that can be physically delivered for this purpose?
2. Does "use up to N hours" flexibility create **embedded volumetric optionality**? The CFTC has a multi-factor
   test for whether that turns a forward into an option.
3. Must the contract be non-transferable and non-cash-settled? It should be both.

**Rules to keep it on the safe side (already reflected in the product design):**
- **Delivery:** physical delivery only. No cash settlement or payment of price differences, and no netting.
- **No resale:** no transfer or resale between customers, and no secondary market.
- **Customers:** commercial users who actually consume the compute.
- **Backing:** an offer only when backed by real provider capacity.
- **Paperwork:** standard order-form terms.

**Who you deal with:** your customers and providers (commercial contracts) and counsel. No CFTC registration if
the exclusion applies.

### Path C — Bilateral OTC swaps with large counterparties

Cash-settled swaps on the OpenGrid index, entered into directly with sophisticated counterparties, for example
a neocloud hedging its rental revenue, or a lab hedging its compute costs.

**What applies:**
- **Eligible counterparties only.** Swaps off an exchange are only legal between **eligible contract
  participants (ECPs)**: broadly, corporations with more than $10M in total assets, or more than $1M net worth
  and hedging business risk, plus regulated entities. Retail can only trade swaps on a DCM. (CEA §2(e); ECP
  definition in CEA §1a(18).)
- **Dealer registration.** Dealing in swaps above the **swap dealer de minimis threshold** (currently $8 billion
  aggregate gross notional over 12 months) requires registering as a **swap dealer**, with capital and
  business-conduct rules. Below it, reporting obligations still apply: trades must be reported to a swap data
  repository (17 CFR Parts 43 and 45).
- **No platform without registration.** If OpenGrid ran a platform where *multiple* participants trade swaps
  with each other, that is a **swap execution facility (SEF)** and needs CFTC registration (17 CFR Part 37).
- **Intermediaries.** If OpenGrid arranges trades or takes orders for others, it may need NFA registration as an
  **introducing broker**, or handle funds only through an FCM.

**Who you deal with:** counsel, the ISDA documentation of each counterparty, a swap data repository, and possibly
the NFA.

### Path D — Become a futures exchange (DCM) yourself

- **Exchange.** Apply for designation as a contract market (17 CFR Part 38, **Form DCM**). You must demonstrate
  all **23 core principles** (CEA §5(d)):
  - market surveillance;
  - contracts not readily susceptible to manipulation;
  - position limits and accountability;
  - financial integrity;
  - disciplinary procedures;
  - system safeguards;
  - **financial resources covering at least one year of operating costs**;
  - and the rest.

  The CFTC's statutory review is **180 days from a materially complete application**. In practice, preparation
  plus review runs much longer.
- **Clearing:** register a **DCO** (17 CFR Part 39), or contract with an existing clearinghouse to clear the
  contracts.
- **Brokers:** members must trade through **FCMs** (registered brokers holding customer funds), plus **NFA**
  membership for the relevant entities.
- **Reality:** years and many millions of dollars of legal, compliance, technology and capital. Only worth it
  with a large, proven, liquid product, and incumbents already exist. **Not recommended.**

### Path E — Offshore or crypto "perps" offered to US users — don't

Offering leveraged or perpetual GPU-price contracts to US persons from an unregistered venue is the classic
pattern the CFTC brings enforcement actions against: an unregistered DCM or FCM, and illegal off-exchange retail
commodity transactions. Tokens can add SEC exposure on top. Do not build this.

## 4. Obligations OpenGrid has *today*, even without futures

- **Anti-manipulation applies to the index now.** The CFTC's anti-fraud and anti-manipulation authority reaches
  benchmarks that affect commodity prices in interstate commerce. Examples: CEA §6(c)(1) and Rule 180.1, and
  false reporting under §9(a)(2). The LIBOR and energy-index cases are the precedent.
  - Never allow anyone, staff included, to influence fixings.
  - Keep the evidence (the fixings ledger).
  - Disclose conflicts.
- **Marketing language.** Don't call the indices "official", "regulated" or "approved". Don't call reservations
  "futures", "hedges" or "tradable" until a licensed product exists.
- **Provider terms of service:** see Path A, item 6.

## 5. Conflicts of interest specific to OpenGrid (an exchange and the CFTC will ask)

OpenGrid both **publishes prices** and **routes paid workloads** (and earns fees on them). Controls to state,
and keep true:
- The index is computed mechanically from observed public provider prices.
- OpenGrid's own routed or quoted prices and fees never enter the index.
- Inputs cannot be edited by staff. Every change is logged, and fixings are hash-chained.
- Methodology changes require a version, notice and committee approval (planned).
- If OpenGrid ever trades or hedges compute itself, wall it off from index operations.

## 6. The SEC: when it would matter

**The SEC is not the regulator for GPU-price futures or swaps.** It becomes relevant only if:
- the contracts reference **securities**, for example GPU company stocks or a narrow-based security index
  (security-based swaps are SEC);
- OpenGrid issues **tokens** or similar instruments that could be investment contracts;
- OpenGrid offers investors pooled exposure, such as a fund (also CFTC commodity pool rules);
- OpenGrid raises capital: ordinary securities law for the fundraising itself (Reg D, etc.), unrelated to the
  product.

## 7. Who's who

| Body | Role for OpenGrid |
|---|---|
| **CFTC** (Commodity Futures Trading Commission) | Regulates futures, options on futures and swaps on commodities, including compute. Approves exchanges, clearinghouses and swap facilities. Reviews contracts the exchanges list. Enforces anti-manipulation rules |
| **NFA** (National Futures Association) | Registers and audits FCMs, introducing brokers, CPOs, CTAs and swap dealers |
| **Exchanges** (DCMs: CME/NYMEX, ICE, Cboe Futures, Coinbase Derivatives, …) | List the contract, file it with the CFTC, run surveillance. **Your counterparty in Path A** |
| **Clearinghouses** (DCOs) | Guarantee performance of cleared trades |
| **SEC** | Only in the §6 cases |
| **IOSCO** | Not a regulator. Its 2013 *Principles for Financial Benchmarks* are the standard exchanges and regulators measure an index against |
| **EU ESMA / UK FCA** | Benchmark regulation if the index is used in EU or UK financial instruments |

## 8. Founder's action plan

1. **Now (no regulatory exposure):**
   - Run the settlement fixings in production and let history accumulate. Every clean month is evidence.
   - Publish methodology/settlement.md publicly.
2. **Weeks 1–4:** hire **CFTC derivatives counsel** for a scoping call. Bring this document and
   methodology/settlement.md. Ask:
   - (a) Path A requirements;
   - (b) whether Path B reservations fall under the forward exclusion, and what the volumetric-optionality risk
     is;
   - (c) what the provider terms of service allow for price redistribution.
3. **Months 1–3:**
   - Form an **index oversight committee** with at least one independent member.
   - Adopt conflicts, complaints and change-control policies. Close the gaps in the IOSCO self-assessment.
4. **Months 2–4:** commission **independent limited assurance** on IOSCO compliance from an audit firm.
5. **Then:** approach exchanges. The pitch:
   - breadth (many providers, hourly);
   - reproducibility (every fixing verifiable down to provider bytes, with a hash-chained ledger);
   - manipulation controls.

   Expect them to compare OpenGrid to Silicon Data and Ornn, and to ask about transaction data.
6. **Differentiator:**
   - Build a **transaction-based OpenGrid index** from reconciled routed executions, published alongside the
     list-price index once volume is meaningful.
   - Customer consent and anonymisation are required. Counsel should review the data rights.
7. **Only with counsel sign-off:**
   - make Path B reservations binding;
   - consider Path C swaps with ECP counterparties.
8. **Never without registration:** retail trading, a multi-party trading venue, holding customer margin, or
   offshore perps.

## 9. Sources (check before relying on them)

- CME Group press release, Aug 11 2026: "CME Group and Silicon Data to Launch Compute Futures on October 5":
  https://www.cmegroup.com/media-room/press-releases/2026/8/11/cme_group_and_silicondatatolaunchcomputefuturesonoctober5tounloc.html
- CME notice SER-9785 and the update SER-9785R: https://www.cmegroup.com/notices/ser/2026/08/ser-9785.html,
  https://www.cmegroup.com/notices/ser/2026/09/ser-9785r.html
- CME contract page: https://www.cmegroup.com/markets/energy/electricity/silicon-data-h100-rental-index.html
- Reporting on the CFTC review extension (secondary sources; confirm on cftc.gov):
  https://valueaddvc.com/pulse/cme-gpu-futures-cftc-delay-2026 ,
  https://finimize.com/content/cmes-nvidia-gpu-futures-hit-a-regulatory-speed-bump
- ICE / Ornn announcement, May 19 2026:
  https://ir.theice.com/press/news-details/2026/ICE-and-Ornn-to-Launch-GPU-Compute-Futures-Contracts/default.aspx
- CFTC, DCM designation: https://www.cftc.gov/IndustryOversight/TradingOrganizations/DCMs/dcmhowto.html
- 17 CFR Part 38 (DCMs, incl. Appendix C on cash-settled contracts): https://www.ecfr.gov/current/title-17/chapter-I/part-38
- 17 CFR Part 40 (listing by certification 40.2 / approval 40.3): https://www.ecfr.gov/current/title-17/chapter-I/part-40
- CFTC listing procedures: https://www.cftc.gov/IndustryOversight/ContractsProducts/ListingProcedures/index.htm
- IOSCO, Principles for Financial Benchmarks (2013): https://www.iosco.org/library/pubdocs/pdf/IOSCOPD415.pdf
- Index competitors: Silicon Data https://www.silicondata.com/products/silicon-index , Ornn https://data.ornn.com/
