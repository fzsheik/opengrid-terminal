"""Which public sources we watch, hand-maintained like canonical.py.

Rules for adding a source:
    - an official feed or API (RSS, Atom, JSON Feed, the Federal Register API). No HTML
      scraping: a page that changes shape silently breaks, and some sites forbid it.
    - verified from a real fetch before it is enabled. Anything we could not fetch is
      listed disabled with the reason, so the gap is visible rather than forgotten.

Verified 2026-10-06 from the dev machine (HTTP status, feed type, item count).

Fields
    kind          rss | atom | json_api (JSON Feed) | federal_register
                  rss and atom share one parser that sniffs the root element, so a feed
                  that switches format does not break.
    category      provider | vendor | hyperscaler | trade_press | regulation | markets
    topics        default topics every item from this source carries (e.g. export_controls
                  for BIS rules); classification adds more from the text.
    entity_hints  {"provider": id} for a provider's own blog: its posts are about it even
                  when the text never names it.
    trust_tier    official (the company / agency itself) | press | analysis
"""

from dataclasses import dataclass, field

_FR = ("https://www.federalregister.gov/api/v1/documents.json?per_page=50&order=newest"
       "&fields[]=title&fields[]=abstract&fields[]=html_url&fields[]=publication_date"
       "&fields[]=document_number&fields[]=type&fields[]=agencies")


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    kind: str
    category: str
    trust_tier: str
    poll_seconds: int = 3600
    topics: tuple = ()
    entity_hints: dict = field(default_factory=dict)
    enabled: bool = True
    note: str = ""
    max_items: int = 100   # newest N per fetch; a 3000-item archive feed is not news
    # "url": an article is its canonical link (the default). "guid": for feeds whose entries all
    # link to one page (release notes: one URL, a fragment per day), the entry id is the identity.
    key: str = "url"


