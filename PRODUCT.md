# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

ML engineers and AI startups (confirmed primary user). They need GPUs for training or inference and want the
cheapest one that fits the job, reached from their own code with one API key. They arrive wanting to try something
in minutes, not to talk to sales.

Secondary audiences that exist in the product but are not designed for first: infrastructure and finance buyers who
read prices, indices and history (Markets pages), and the OpenGrid operator (admin and ops tools).

## Product Purpose

OpenGrid reads prices, capacity and reliability from about 60 GPU clouds, turns them into one market, and routes a
workload to the best provider through one OpenGrid API key. Success: a developer gets a key, reads live prices from
code, and finds (and eventually runs on) the cheapest GPU that fits.

## Positioning

Lead claim (confirmed): **one key, every GPU cloud.** A single API key that reads and routes across ~60 GPU clouds to
the cheapest fit. Supporting it: OpenGrid's own clean, continuously collected market data (prices, capacity,
uptime, probes), which is what makes "cheapest" trustworthy.

## Operating Context

- The front door is the landing page at `/` (`web/landing.html`): an animated headline that types "Cheapest GPUs",
  live GPU prices and "OpenGrid"; one search bar answered by the OpenGrid Assistant (`POST /v1/assistant`, observed
  prices only); and "Generate API key", issued in one click with no sign-up (`POST /v1/instant-key`, data:read only).
- "Learn more" leads to `/get-started`, then the app: GPUs, Find Compute (workload → GPUs that fit), Markets,
  Developers (API docs, `/api`), My Jobs.
- Developers use the key with curl, the Python examples, the CLI and the hosted MCP endpoint.
- Light and dark themes; keyboard command bar in the app.

## Capabilities and Constraints

From the repository (`CAPABILITIES.md`); not separately confirmed in the interview, so treat as current facts to
re-check, not as permanent:

- Live market data from ~60 providers is public and free; the instant key is read-only market data.
- Execution is limited: real GPU runs are early access and approved one by one by OpenGrid; the only end-to-end
  training template is the PyTorch MNIST demo, which visitors see as a simulation.
- Workspaces (jobs, deployments, keys with more scopes) are invite-only.
- Some provider data is internal: used for routing, never shown or resold to customers.
- Terminology: "GPU-hour", "observed" price (not a quote), "estimate" is always labelled.

## Brand Commitments

- Name: OpenGrid (one word, capital G). Grid mark: a 3×3 line grid.
- Voice: plain, literal language; say what works and what does not; never claim more than the data shows.

## Evidence on Hand

- Real: live prices, provider count, indices, uptime and probe data from production.
- Absent: customer testimonials, logos, case studies, usage numbers. Future work must not invent them.

## Product Principles

1. Time to first key is the metric: anything between a developer and a working key must earn its place.
2. Cheapest means real: every price shown is observed market data, labelled, never a guess dressed as a quote.
3. One key is the product; the rest of the app explains and extends what that key can do.
4. Honest scope: show what runs today, mark what is invite-only or coming, never blur the two.
