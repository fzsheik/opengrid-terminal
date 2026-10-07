"""Deterministic, transparent classification of a news item. Re-runnable over stored items.

Everything is a hand-maintained table matched against title + summary; no model, no
guessing, so the same text always yields the same entities, topics and score, and the
match that produced each one is kept.

GPUs. A mention of a model ("H100") maps to its FAMILY: entity gpu_family=H100, listing
every canonical H100 variant we know. A specific canonical variant is named only when the
text itself states the form factor ("H100 SXM", "HGX H100", "H100 PCIe", "H100 NVL",
"A100 80GB SXM4"). Architecture names (Blackwell, Hopper) are families too, listing the
datacenter parts of that generation.

Providers. provider_meta ids plus the compute companies we do not (yet) poll, with
aliases ("Lambda Labs" -> lambda, "DataCrunch" -> verda). Ambiguous words ("Lambda",
"Crusoe", "Oracle", "OCI", "Verda") count only when the text also has compute context,
and "AWS Lambda" never counts as Lambda. A provider's own blog implies that provider
(entity_hints), recorded with via="source".

Regions. Country / place keywords -> ISO code -> the region groups of regions.py
(imported lazily; the contract is regions.REGION_GROUPS / COUNTRY_GROUP).

Relevance (0-100), components kept on every item (see methodology/news.md):
    entity_points  <= 40   specific GPU 20 (+5 each more), family-only 15 (+4), provider
                           named in text 12 (+4), provider from source hint 6, region 4,
                           compute context 8
    topic_points   <= 40   sum of topic weights; a topic matched only in the summary counts 0.6x
    tier_mult              official 1.0, analysis 0.95, press 0.85
    recency_mult           by lag between publication and first sight (a backfilled archive
                           item is old news): <=2d 1.0, <=7d 0.9, <=30d 0.75, older 0.6
    relevance = round(min(100, (entity_points + topic_points) * 1.25 * tier_mult * recency_mult))
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import canonical
import provider_meta

# --------------------------------------------------------------------------- GPUs


@dataclass(frozen=True)
class Family:
    id: str
    pattern: str                 # mention in text
    canon: str | None            # regex over canonical names -> the family's variants
    architecture: str | None = None
    needs_context: bool = False  # e.g. "Rubin" alone could be a surname

    def rx(self):
        return re.compile(self.pattern)


_NB = r"(?<![A-Za-z0-9])"   # not preceded by a letter/digit: GH200 is not H200, GB200 is not B200
_NA = r"(?![0-9])"

FAMILIES: list[Family] = [
    Family("H100", _NB + r"H100" + _NA, r"^NVIDIA H100 ", "Hopper"),
    Family("H200", _NB + r"H200" + _NA, r"^NVIDIA H200 ", "Hopper"),
    Family("H20", _NB + r"H20(?![0-9A-Za-z])", None, "Hopper"),
    Family("H800", _NB + r"H800" + _NA, None, "Hopper"),
    Family("GH200", _NB + r"GH200" + _NA, r"^NVIDIA GH200", "Hopper"),
    Family("B200", _NB + r"B200" + _NA, r"^NVIDIA B200 ", "Blackwell"),
    Family("B300", _NB + r"B300" + _NA, r"^NVIDIA B300 (?!MIG)", "Blackwell"),
    Family("GB200", _NB + r"GB200" + _NA, None, "Blackwell"),
    Family("GB300", _NB + r"GB300" + _NA, r"^NVIDIA GB300", "Blackwell"),
    Family("B30A", _NB + r"B30A" + _NA, None, "Blackwell"),
    Family("Rubin", r"\b(?:Vera\s+)?Rubin(?:\s+(?:Ultra|CPX|NVL\d+))?\b", None, "Rubin", needs_context=True),
    Family("A100", _NB + r"A100" + _NA, r"^NVIDIA A100 ", "Ampere"),
    Family("A800", _NB + r"A800" + _NA, None, "Ampere"),
    Family("L40S", _NB + r"L40S\b", r"^NVIDIA L40S ", "Ada Lovelace"),
    Family("L40", _NB + r"L40(?![0-9A-Za-z])", r"^NVIDIA L40 ", "Ada Lovelace"),
    Family("L4", r"(?:NVIDIA\s+L4|\bL4\s+(?:GPUs?|Tensor))\b", r"^NVIDIA L4 ", "Ada Lovelace"),
    Family("A10G", _NB + r"A10G\b", r"^NVIDIA A10G ", "Ampere"),
    Family("T4", r"(?:NVIDIA\s+T4|\bT4\s+(?:GPUs?|Tensor))\b", r"^NVIDIA T4 ", "Turing"),
    Family("V100", _NB + r"V100" + _NA, r"^NVIDIA Tesla V100", "Volta"),
    Family("RTX 4090", r"\bRTX\s*4090\b", r"^NVIDIA RTX 4090 ", "Ada Lovelace"),
    Family("RTX 5090", r"\bRTX\s*5090\b", r"^NVIDIA RTX 5090 ", "Blackwell"),
    Family("RTX PRO 6000", r"\bRTX\s*PRO\s*6000\b", r"^NVIDIA RTX PRO 6000 (?!SE MIG)", "Blackwell"),
    Family("MI300X", _NB + r"MI300X\b", r"^AMD Instinct MI300X ", "CDNA 3"),
    Family("MI325X", _NB + r"MI325X\b", r"^AMD Instinct MI325X ", "CDNA 3"),
    Family("MI350", _NB + r"MI350(?![0-9X])", r"^AMD Instinct MI350 ", "CDNA 4"),
    Family("MI355X", _NB + r"MI355X\b", r"^AMD Instinct MI355X ", "CDNA 4"),
    Family("MI400", _NB + r"MI4[0-9]{2}X?\b", None, "CDNA 5"),
    Family("Gaudi", r"\bGaudi\s*[23]?\b", r"^Intel Gaudi", None, needs_context=True),
]
# Architecture words. Only the datacenter / cloud parts are listed as their variants.
ARCHITECTURES = {
    "Blackwell": (r"\bBlackwell(?:\s+Ultra)?\b", ("B200", "B300", "GB200", "GB300", "RTX PRO 6000")),
    "Hopper": (r"\bHopper\b", ("H100", "H200", "GH200")),
}

# Explicit form factor in the text -> one canonical variant.
_SP = r"[\s-]*"
VARIANT_RULES: list[tuple[str, str]] = [
    (_NB + r"H100" + _SP + r"(?:80\s*GB" + _SP + r")?PCIe" + _SP + r"NVLink", "NVIDIA H100 80GB PCIe NVLink"),
    (_NB + r"H100" + _SP + r"(?:80\s*GB" + _SP + r")?PCIe(?!" + _SP + r"NVLink)", "NVIDIA H100 80GB PCIe"),
    (_NB + r"H100" + _SP + r"(?:80\s*GB" + _SP + r")?SXM5?\b|\bHGX" + _SP + r"H100", "NVIDIA H100 80GB SXM5"),
    (_NB + r"H100" + _SP + r"NVL\b", "NVIDIA H100 94GB NVL"),
    (_NB + r"H200" + _SP + r"(?:141\s*GB" + _SP + r")?SXM5?\b|\bHGX" + _SP + r"H200", "NVIDIA H200 141GB SXM5"),
    (_NB + r"H200" + _SP + r"NVL\b", "NVIDIA H200 143GB NVL"),
    (_NB + r"A100" + _SP + r"80\s*GB" + _SP + r"SXM4?\b|A100" + _SP + r"SXM4?" + _SP + r"80\s*GB", "NVIDIA A100 80GB SXM4"),
    (_NB + r"A100" + _SP + r"40\s*GB" + _SP + r"SXM4?\b|A100" + _SP + r"SXM4?" + _SP + r"40\s*GB", "NVIDIA A100 40GB SXM4"),
    (_NB + r"A100" + _SP + r"80\s*GB" + _SP + r"PCIe|A100" + _SP + r"PCIe" + _SP + r"80\s*GB", "NVIDIA A100 80GB PCIe"),
    (_NB + r"A100" + _SP + r"40\s*GB" + _SP + r"PCIe|A100" + _SP + r"PCIe" + _SP + r"40\s*GB", "NVIDIA A100 40GB PCIe"),
    (r"(?<!G)B200" + _SP + r"SXM\d?\b|\bHGX" + _SP + r"B200", "NVIDIA B200 180GB SXM"),
    (r"(?<!G)B300" + _SP + r"SXM\d?\b|\bHGX" + _SP + r"B300", "NVIDIA B300 288GB SXM"),
]
_VARIANT_RX = [(re.compile(p, re.I), name) for p, name in VARIANT_RULES]


def _variants(canon_rx: str | None) -> list[str]:
    if not canon_rx:
        return []
    rx = re.compile(canon_rx)
    return sorted(n for n in canonical.all_canonical_names() if rx.search(n) and "MIG" not in n)


FAMILY_VARIANTS: dict[str, list[str]] = {f.id: _variants(f.canon) for f in FAMILIES}
FAMILY_ARCH: dict[str, str | None] = {f.id: f.architecture for f in FAMILIES}
for _arch, (_p, _fams) in ARCHITECTURES.items():
    FAMILY_VARIANTS[_arch] = sorted({v for f in _fams for v in FAMILY_VARIANTS.get(f, [])})
    FAMILY_ARCH[_arch] = _arch
_FAMILY_RX = [(f, f.rx()) for f in FAMILIES] + [
    (Family(a, p, None, a), re.compile(p)) for a, (p, _f) in ARCHITECTURES.items()]
assert all(n in canonical.all_canonical_names() for _p, n in VARIANT_RULES), "variant rule names must be canonical"


def families_for_gpu(gpu: str) -> list[str]:
    """Family ids (model and architecture) whose variant list contains this canonical GPU."""
    return sorted(f for f, vs in FAMILY_VARIANTS.items() if gpu in vs)


def family_id(value: str) -> str | None:
    """Case-insensitive family lookup: 'h100' -> 'H100', 'blackwell' -> 'Blackwell'."""
    low = value.strip().lower().replace("-", " ")
    for f in FAMILY_VARIANTS:
        if f.lower() == low:
            return f
    return None


# --------------------------------------------------------------------------- providers

# (id, display name, [(pattern, case_sensitive, needs_context)], provider_class when not in provider_meta)
_P = [
    ("aws", "AWS", [(r"\bAWS\b", True, False), (r"Amazon Web Services", False, False), (r"\bAmazon EC2\b|\bEC2\b", True, False)], None),
    ("gcp", "Google Cloud", [(r"\bGoogle Cloud\b", False, False), (r"\bGCP\b", True, False), (r"Google Compute Engine", False, False)], "hyperscaler"),
    ("azure", "Microsoft Azure", [(r"\bAzure\b", True, False)], "hyperscaler"),
    ("oci", "Oracle Cloud (OCI)", [(r"Oracle Cloud", False, False), (r"\bOCI\b", True, True), (r"\bOracle\b", True, True)], "hyperscaler"),
    ("lambda", "Lambda", [(r"Lambda Labs|Lambda Cloud|lambda\.ai|Lambda,? Inc\b", False, False),
                          (r"(?<!AWS )(?<!Amazon )\bLambda\b(?!\s*(?:functions?|@Edge|layers?|SnapStart|runtimes?|invocations?|URLs?|MCP)\b)", True, True)], None),
    ("coreweave", "CoreWeave", [(r"\bCoreWeave\b", False, False)], "neocloud"),
    ("crusoe", "Crusoe", [(r"Crusoe (?:Cloud|Energy|AI)", False, False), (r"\bCrusoe\b", True, True)], None),
    ("nebius", "Nebius", [(r"\bNebius\b", False, False)], None),
    ("runpod", "RunPod", [(r"\bRunPod\b", False, False)], None),
    ("vast", "Vast.ai", [(r"\bVast\.ai\b", False, False)], None),
    ("hyperstack", "Hyperstack", [(r"\bHyperstack\b", False, False), (r"NexGen Cloud", False, False)], None),
    ("together", "Together AI", [(r"Together AI|Together\.ai|Together Computer", False, False)], "neocloud"),
    ("voltagepark", "Voltage Park", [(r"Voltage Park", False, False)], None),
    ("salad", "Salad", [(r"SaladCloud|Salad Cloud|Salad Technologies|salad\.com", False, False)], None),
    ("hyperbolic", "Hyperbolic", [(r"Hyperbolic Labs|hyperbolic\.ai|Hyperbolic AI\b", False, False)], None),
    ("digitalocean", "DigitalOcean", [(r"\bDigitalOcean\b", False, False)], None),
    ("lium", "Lium", [(r"\bLium\b", True, True)], None),
    ("massedcompute", "Massed Compute", [(r"Massed Compute", False, False)], None),
    ("verda", "Verda", [(r"\bDataCrunch\b", False, False), (r"\bVerda\b", True, True)], None),
    ("latitude", "Latitude.sh", [(r"Latitude\.sh", False, False)], None),
    ("denvr", "Denvr", [(r"\bDenvr\b", False, False)], None),
    ("fluidstack", "Fluidstack", [(r"\bFluidstack\b", False, False)], "neocloud"),
    ("nscale", "Nscale", [(r"\bNscale\b", False, False)], "neocloud"),
    ("tensorwave", "TensorWave", [(r"\bTensorWave\b", False, False)], "neocloud"),
    ("vultr", "Vultr", [(r"\bVultr\b", False, False)], "general_cloud"),
    ("scaleway", "Scaleway", [(r"\bScaleway\b", False, False)], "general_cloud"),
    ("ovhcloud", "OVHcloud", [(r"\bOVH(?:cloud)?\b", False, False)], "general_cloud"),
    ("sfcompute", "SF Compute", [(r"SF Compute|San Francisco Compute", False, False)], "marketplace"),
    ("shadeform", "Shadeform", [(r"\bShadeform\b", False, False)], "marketplace"),
    ("cudo", "Cudo Compute", [(r"Cudo Compute", False, False)], "neocloud"),
    ("genesiscloud", "Genesis Cloud", [(r"Genesis Cloud", False, False)], "neocloud"),
    ("iren", "IREN", [(r"\bIREN\b", True, False), (r"Iris Energy", False, False)], "neocloud"),
    ("applieddigital", "Applied Digital", [(r"Applied Digital", False, False)], "neocloud"),
    ("paperspace", "Paperspace", [(r"\bPaperspace\b", False, False)], "neocloud"),
]
PROVIDERS = {pid: name for pid, name, _a, _c in _P}
_PROVIDER_CLASS = {pid: (cls or provider_meta.meta(pid).provider_class) for pid, _n, _a, cls in _P}
_PROVIDER_RX = [(pid, [(re.compile(p, 0 if cs else re.I), ctx) for p, cs, ctx in aliases]) for pid, _n, aliases, _c in _P]


def provider_class(pid: str) -> str:
    return _PROVIDER_CLASS.get(pid) or provider_meta.meta(pid).provider_class


# --------------------------------------------------------------------------- regions

# keyword -> (ISO code or None, fallback group). The group comes from regions.COUNTRY_GROUP
# when the code is known there, so news and listings agree on where a country belongs.
_R = [
    (r"\bU\.S\.(?:A\.)?|\bUSA\b|\bUS\b|United States", "US", "US", True),
    (r"\b(?:Texas|Virginia|Ohio|Oregon|Arizona|Iowa|Nevada|Utah|Wyoming|North Dakota|Pennsylvania|Louisiana|"
     r"Tennessee|Wisconsin|Indiana|New Mexico|Oklahoma|Illinois|California|Silicon Valley|Abilene)\b", "US", "US", True),
    (r"\b(?:Canada|Canadian|Quebec|Ontario|Alberta|Toronto|Montreal|British Columbia)\b", "CA", "Canada", True),
    (r"\bU\.K\.|\bUK\b|United Kingdom|\b(?:Britain|British|England|Scotland|Wales|London)\b", "GB", "UK", True),
    (r"\b(?:Germany|German|Frankfurt|Berlin|Munich)\b", "DE", "Europe", True),
    (r"\b(?:France|French|Paris)\b", "FR", "Europe", True),
    (r"\b(?:Netherlands|Dutch|Amsterdam)\b", "NL", "Europe", True),
    (r"\b(?:Finland|Finnish|Helsinki)\b", "FI", "Europe", True),
    (r"\b(?:Norway|Norwegian|Oslo)\b", "NO", "Europe", True),
    (r"\b(?:Sweden|Swedish|Stockholm)\b", "SE", "Europe", True),
    (r"\b(?:Iceland|Icelandic|Reykjavik)\b", "IS", "Europe", True),
    (r"\b(?:Denmark|Danish|Copenhagen)\b", "DK", "Europe", True),
    (r"\b(?:Spain|Spanish|Madrid)\b", "ES", "Europe", True),
    (r"\b(?:Portugal|Lisbon)\b", "PT", "Europe", True),
    (r"\b(?:Italy|Italian|Milan)\b", "IT", "Europe", True),
    (r"\b(?:Poland|Polish|Warsaw)\b", "PL", "Europe", True),
    (r"\b(?:Ireland|Irish|Dublin)\b", "IE", "Europe", True),
    (r"\b(?:Switzerland|Swiss|Zurich)\b", "CH", "Europe", True),
    (r"\b(?:Russia|Russian|Moscow)\b", "RU", "Europe", True),
    (r"\b(?:Europe|European|EU)\b", None, "Europe", True),
    (r"\b(?:China|Chinese|Beijing|Shanghai|Shenzhen)\b", "CN", "APAC", True),
    (r"\b(?:Japan|Japanese|Tokyo|Osaka)\b", "JP", "APAC", True),
    (r"\b(?:South Korea|Korea|Korean|Seoul)\b", "KR", "APAC", True),
    (r"\b(?:India|Indian|Mumbai|Bengaluru|Bangalore|Hyderabad|Chennai)\b", "IN", "APAC", True),
    (r"\bSingapore\b", "SG", "APAC", True),
    (r"\b(?:Taiwan|Taiwanese|Taipei|Hsinchu)\b", "TW", "APAC", True),
    (r"\b(?:Australia|Australian|Sydney|Melbourne)\b", "AU", "APAC", True),
    (r"\b(?:Malaysia|Malaysian|Johor|Kuala Lumpur)\b", "MY", "APAC", True),
    (r"\b(?:Indonesia|Jakarta|Batam)\b", "ID", "APAC", True),
    (r"\b(?:Thailand|Vietnam|Philippines)\b", None, "APAC", True),
    (r"\bHong Kong\b", "HK", "APAC", True),
    (r"\b(?:Asia|Asia-Pacific|APAC)\b", None, "APAC", True),
    (r"\bUAE\b|United Arab Emirates|\b(?:Abu Dhabi|Dubai|Emirati)\b", "AE", "Middle East", True),
    (r"\b(?:Saudi Arabia|Saudi|Riyadh)\b", "SA", "Middle East", True),
    (r"\b(?:Qatar|Bahrain|Kuwait|Oman|Israel|Israeli|Turkey|Türkiye)\b", None, "Middle East", True),
    (r"\bMiddle East\b|\bGulf states\b", None, "Middle East", True),
    (r"\b(?:Brazil|Brazilian|São Paulo|Sao Paulo)\b", "BR", "LATAM", True),
    (r"\b(?:Mexico|Mexican|Chile|Chilean|Argentina|Colombia)\b", None, "LATAM", True),
    (r"\bLatin America\b|\bLATAM\b", None, "LATAM", True),
    (r"\b(?:South Africa|Nigeria|Kenya|Egypt|Morocco)\b", None, "Africa", True),
    (r"\bAfrica(?:n)?\b", None, "Africa", True),
]
_REGION_RX = [(re.compile(p, 0 if cs else re.I), iso, g) for p, iso, g, cs in _R]


def _group(iso: str | None, fallback: str) -> str | None:
    try:
        import regions  # owned by the structure agent; imported lazily per the contract
        groups = set(regions.REGION_GROUPS)
        g = regions.COUNTRY_GROUP.get(iso, fallback) if iso and hasattr(regions, "COUNTRY_GROUP") else fallback
    except ImportError:
        groups, g = {"US", "Canada", "Europe", "UK", "APAC", "Middle East", "LATAM", "Africa"}, fallback
    return g if g in groups else None


# --------------------------------------------------------------------------- topics

@dataclass(frozen=True)
class Topic:
    id: str
    weight: int
    pattern: str | None
    description: str


_PRICE_NUM = r"\$\s?\d+(?:\.\d+)?\s*(?:/|per)\s*(?:GPU[- ]?)?(?:hr|hour)"
TOPICS: list[Topic] = [
    Topic("price_change", 15, r"\bprice (?:cuts?|drops?|reductions?|increases?|hikes?|changes?)\b|\b(?:cut|cuts|cutting|slash(?:es|ed)?|lower(?:s|ed)?|"
          r"reduc(?:es|ed)|rais(?:es|ed)|hik(?:es|ed))\s+(?:its\s+|the\s+)?(?:[\w.-]+\s+){0,2}prices?\b|\bpricing (?:update|change)s?\b|"
          r"\bcheaper\b|" + _PRICE_NUM, "a provider's list price changed or a price point was announced"),
    Topic("pricing_report", 15, r"\bprice index\b|\bpricing report\b|\bGPU (?:rental )?(?:prices?|pricing)\b|\bpricing (?:explained|guide|comparison|analysis)\b|\brental (?:prices?|rates?)\b|"
          r"\bcost per GPU[- ]hour\b|\bGPU[- ]hour (?:prices?|rates?)\b|\bcompute prices?\b",
          "reports and indices about GPU / compute pricing"),
    Topic("availability", 10, r"\bgenerally available\b|\bgeneral availability\b|\bnow available\b|\bavailable now\b|"
          r"\bsold out\b|\bwait ?list\b|\bshortages?\b|\bin stock\b|\ballocation\b|\bpreview\b|\bbackorder",
          "general availability, sell-outs, shortages, waitlists"),
    Topic("capacity", 8, r"\bcapacity\b|\bmegawatts?\b|\bgigawatts?\b|\b\d+(?:\.\d+)?\s?[MG]W\b|\b\d{1,3}(?:,\d{3})+\s+(?:GPUs|accelerators)\b|"
          r"\b\d+[kK]\s+GPUs\b|\b(?:GPU|AI|training|compute) clusters?\b|\bsupercomputers?\b", "new or expanded compute capacity"),
    Topic("outage", 10, r"\boutages?\b|\bdowntime\b|\bservice disruption\b|\bdisruptions?\b|\bdegraded\b|\bservice interruption\b",
          "outages and service disruptions"),
    Topic("region_launch", 8, r"\bnew regions?\b|\bregion (?:launch|expansion)\b|\bavailability zones?\b|\blaunch(?:es|ed)? in\b|"
          r"\bexpands? (?:to|into)\b|\bnow available in\b", "a new cloud region / location"),
    Topic("gpu_launch", 12, None, "a GPU / accelerator launched, unveiled or shipping (derived: launch words + a GPU mention)"),
    Topic("funding", 6, r"\brais(?:es|ed|ing) \$|\bfunding round\b|\bSeries [A-H]\b|\bvaluation\b|\bIPO\b|\bdebt financing\b|"
          r"\bcredit facility\b|\bfinancing\b|\binvestment from\b", "funding rounds, debt, IPOs"),
    Topic("capex", 10, r"\bcapex\b|\bcapital expenditures?\b|\bcapital spending\b|\b(?:spend|invest)(?:s|ing)? (?:up to |about |nearly |over )?"
          r"\$\d+(?:\.\d+)?\s?(?:billion|bn|trillion)\b|\binfrastructure spending\b", "hyperscaler / AI capex"),
    Topic("supply_deal", 12, r"\bsupply (?:deal|agreement|contract)s?\b|\bpurchase (?:agreement|order)s?\b|\boff-?take\b|"
          r"\b(?:orders?|ordered|secures?|secured|buys?|bought|acquir(?:es|ed)|purchas(?:es|ed))\s+(?:up to |over |more than |about |nearly )?"
          r"[\d,.]+\s*(?:k|thousand|million)?\s+(?:[\w-]+\s+){0,3}(?:GPUs|chips|accelerators)\b|"
          r"\bagreement to (?:supply|deploy)\b", "GPU / chip supply and purchase deals"),
    Topic("export_controls", 12, r"\bexport controls?\b|\bexport (?:restrictions?|rules?|licen[cs]es?|ban)\b|\bEntity List\b|"
          r"\bBureau of Industry and Security\b|\bBIS\b|\bExport Administration Regulations\b|\bsanctions?\b|"
          r"\bchip (?:ban|smuggling)\b|\bsmuggl\w*\b|\bdiversion\b|\bAI diffusion\b", "export controls, sanctions, Entity List"),
    Topic("power", 7, r"\bpower (?:grid|supply|constraints?|shortages?|purchase|demand|capacity)\b|\b(?:the|power|electric(?:ity)?|national) grid\b|\bgrid (?:operators?|connections?|capacity|constraints?)\b|"
          r"\bnuclear\b|\belectric utilit\w*\b|\butility-scale\b|\bpower utilit\w*\b|\belectricity\b|\bPPAs?\b|\bsubstations?\b|\binterconnection\b|\blarge loads?\b|"
          r"\bnatural gas\b|\bturbines?\b", "power and grid constraints on datacenters"),
    Topic("contracts", 10, r"\bcontracts? (?:worth|valued|with|to supply|for)\b|\b(?:awarded|wins?|won|inks?|inked) (?:a |an )?[\w\s$.,-]{0,40}?(?:contract|deal)\b|\bmulti-?year (?:deal|agreement)\b|\bdeal (?:worth|valued)\b|"
          r"\$\d+(?:\.\d+)?\s?(?:billion|bn|million|m)\s+(?:deal|agreement|contract)\b|\bsign(?:s|ed) (?:a |an )?[\w\s-]{0,30}agreement\b",
          "large compute contracts"),
    Topic("futures", 12, r"\bfutures\b|\bforward (?:contracts?|curve)\b|\b(?:compute|GPU|financial) derivatives?\b|\bcompute exchange\b|\bhedg\w*\b|"
          r"\bcompute (?:as a )?commodit\w*\b|\boptions market\b", "GPU-compute futures, forwards, exchanges"),
    Topic("datacenter", 4, r"\bdata ?cent(?:er|re)s?\b|\bdatacent(?:er|re)s?\b|\bcampus\b|\bcolocation\b|\bcolo\b|\bhyperscale\b",
          "datacenter buildout"),
    Topic("earnings", 5, r"\bearnings\b|\bquarterly results\b|\b(?:first|second|third|fourth)[- ]quarter\b|\bQ[1-4] (?:20\d\d|results|revenue)\b|"
          r"\brevenue\b|\bguidance\b", "earnings and revenue"),
    Topic("neocloud_expansion", 10, None, "a neocloud adding capacity, sites or regions (derived: neocloud provider + expansion topic)"),
]
TOPIC_IDS = [t.id for t in TOPICS]
TOPIC_WEIGHT = {t.id: t.weight for t in TOPICS}
_TOPIC_RX = [(t, re.compile(t.pattern, re.I)) for t in TOPICS if t.pattern]
_LAUNCH = re.compile(r"\b(?:launch\w*|unveil\w*|introduc\w*|debut\w*|announc\w*|ships?|shipping|shipped|"
                     r"unleash\w*|reveal\w*|roadmap|next-gen\w*)\b", re.I)
# Compute context: words that make an item about compute supply rather than cloud software in general.
_COMPUTE = re.compile(r"\bGPUs?\b|\b(?:AI|GPU|ML) accelerators?\b|\baccelerators\b|\baccelerated computing\b|"
                      r"\bAI (?:infrastructure|compute|chips?|factor(?:y|ies)|clusters?|data ?cent(?:er|re)s?)\b|"
                      r"\bcompute capacity\b|\bHPC\b|\bsupercomput\w*\b|\bneoclouds?\b|\binference (?:capacity|clusters?)\b|"
                      r"\btraining clusters?\b|\bdata ?cent(?:er|re)s?\b|\bdatacent(?:er|re)s?\b|\bNVIDIA\b|\bCUDA\b|\bTPUs?\b|"
                      r"\bTrainium\d?\b|\bInferentia\d?\b|\b(?:P4de?|P5e?n?|P6e?|G4dn|G5g?|G6e?|G7e|Trn[123]|Inf[12])\b(?:\.\w+)? instances?\b",
                      re.I)
# Topics that are about compute only when the item has compute context: "now available in more
# regions" is a compute signal for GPU instances and noise for a database feature.
_GATED = {"availability", "region_launch", "outage", "price_change", "capacity", "funding", "earnings", "contracts"}
_CONTEXT_CLASSES = {"neocloud", "marketplace", "decentralized"}
_EXPANSION_TOPICS = {"capacity", "region_launch", "datacenter", "supply_deal", "contracts"}

TIER_MULT = {"official": 1.0, "analysis": 0.95, "press": 0.85}


def topic_catalog() -> list[dict]:
    return [{"id": t.id, "weight": t.weight, "description": t.description} for t in TOPICS]


# --------------------------------------------------------------------------- classify

def _gpu_entities(text: str, has_context: bool) -> tuple[list[str], list[dict]]:
    gpus, spans = [], []
    for rx, name in _VARIANT_RX:
        for m in rx.finditer(text):
            spans.append(m.span())
            if name not in gpus:
                gpus.append(name)
    inside = lambda m: any(a <= m.start() and m.end() <= b for a, b in spans)  # noqa: E731
    fams = []
    for f, rx in _FAMILY_RX:
        if f.needs_context and not has_context:
            continue
        # A family named only inside a stated variant ("H100" in "H100 SXM") is not a bare family mention.
        bare = [m for m in rx.finditer(text) if not inside(m)]
        if bare:
            fams.append({"family": f.id, "architecture": FAMILY_ARCH.get(f.id), "match": bare[0].group(0),
                         "variants": FAMILY_VARIANTS.get(f.id, [])})
    # A stated variant implies its family; listed for display, with match=None (no family entity row).
    for g in gpus:
        for fid in families_for_gpu(g):
            if fid not in ARCHITECTURES and not any(x["family"] == fid for x in fams):
                fams.append({"family": fid, "architecture": FAMILY_ARCH.get(fid), "match": None,
                             "variants": FAMILY_VARIANTS[fid], "implied_by": g})
    return gpus, fams


def _provider_entities(text: str, has_context: bool, hint: str | None) -> list[dict]:
    out = []
    for pid, rxs in _PROVIDER_RX:
        for rx, needs_ctx in rxs:
            m = rx.search(text)
            if m and (not needs_ctx or has_context):
                out.append({"id": pid, "name": PROVIDERS[pid], "via": "text", "match": m.group(0),
                            "known": pid in provider_meta.PROVIDER_META, "provider_class": provider_class(pid)})
                break
    if hint and not any(p["id"] == hint for p in out):
        out.append({"id": hint, "name": PROVIDERS.get(hint, hint), "via": "source", "match": None,
                    "known": hint in provider_meta.PROVIDER_META, "provider_class": provider_class(hint)})
    return out


def _region_entities(text: str) -> list[dict]:
    hits: dict[str, dict] = {}
    for rx, iso, fallback in _REGION_RX:
        m = rx.search(text)
        if not m:
            continue
        g = _group(iso, fallback)
        if g is None:
            continue
        h = hits.setdefault(g, {"group": g, "matched": [], "countries": []})
        h["matched"].append(m.group(0))
        if iso and iso not in h["countries"]:
            h["countries"].append(iso)
    return list(hits.values())


def recency_mult(published_at: datetime | None, first_seen_at: datetime | None) -> float:
    if not published_at or not first_seen_at:
        return 1.0
    lag = first_seen_at - published_at
    if lag <= timedelta(days=2):
        return 1.0
    if lag <= timedelta(days=7):
        return 0.9
    if lag <= timedelta(days=30):
        return 0.75
    return 0.6


def classify(title: str | None, summary: str | None, *, source_tier: str = "press",
             source_topics=(), provider_hint: str | None = None,
             published_at: datetime | None = None, first_seen_at: datetime | None = None) -> dict:
    """Entities, topics and relevance for one item. Pure function of its inputs."""
    title, summary = title or "", summary or ""
    text = f"{title}\n{summary}"
    compute = [m.group(0) for m in _COMPUTE.finditer(text)][:5]
    gpus, fams = _gpu_entities(text, bool(compute))
    has_context = bool(compute or gpus or fams)
    providers = _provider_entities(text, has_context, provider_hint)
    regions = _region_entities(text)

    context = bool(compute or gpus or fams or any(p["provider_class"] in _CONTEXT_CLASSES for p in providers))
    topics: dict[str, str] = {}
    for t, rx in _TOPIC_RX:
        if t.id in _GATED and not context:
            continue
        if rx.search(title):
            topics[t.id] = "title"
        elif rx.search(summary):
            topics[t.id] = "summary"
    if fams and (_LAUNCH.search(title) or (_LAUNCH.search(summary) and any(f["match"] and f["match"] in title for f in fams))):
        topics["gpu_launch"] = "title" if _LAUNCH.search(title) else "summary"
    if any(p["provider_class"] == "neocloud" for p in providers) and _EXPANSION_TOPICS & set(topics):
        topics["neocloud_expansion"] = "derived"
    for t in source_topics:
        topics.setdefault(t, "source")

    # ---- relevance
    ent = 0.0
    if gpus:
        ent += 20 + 5 * (len(gpus) - 1)
    elif fams:
        ent += 15 + 4 * (len([f for f in fams if f["match"]]) - 1)
    text_providers = [p for p in providers if p["via"] == "text"]
    if text_providers:
        ent += 12 + 4 * (len(text_providers) - 1)
    elif providers:
        ent += 6
    if regions:
        ent += 4
    if compute:
        ent += 8
    ent = min(40.0, ent)
    top = 0.0
    for tid, where in topics.items():
        w = TOPIC_WEIGHT.get(tid, 0)
        top += w * (0.6 if where == "summary" else 1.0)
    top = min(40.0, top)
    tier = TIER_MULT.get(source_tier, 0.85)
    rec = recency_mult(published_at, first_seen_at)
    signal = ent + top
    score = int(round(min(100.0, signal * 1.25 * tier * rec)))
    return {
        "entities": {"gpus": gpus, "gpu_families": fams, "providers": providers, "regions": regions,
                     "compute_terms": compute},
        "topics": sorted(topics),
        "topic_basis": topics,
        "relevance": score,
        "relevance_components": {"entity_points": ent, "topic_points": round(top, 2), "signal": round(signal, 2),
                                 "tier_mult": tier, "recency_mult": rec, "formula":
                                 "round(min(100, (entity_points + topic_points) * 1.25 * tier_mult * recency_mult))"},
    }


def entity_rows(c: dict) -> list[tuple[str, str]]:
    """(entity_type, entity_value) rows for news_entities, deduplicated."""
    rows = {("gpu", g) for g in c["entities"]["gpus"]}
    # Only families the text names. A family implied by a stated variant stays in the jsonb for display but
    # gets no row: an "H100 SXM" story must not surface for H100 PCIe through the shared family.
    rows |= {("gpu_family", f["family"]) for f in c["entities"]["gpu_families"] if f.get("match")}
    rows |= {("provider", p["id"]) for p in c["entities"]["providers"]}
    rows |= {("region", r["group"]) for r in c["entities"]["regions"]}
    rows |= {("topic", t) for t in c["topics"]}
    return sorted(rows)