H = 3600
_ALL = [
    # ---------------------------------------------------------------- hyperscalers
    Source("aws_whats_new", "AWS What's New", "https://aws.amazon.com/about-aws/whats-new/recent/feed/",
           "rss", "hyperscaler", "official", 30 * 60, entity_hints={"provider": "aws"}),
    Source("aws_news_blog", "AWS News Blog", "https://aws.amazon.com/blogs/aws/feed/",
           "rss", "hyperscaler", "official", H, entity_hints={"provider": "aws"}),
    Source("aws_compute_blog", "AWS Compute Blog", "https://aws.amazon.com/blogs/compute/feed/",
           "rss", "hyperscaler", "official", 3 * H, entity_hints={"provider": "aws"}),
    Source("aws_ml_blog", "AWS Machine Learning Blog", "https://aws.amazon.com/blogs/machine-learning/feed/",
           "rss", "hyperscaler", "official", 3 * H, entity_hints={"provider": "aws"}),
    Source("gcp_blog_compute", "Google Cloud Blog: Compute", "https://cloudblog.withgoogle.com/products/compute/rss/",
           "rss", "hyperscaler", "official", H, entity_hints={"provider": "gcp"}),
    Source("gcp_blog_ai", "Google Cloud Blog: AI & ML", "https://cloudblog.withgoogle.com/products/ai-machine-learning/rss/",
           "rss", "hyperscaler", "official", 3 * H, entity_hints={"provider": "gcp"}),
    Source("gcp_compute_release_notes", "Google Compute Engine release notes",
           "https://cloud.google.com/feeds/compute-release-notes.xml",
           "atom", "hyperscaler", "official", 3 * H, entity_hints={"provider": "gcp"}, key="guid",
           note="redirects to docs.cloud.google.com; every entry links to one page, so entries key on their id"),
    Source("azure_blog", "Microsoft Azure Blog", "https://azure.microsoft.com/en-us/blog/feed/",
           "rss", "hyperscaler", "official", H, entity_hints={"provider": "azure"}),
    Source("azure_updates", "Azure Updates", "https://www.microsoft.com/releasecommunications/api/v2/azure/rss",
           "rss", "hyperscaler", "official", H, entity_hints={"provider": "azure"}),
    Source("microsoft_blog", "Official Microsoft Blog", "https://blogs.microsoft.com/feed/",
           "rss", "hyperscaler", "official", 3 * H, note="capex and datacenter announcements"),
    Source("oci_blog", "Oracle Cloud Infrastructure Blog", "https://blogs.oracle.com/cloud-infrastructure/rss",
           "rss", "hyperscaler", "official", 3 * H, entity_hints={"provider": "oci"}, enabled=False,
           note="returned 200 (a ~3000-item archive) on first check, then HTTP 403 to every client on re-check; "
                "disabled until it is reliably reachable"),
    Source("meta_newsroom", "Meta Newsroom", "https://about.fb.com/feed/",
           "rss", "hyperscaler", "official", 6 * H, note="capex / datacenter buildout"),
    Source("google_ai_blog", "Google: The Keyword (AI)", "https://blog.google/technology/ai/rss/",
           "rss", "hyperscaler", "official", 6 * H),
    # ---------------------------------------------------------------- neoclouds / providers
    Source("lambda_blog", "Lambda Blog", "https://lambda.ai/blog/rss.xml",
           "rss", "provider", "official", H, entity_hints={"provider": "lambda"}),
    Source("coreweave_blog", "CoreWeave Blog", "https://www.coreweave.com/blog/rss.xml",
           "rss", "provider", "official", H, entity_hints={"provider": "coreweave"}),
    Source("runpod_blog", "RunPod Blog", "https://www.runpod.io/blog/rss.xml",
           "rss", "provider", "official", H, entity_hints={"provider": "runpod"}),
    Source("together_blog", "Together AI Blog", "https://www.together.ai/blog/rss.xml",
           "rss", "provider", "official", H, entity_hints={"provider": "together"}),
    Source("voltagepark_blog", "Voltage Park Blog", "https://www.voltagepark.com/blog/rss.xml",
           "rss", "provider", "official", H, entity_hints={"provider": "voltagepark"}),
    Source("hyperstack_blog", "Hyperstack Blog", "https://www.hyperstack.cloud/blog/rss.xml",
           "rss", "provider", "official", 3 * H, entity_hints={"provider": "hyperstack"},
           note="valid RSS but the channel had no items when verified"),
    Source("digitalocean_blog", "DigitalOcean Blog", "https://www.digitalocean.com/rss/blog.atom",
           "atom", "provider", "official", 3 * H, entity_hints={"provider": "digitalocean"}),
    Source("vultr_blog", "Vultr Blog", "https://blogs.vultr.com/rss.xml",
           "rss", "provider", "official", 3 * H, entity_hints={"provider": "vultr"}),
    Source("scaleway_blog", "Scaleway Blog", "https://www.scaleway.com/en/blog/rss.xml",
           "rss", "provider", "official", 6 * H, entity_hints={"provider": "scaleway"}),
    # ---------------------------------------------------------------- vendors
    Source("nvidia_newsroom", "NVIDIA Newsroom", "https://nvidianews.nvidia.com/releases.xml",
           "rss", "vendor", "official", H),
    Source("nvidia_blog", "NVIDIA Blog", "https://blogs.nvidia.com/feed/",
           "rss", "vendor", "official", H),
    Source("nvidia_developer_blog", "NVIDIA Technical Blog", "https://developer.nvidia.com/blog/feed",
           "atom", "vendor", "official", 6 * H),
    Source("amd_ir", "AMD Press Releases", "https://ir.amd.com/news-events/press-releases/rss",
           "rss", "vendor", "official", H),
    # ---------------------------------------------------------------- regulation
    Source("fr_bis_rules", "Federal Register: BIS rules & proposed rules",
           _FR + "&conditions[agencies][]=industry-and-security-bureau&conditions[type][]=RULE&conditions[type][]=PRORULE",
           "federal_register", "regulation", "official", 6 * H, topics=("export_controls",),
           note="official Federal Register API (no key); Entity List and EAR changes"),
    Source("fr_bis_semis", "Federal Register: BIS semiconductor / advanced computing",
           _FR + "&conditions[agencies][]=industry-and-security-bureau"
                 "&conditions[term]=semiconductor%20%7C%20%22advanced%20computing%22%20%7C%20%22integrated%20circuits%22",
           "federal_register", "regulation", "official", 6 * H, topics=("export_controls",)),
    Source("fr_ferc_datacenter", "Federal Register: FERC rules on data centers / large loads",
           _FR + "&conditions[agencies][]=federal-energy-regulatory-commission&conditions[type][]=RULE"
                 "&conditions[type][]=PRORULE&conditions[term]=%22data%20center%22%20%7C%20%22large%20load%22",
           "federal_register", "regulation", "official", 12 * H, topics=("power",),
           note="power-grid rules affecting datacenter supply (co-location, large-load interconnection)"),
    # ---------------------------------------------------------------- trade press / analysis
    Source("dcd", "DatacenterDynamics", "https://www.datacenterdynamics.com/en/rss/",
           "rss", "trade_press", "press", 30 * 60),
    Source("the_register", "The Register", "https://www.theregister.com/headlines.atom",
           "rss", "trade_press", "press", 30 * 60, note="redirects to api.theregister.com RSS"),
    Source("hpcwire", "HPCwire", "https://www.hpcwire.com/feed/", "rss", "trade_press", "press", H),
    Source("tomshardware", "Tom's Hardware", "https://www.tomshardware.com/feeds/all", "rss", "trade_press", "press", 30 * 60),
    Source("techcrunch_ai", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/",
           "rss", "trade_press", "press", 30 * 60),
    Source("datacenterknowledge", "Data Center Knowledge", "https://www.datacenterknowledge.com/rss.xml",
           "rss", "trade_press", "press", H),
    Source("datacenterfrontier", "Data Center Frontier",
           "https://www.datacenterfrontier.com/__rss/website-scheduled-content.xml?input=%7B%22sectionAlias%22%3A%22home%22%7D",
           "rss", "trade_press", "press", H),
    Source("servethehome", "ServeTheHome", "https://www.servethehome.com/feed/", "rss", "trade_press", "press", H),
    Source("nextplatform", "The Next Platform", "https://www.nextplatform.com/feed/", "rss", "trade_press", "analysis", H),
    Source("eetimes", "EE Times", "https://www.eetimes.com/feed/", "rss", "trade_press", "press", H),
    Source("digitimes", "DigiTimes", "https://www.digitimes.com/rss/daily.xml", "rss", "trade_press", "press", H,
           note="supply-chain and chip shipment news; many articles paywalled, headlines are free"),
    Source("trendforce_semis", "TrendForce: Semiconductors", "https://www.trendforce.com/feed/Semiconductors.html",
           "rss", "trade_press", "analysis", 3 * H),
    Source("ieee_spectrum_semis", "IEEE Spectrum: Semiconductors", "https://spectrum.ieee.org/feeds/topic/semiconductors.rss",
           "rss", "trade_press", "press", 6 * H),
    Source("utilitydive", "Utility Dive", "https://www.utilitydive.com/feeds/news/", "rss", "trade_press", "press", H,
           note="power / grid constraints on datacenters"),
    Source("semianalysis", "SemiAnalysis", "https://newsletter.semianalysis.com/feed", "rss", "trade_press", "analysis", 3 * H),
    Source("semianalysis_site", "SemiAnalysis (site)", "https://semianalysis.com/feed/", "rss", "trade_press", "analysis", 3 * H),
    Source("the_information", "The Information", "https://www.theinformation.com/feed", "atom", "trade_press", "press", H,
           note="headlines and teasers only; articles are paywalled"),
    # ---------------------------------------------------------------- not enabled (could not verify a feed)
    Source("cme_press", "CME Group press releases", "https://www.cmegroup.com/media-room/press-releases.rss",
           "rss", "markets", "official", 6 * H, topics=("futures",), enabled=False,
           note="CME returns a block page for automated clients and its terms forbid scripted access; "
                "GPU-futures news is picked up through trade press instead"),
    Source("ice_press", "ICE press releases", "https://ir.theice.com/press/press-releases/rss",
           "rss", "markets", "official", 6 * H, topics=("futures",), enabled=False, note="HTTP 403 to non-browser clients"),
    Source("coreweave_ir", "CoreWeave investor news", "https://investors.coreweave.com/news-events/press-releases/rss",
           "rss", "provider", "official", H, entity_hints={"provider": "coreweave"}, enabled=False, note="HTTP 403"),
    Source("nvidia_ir", "NVIDIA investor news", "https://investor.nvidia.com/rss/news-releases.xml",
           "rss", "vendor", "official", H, enabled=False, note="HTTP 403; nvidianews.nvidia.com covers the same releases"),
    Source("crusoe_blog", "Crusoe Blog", "https://www.crusoe.ai/resources/blog/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "crusoe"}, enabled=False, note="no RSS/Atom feed found (all candidate paths 404)"),
    Source("nebius_blog", "Nebius Blog", "https://nebius.com/blog/rss", "rss", "provider", "official", H,
           entity_hints={"provider": "nebius"}, enabled=False, note="no RSS/Atom feed found (all candidate paths 404)"),
    Source("vast_blog", "Vast.ai articles", "https://vast.ai/article/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "vast"}, enabled=False, note="no feed found (404)"),
    Source("verda_blog", "Verda (DataCrunch) Blog", "https://verda.com/blog/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "verda"}, enabled=False, note="no feed found (404)"),
    Source("hyperbolic_blog", "Hyperbolic Blog", "https://www.hyperbolic.ai/blog/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "hyperbolic"}, enabled=False, note="HTTP 500"),
    Source("salad_blog", "Salad Blog", "https://salad.com/blog/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "salad"}, enabled=False, note="redirects to an HTML community page"),
    Source("fluidstack_news", "Fluidstack news", "https://fluidstack.io/news/rss.xml", "rss", "provider", "official", H,
           entity_hints={"provider": "fluidstack"}, enabled=False, note="no feed found (404)"),
    Source("silicondata_blog", "Silicon Data (GPU rental index) blog", "https://www.silicondata.com/blog/rss.xml",
           "rss", "markets", "analysis", 6 * H, topics=("pricing_report",), enabled=False, note="no feed found (404)"),
    Source("intel_newsroom", "Intel Newsroom", "https://newsroom.intel.com/feed/", "rss", "vendor", "official", H,
           enabled=False, note="redirects to an HTML page; no feed"),
    Source("genesiscloud_blog", "Genesis Cloud blog", "https://www.genesiscloud.com/blog/rss.xml", "rss", "provider",
           "official", H, enabled=False, note="TLS handshake error from this machine"),
]

SOURCES: dict[str, Source] = {s.id: s for s in _ALL}
CATEGORIES = ("provider", "vendor", "hyperscaler", "trade_press", "regulation", "markets")
TIERS = ("official", "press", "analysis")
KINDS = ("rss", "atom", "json_api", "federal_register")

assert all(s.category in CATEGORIES and s.trust_tier in TIERS and s.kind in KINDS and s.key in ("url", "guid")
           for s in _ALL)


def enabled() -> list[Source]:
    return [s for s in _ALL if s.enabled]


def get(source_id: str) -> Source | None:
    return SOURCES.get(source_id)


def as_dict(s: Source) -> dict:
    return {"id": s.id, "name": s.name, "url": s.url, "kind": s.kind, "category": s.category,
            "trust_tier": s.trust_tier, "poll_seconds": s.poll_seconds, "topics": list(s.topics),
            "entity_hints": dict(s.entity_hints), "enabled": s.enabled, "note": s.note, "key": s.key}
