"""On-demand vertical topic deep-dive: briefing.py --topic "<query>" [--max-ieee N]

Stage A (0 IEEE calls): parse the query into concept groups (+ an optional power ceiling such as
  "sub-100nW"), search the local store and OpenAlex across the IC venues — classics by citation count
  and recent (>= recent_from_year) work — and score relevance locally.
Stage B (IEEE reserve): one boolean IEEE query (venue-filtered) + per-DOI lookups to confirm metadata and
  fill abstracts. Draws on `ieee_topic` in api_usage (topic.ieee_topic_reserve per day), separate from
  the daily collection's ieee_daily_budget, so it works even after the 07:00 run used its 50 calls.
  v3.0.6: skipped when no IEEE venue is configured or there is no IEEE_API_KEY; with the bio_med preset (or a
  PubMed venue) one PubMed E-utilities query (the same boolean concept groups, [tiab]) joins OpenAlex / arXiv.
Stage C (one claude -p): Executive summary & evolution, Classic-vs-SOTA master table, per-paper digests, worded
  for the domain preset (presets.topic_prompt; ic_ece = the original prompt, digest_ver 'topic-v1'); the Mermaid
  lineage map is computed locally from OpenAlex references.
  v3.0.6, 0 tokens (--no-claude, no Claude CLI, or a failed call): the dive is still saved and written, with
  ranked 🏛️ Classics / 🚀 Recent tables (evidence badge, key numbers from the abstract) instead of the synthesis.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from datetime import date

from env_guard import ensure_minisforum_env

ensure_minisforum_env()
try:
    import arxiv_source  # Starter Edition: arXiv preprints (config "arxiv")
except ImportError:
    arxiv_source = None
import curation
import fetcher
import relations

UNIT_NW = {"pW": 1e-3, "nW": 1.0, "µW": 1e3, "μW": 1e3, "uW": 1e3, "mW": 1e6}
POWER_RX = re.compile(r"(\d+(?:\.\d+)?)\s?-?\s?(pW|nW|µW|μW|uW|mW)\b")
TEMP_SENSOR_RX = re.compile(r"(?i)temperature[- ](sensor|to[- ]digital)|thermal sensor")

PROMPT = """You are a senior analog/mixed-signal IC designer writing a longitudinal literature deep-dive in
English for a PhD researcher. Topic: "{query}".
Below are the selected papers (JSON): "group" is "classic" (foundational, highly cited) or "recent" (SOTA),
with title, venue, year, authors, citations, verification hint, extracted power numbers and abstract.
Use ONLY these abstracts; never invent numbers ("not stated" when missing). Measured silicon matters
most; mark simulation-only results with ⚠️ and never present them as state of the art.

Write Markdown starting directly with "## 1. Executive Summary & Evolution of Techniques" and nothing before:

## 1. Executive Summary & Evolution of Techniques
3–6 short paragraphs: the main technique families and how they evolved from the classics to the recent
papers (cite as [C1], [R3] etc. using the "id" field), what limits the state of the art, open problems.
## 2. Master Comparison Table (Classic vs. SOTA)
One Markdown table, all papers, classics first then recent, columns exactly:
Year & Venue | Paper & Authors | Verification | Process & Area | Power | Key Leakage/Low-Power Technique | FoM / Key Metric | DOI
(Paper & Authors = "[id] short title — first author … last author"; Verification = "✅ Silicon" / "⚠️ Sim" / "❔").
## 3. Per-Paper Technical Digests
### Foundational Classics
For each classic: "#### [id] Title (Venue Year)" then 2–4 bullets: core idea, key numbers, why it still matters.
### Recent SOTA
Same format; add one bullet on what it improves over the classics.
For papers with "recalled": true, write ONLY the "#### [id] Title (Venue Year)" heading line and no bullets:
their cached digest is inserted automatically (do still use them in sections 1 and 2).

Papers (JSON):
{papers}
"""


def slugify(q):
    return re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")[:60]


# v3.4.1: IC spellings made uniform before the split ("lowpower" → "low-power", "sub threshold" → "subthreshold")
COMPOUNDS = ((r"\bsub[\s-]?threshold\b", "subthreshold"), (r"\blow[\s-]?power\b", "low-power"),
             (r"\bultra[\s-]?low\b", "ultra-low"), (r"\bon[\s-]?chip\b", "on-chip"))
VARIANTS = {"subthreshold": ["subthreshold", "sub-threshold", "weak inversion"]}


def parse_query(q, cfg):
    """→ (concept groups [[terms]], power ceiling in nW or None). v3.4.1: COMPOUNDS normalised first; a hyphenated
    modifier of three or more words ("leakage-current-biased") becomes one concept per word, unless it is a
    configured synonym; two-word compounds ("switched-capacitor") stay one phrase (space or hyphen both match)."""
    tc = cfg["topic"]
    limit = None
    for rx, to in COMPOUNDS:
        q = re.sub(rx, to, q, flags=re.I)
    m = re.search(r"sub-?\s?(\d+(?:\.\d+)?)\s?(pW|nW|µW|μW|uW)", q, re.I)
    if m:
        unit = next(u for u in UNIT_NW if u.lower() == m[2].lower())
        limit = float(m[1]) * UNIT_NW[unit]
        q = q.replace(m[0], " ")
    groups, seen = [], set()
    syns = tc["synonyms"]
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9-]+", q):
        base = re.sub(r"^ultra-", "", re.sub(r"^low-", "", tok.lower()))
        parts = [base]
        if base.count("-") >= 2 and base not in syns and base.replace("-", " ") not in syns:
            parts = [x for x in base.split("-") if x]
        for b in parts:
            if b in tc["generic_words"] or b in seen or len(b) < 3:
                continue
            seen.add(b)
            syn = syns.get(b) or syns.get(b.rstrip("s"))
            groups.append(syn or VARIANTS.get(b) or [b])
    if limit is not None:
        groups.append(["re:power::\\d\\s?-?\\s?(nW|pW)\\b", "nanowatt", "picowatt", "ultra-low-power", "ultra-low power"])
    return groups, limit


def group_hits(groups, cfg):
    """v3.4.1: papers in the local store matching each concept group (FTS5, read only; None for a regex-only group
    or without an FTS index), for STARTUP.html's live preview: a 0 usually means a typo."""
    import sqlite3
    import store as store_mod
    sp = fetcher.HERE / cfg["paths"]["store_file"]
    out = [None] * len(groups)
    if not sp.exists():
        return out
    try:
        c = sqlite3.connect(f"file:{sp.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        for i, g in enumerate(groups):
            q = store_mod.fts_any(g)
            if q:
                out[i] = c.execute("SELECT COUNT(*) FROM papers_fts WHERE papers_fts MATCH ?", (q,)).fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out


# ---------- v3.4.2: Short Title + Scope paragraph → a planned dive spec ----------
# spec = {"title", "prompt", "core": [[term, synonym, …], …] (2–4 groups: a paper needs all but one),
#         "boosters": [term, …] (soft +score), "exclude": [term, …] (NOT), "power_nw": float|None, "seeds": [doi, …]}
BOOSTER_WORDS = {"power", "cmos", "bjt", "calibration", "calibrated", "trim", "trimming", "trimmed", "biased", "bias",
                 "efficient", "efficiency", "accurate", "accuracy", "compact", "robust", "fast", "digital", "analog",
                 "integrated", "chip", "on-chip", "process", "voltage", "supply", "area", "small", "wide", "range",
                 "resolution", "precision", "novel", "architecture", "energy-efficient", "measured", "silicon"}
ERA_SYNONYMS = {"temperature": ["PTAT", "CTAT", "bandgap"], "leakage": ["tunneling current"],
                "subthreshold": ["weak inversion"], "harvesting": ["energy scavenging"]}
EXCLUDE_RX = re.compile(r"\b(?:exclud(?:e|es|ing)|without|except|avoid(?:ing)?|not|no)\b[:\s]+([^.;\n]+)", re.I)
CN_EXCLUDE_RX = re.compile(r"(?:不要|排除|除了|不包括|不含|去掉)\s*([A-Za-z0-9 ,/&-]+)")
SCOPE_POWER_RX = re.compile(r"\b(?:below|under|less than|lower than|at most)\s*(\d+(?:\.\d+)?)\s?(pW|nW|µW|μW|uW)\b|[<≤]\s*(\d+(?:\.\d+)?)\s?(pW|nW|µW|μW|uW)\b", re.I)
# words of a scope sentence that are never a concept of their own
PROSE = set("""today now nowadays seed seeds below above under over from classic classics design designs modern recent
sota state art older newer era eras paper papers work works want wanted look like also both any only especially including
include includes such etc between across span spanning year years early late new old latest first foundational literature
review survey plus well than less more good best very into onto their them this that these those which where what how why
when who toward towards about around via per each other others its are was were been being have has had can could should
would will may might must shall does did need needs find show see give get use used uses focus focusing mainly mostly
interested interest topic topics related relevant include kind kinds type types way ways approach approaches
especially particularly like e.g i.e""".split())
DOI_RX = re.compile(r"\b10\.\d{4,9}/[^\s\"<>,;]+", re.I)
TERM_RX = re.compile(r"^[A-Za-z0-9µμ][A-Za-z0-9µμ .,'/+()&-]{0,59}$")


def _english(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if TERM_RX.match(t) else ""


def _era(group):
    out = list(group)
    for x in ERA_SYNONYMS.get(str(group[0]).lower(), []):
        if x.lower() not in {y.lower() for y in out}:
            out.append(x)
    return out


def _cap(w):
    return w if (w.isupper() or any(c.isdigit() for c in w)) else w[:1].upper() + w[1:]


def auto_title(core, power_nw=None):
    """'Sub-100nW Subthreshold Leakage Sensor': the first term of each core group (English, ≤ 6 words)."""
    words = []
    if power_nw:
        words.append(f"Sub-{power_nw:g}nW" if power_nw < 1000 else f"Sub-{power_nw / 1000:g}µW")
    for g in core:
        for w in str(g[0]).replace("-", " - ").split():
            if w != "-" and _english(w):
                words.append(_cap(w))
    return re.sub(r"\s+-\s+", "-", " ".join(words[:6])).strip()


def clean_title(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip()[:80]
    return t if t and re.match(r"^[A-Za-z0-9µμ][A-Za-z0-9µμ .,'/+()&:-]*$", t) else ""


def clean_spec(spec):
    """A spec from the page or --topic-spec, checked (English terms only), or ValueError."""
    if not isinstance(spec, dict):
        raise ValueError("spec: expected an object")
    core = spec.get("core") or []
    if not isinstance(core, list) or not 1 <= len(core) <= 4:
        raise ValueError("spec: 1–4 core concept groups")
    out_core = []
    for g in core:
        if not isinstance(g, list) or not 1 <= len(g) <= 16:
            raise ValueError("spec: a core group has 1–16 terms")
        terms = [_english(t) for t in g]
        if not all(terms):
            raise ValueError("spec: core terms are short English words or phrases (no other scripts)")
        out_core.append(list(dict.fromkeys(terms)))
    def terms(k, n):
        v = spec.get(k) or []
        if not isinstance(v, list) or len(v) > n:
            raise ValueError(f"spec: {k} is a list of at most {n} terms")
        x = [_english(t) for t in v]
        if not all(x):
            raise ValueError(f"spec: {k} are short English words or phrases")
        return list(dict.fromkeys(x))
    p = spec.get("power_nw")
    if p is not None and (isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p <= 1e9):
        raise ValueError("spec: power_nw is a number of nanowatts or empty")
    seeds = spec.get("seeds") or []
    if not isinstance(seeds, list) or len(seeds) > 10 or not all(isinstance(d, str) and DOI_RX.fullmatch(d.strip()) for d in seeds):
        raise ValueError("spec: seeds are up to 10 DOIs")
    prompt = str(spec.get("prompt") or "")
    if len(prompt) > 2000:
        raise ValueError("spec: the scope paragraph is at most 2,000 characters")
    return {"title": clean_title(spec.get("title")), "prompt": prompt.strip(), "core": out_core,
            "boosters": terms("boosters", 20), "exclude": terms("exclude", 20), "power_nw": p,
            "seeds": [d.strip().lower() for d in seeds]}


def plan_spec(prompt, cfg, title="", seeds=()):
    """🪄 Plan Concepts (0 tokens): the scope paragraph → spec. Concept words become groups (with synonyms across
    eras); modifiers, metric words and words with 0 local hits become soft boosters; "exclude / without / not …"
    clauses become exclude terms; DOIs become seeds. At most 4 core groups: the most specific (fewest local hits)."""
    text = str(prompt or "")
    sd = [d.rstrip(".)").lower() for d in DOI_RX.findall(text)] + [str(d).strip().lower() for d in seeds or [] if str(d).strip()]
    text = DOI_RX.sub(" ", text)
    excl = []
    for m in EXCLUDE_RX.finditer(text):
        for ph in re.split(r",|;|/|\bor\b|\band\b", m[1]):
            ph = _english(ph.strip().lower())
            if ph and len(ph) >= 3 and ph not in excl and ph not in cfg["topic"]["generic_words"]:
                excl.append(ph)
    for m in CN_EXCLUDE_RX.finditer(text):
        for ph in re.split(r",|/|\bor\b|\band\b", m[1]):
            ph = _english(ph.strip().lower())
            if ph and len(ph) >= 2 and ph not in excl:
                excl.append(ph)
    text = CN_EXCLUDE_RX.sub(" ", EXCLUDE_RX.sub(" ", text))
    text = SCOPE_POWER_RX.sub(lambda m: f" sub-{m[1] or m[3]}{m[2] or m[4]} ", text)  # "below 100 nW" → the power ceiling
    cjk = len(re.findall(r"[぀-ヿ㐀-鿿가-힯]", text))
    groups, limit = parse_query(text, cfg)
    if limit is not None:
        groups = groups[:-1]  # the power regex group: power_nw boosts instead
    groups = [g for g in ([_english(t) for t in g if _english(t)] for g in groups) if g and g[0].lower() not in PROSE]
    syns = {k.lower() for k in cfg["topic"]["synonyms"]} | set(VARIANTS) | set(ERA_SYNONYMS)
    merged = []
    for g in groups:  # a group another one already covers ("gate-leakage" in leakage's synonyms) is not a concept of its own
        if not any({t.lower() for t in g} & {t.lower() for t in m} for m in merged):
            merged.append(g)
    groups = merged
    hits = group_hits(groups, cfg)
    core, other, boost = [], [], []
    for i, (g, n) in enumerate(zip(groups, hits)):
        if g[0].lower() in BOOSTER_WORDS or n == 0:
            boost.append(g[0])  # a modifier or metric word, or 0 local hits (a typo?): soft
        elif g[0].lower() in syns or len(g) > 1:
            core.append(_era(g))  # a domain concept the radar knows synonyms for
        else:
            other.append((10 ** 9 if n is None else n, i, g))
    for n, i, g in sorted(other):  # plain words fill the core only while it has fewer than 2 groups
        (core.append(g) if len(core) < 2 else boost.append(g[0]))
    if len(core) > 4:
        boost += [g[0] for g in core[4:]]
        core = core[:4]
    note = ""
    if cjk and cjk > len(re.findall(r"[A-Za-z]", text)):
        note = "The scope is mostly not in English: ✨ AI Extract Concepts reads it and answers in English (or write the scope in English)."
    elif not core:
        note = "No concept words found: add the circuit or mechanism you are after."
    spec = {"title": clean_title(title) or auto_title(core, limit), "prompt": str(prompt or "").strip()[:2000],
            "core": core, "boosters": list(dict.fromkeys(boost))[:20], "exclude": excl[:20], "power_nw": limit,
            "seeds": list(dict.fromkeys(d for d in sd if DOI_RX.fullmatch(d)))[:10], "source": "0-token", "note": note}
    return spec


def spec_info(spec, cfg):
    """Live preview numbers: local FTS5 hits per core group and booster, and how many local papers pass the core
    gate (all groups but one, no exclude term), split into 🏛️ classics (< split year) and 🚀 recent."""
    import sqlite3
    import store as store_mod
    core = spec.get("core") or []
    out = {"core_hits": group_hits(core, cfg), "booster_hits": group_hits([[b] for b in spec.get("boosters") or []], cfg),
           "exclude_hits": group_hits([[b] for b in spec.get("exclude") or []], cfg),
           "pool": {"classic": 0, "recent": 0, "split": cfg["topic"]["recent_from_year"], "min_year": min_year(cfg)}}
    sp = fetcher.HERE / cfg["paths"]["store_file"]
    q = store_mod.fts_any([t for g in core for t in g])
    if not sp.exists() or not q:
        return out
    need, split, y0 = max(1, len(core) - 1), cfg["topic"]["recent_from_year"], min_year(cfg)
    try:
        c = sqlite3.connect(f"file:{sp.as_posix()}?mode=ro", uri=True, timeout=5)
        rows = c.execute("SELECT p.title, p.abstract, p.index_terms, p.published FROM papers_fts f JOIN papers p "
                         "ON p.rowid = f.rowid WHERE papers_fts MATCH ? LIMIT 30000", (q,)).fetchall()
        c.close()
    except sqlite3.Error:
        return out
    for title, abstract, terms, pub in rows:
        try:
            it = " ".join(json.loads(terms)) if terms else ""
        except ValueError:
            it = ""
        text = f"{title} {abstract or ''} {it}"
        if sum(1 for g in core if any(fetcher._kw_hit(t, text) for t in g)) < need:
            continue
        if any(fetcher._kw_hit(x, f"{title} {abstract or ''}") for x in spec.get("exclude") or []):
            continue
        y = (relations._ym(pub) or 0) // 12
        if y0 and y and y < y0:
            continue
        out["pool"]["classic" if y and y < split else "recent"] += 1
    return out


AI_PROMPT = """You plan a literature search for an engineering researcher (default field: analog / mixed-signal IC design).
Read the scope below. It may be written in Chinese or mixed languages. Answer with ONE JSON object and nothing else,
in ENGLISH ONLY:
{{"title": "2-6 word English title", "core_groups": [["term", "synonym", "..."], ...], "boosters": ["term", ...],
 "power_ceiling_nw": number or null, "exclude": ["term", ...]}}
Rules: 2-4 core_groups, each 2-6 English synonyms that a paper title or abstract would use, bridging older (2000s-2010s)
and current terminology (e.g. "subthreshold", "weak inversion"; "gate leakage", "tunneling current"; "PTAT", "bandgap").
boosters = optional modifiers or metrics (they only raise the ranking). exclude = topics the scope says to leave out.
Lowercase terms except acronyms. No regular expressions.
Scope: {scope}
Title hint (may be empty): {title}"""


def ai_extract(prompt, cfg, title="", seeds=()):
    """✨ AI Extract Concepts: one small `claude -p --model haiku` call → an English spec (checked like a typed one)."""
    import subprocess
    import sys
    cc = cfg.get("claude") or {}
    cmd = [cc.get("command") or "claude", "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config",
           "--model", "haiku"]
    r = subprocess.run(cmd, input=AI_PROMPT.format(scope=str(prompt or "")[:2000], title=clean_title(title)),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
                       cwd=fetcher.HERE, shell=(sys.platform == "win32"))
    if r.returncode != 0 or "{" not in (r.stdout or ""):
        raise RuntimeError(f"claude -p (haiku) failed (exit {r.returncode}): {(r.stderr or r.stdout or '')[-300:]}")
    t = r.stdout[r.stdout.index("{"):r.stdout.rindex("}") + 1]
    try:
        j = json.loads(t)
    except ValueError:
        raise RuntimeError("claude answered no readable JSON: try again or use 🪄 Plan Concepts")
    core = [[_english(x) for x in g if _english(x)][:6] for g in (j.get("core_groups") or []) if isinstance(g, list)]
    core = [g for g in core if g][:4]
    p = j.get("power_ceiling_nw")
    base = plan_spec(prompt, cfg, title, seeds)  # seeds, the exclude clauses and a fallback title
    spec = {"title": clean_title(title) or clean_title(j.get("title")) or auto_title(core, p) or base["title"],
            "prompt": str(prompt or "").strip()[:2000], "core": core or base["core"],
            "boosters": [b for b in (_english(x) for x in j.get("boosters") or []) if b][:20],
            "exclude": list({b.lower(): b for b in reversed([b for b in (_english(x) for x in j.get("exclude") or []) if b] + base["exclude"])}.values())[::-1][:20],
            "power_nw": p if isinstance(p, (int, float)) and not isinstance(p, bool) and 0 < p <= 1e9 else base["power_nw"],
            "seeds": base["seeds"], "source": "haiku"}
    return spec


def _powers_nw(text):
    return [float(n) * UNIT_NW[u] for n, u in POWER_RX.findall(text or "")]


def relevance(p, groups, limit, cfg, spec=None):
    """v3.4.2 with a spec: groups = its core groups; an exclude term in the title / abstract rules the paper out;
    boosters (+1 each, ≤ +4) and seed DOIs (the paper itself +6, citing one +3) only raise the ranking."""
    text = f"{p['title']} {p.get('abstract') or ''} {' '.join(p.get('index_terms') or [])}"
    if spec and any(fetcher._kw_hit(x, f"{p['title']} {p.get('abstract') or ''}") for x in spec.get("exclude") or []):
        return 0, 0
    hit_groups = sum(1 for g in groups if any(fetcher._kw_hit(t, text) for t in g))
    need = max(1, len(groups) - 1)
    if hit_groups < need:
        return 0, hit_groups
    title_hits = sum(1 for g in groups if any(fetcher._kw_hit(t, p["title"]) for t in g))
    # every concept matters (−4 per miss); a concept in the title says what the paper is *about* (+2)
    r = 3 * hit_groups - 4 * (len(groups) - hit_groups) + 2 * title_hits
    pw = _powers_nw(text)
    if limit is not None:  # the power ceiling is the point of e.g. "sub-100nW" …
        if pw and min(pw) <= limit:
            r += 4
        elif TEMP_SENSOR_RX.search(p["title"]):
            # … but a classic temperature sensor a bit above it (or with no power in the abstract) is still a
            # reference design: −1 up to 10× the ceiling or unstated, −2 beyond (v2.9; was −3 for every paper)
            r -= 1 if not pw or min(pw) <= 10 * limit else 2
        else:
            r -= 3
    v = fetcher.verification(p, cfg) if p.get("abstract") else "unclear"
    r += {"silicon_verified": 1, "simulation_only": -4}.get(v, 0)
    r += min(2, max(0, fetcher.score(p, cfg)[0]) // 6)
    if spec:
        r += min(4, sum(1 for b in spec.get("boosters") or [] if fetcher._kw_hit(b, text)))
        seeds = set(spec.get("seeds") or [])
        if p.get("doi", "").lower() in seeds:
            r += 6
        elif seeds & {str(x)[4:] if str(x).startswith("doi:") else str(x) for x in p.get("refs") or []}:
            r += 3
    return max(r, 0), hit_groups


def _venue_filter(cfg):
    issns = [i for v in cfg["venues"] for i in v["issn"]]
    return "primary_location.source.issn:" + "|".join(issns)


def _venue_of(name, cfg):
    for v in cfg["venues"]:
        rx = v.get("container_regex") or re.escape(v["name"].replace("IEEE ", ""))
        if re.search(rx, name or "", re.I):
            return v
    return None


def min_year(cfg):
    """v3.2.3 topic.min_year: papers published before it are ignored everywhere (0 / blank / <= 1900 = all years)."""
    try:
        y = int((cfg.get("topic") or {}).get("min_year") or 0)
    except (TypeError, ValueError):
        return 0
    return y if y > 1900 else 0


def year_of(p):
    return (relations._ym(p.get("published")) or 0) // 12


def in_years(p, cfg):
    """True unless topic.min_year is set and the paper is older (or has no year)."""
    y0 = min_year(cfg)
    return not y0 or year_of(p) >= y0


def openalex_search(groups, cfg, log):
    """Classics (by citations, before recent_from_year), recent work, and the configured anchor papers
    (they compete on relevance like everything else), in the IC venues."""
    tc = cfg["topic"]
    heads = [next((t for t in g if not t.startswith("re:")), "nanowatt") for g in groups]
    variants = {" ".join(heads)} | {" ".join(heads[:i] + heads[i + 1:]) for i in range(len(heads)) if len(heads) > 2}
    y0, lo = tc["recent_from_year"], min_year(cfg)
    spans = [] if lo >= y0 else [(f"publication_year:{lo}-{y0 - 1}" if lo else f"publication_year:<{y0}", "cited_by_count:desc")]
    spans.append((f"publication_year:>{max(y0, lo) - 1}", None))
    queries = [({"filter": f"{_venue_filter(cfg)},{flt}", "search": s, **({"sort": sort} if sort else {})})
               for s in variants for flt, sort in spans]
    if cfg.get("anchors"):
        queries.append({"filter": "doi:" + "|".join(a.lower() for a in cfg["anchors"])})
    out = {}
    for base in queries:
        params = {**base, "per-page": tc["openalex_per_query"], "mailto": cfg["contact_email"],
                  "select": "id,doi,display_name,publication_year,publication_date,authorships,primary_location,"
                            "abstract_inverted_index,referenced_works,cited_by_count"}
        try:
            res = json.loads(fetcher._get("https://api.openalex.org/works?" + urllib.parse.urlencode(params), cfg))["results"]
        except Exception as e:
            log(f"  [topic/openalex] '{base.get('search', 'anchors')}': {e}")
            continue
        for w in res:
            doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name", "")
            v = _venue_of(src, cfg)
            if not doi or not v:
                continue
            out.setdefault(doi, {"doi": doi, "title": fetcher._clean(w.get("display_name")), "venue": v["short"],
                                 "venue_name": v["name"], "published": w.get("publication_date") or str(w.get("publication_year")),
                                 "authors": [(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []],
                                 "abstract": fetcher._oa_abstract(w.get("abstract_inverted_index")),
                                 "oa_id": w["id"].rsplit("/", 1)[-1], "cited_by": w.get("cited_by_count") or 0,
                                 "refs": ["oa:" + x.rsplit("/", 1)[-1] for x in w.get("referenced_works") or []],
                                 "source": "openalex"})
    log(f"  OpenAlex: {len(out)} papers from {len(queries)} queries (incl. anchors)")
    return out


def ieee_search(groups, cfg, store, budget, log):
    """One boolean query over all of IEEE, filtered to our venues by publication title."""
    def term(t):
        return '"nW" OR "pW" OR "nanowatt"' if t.startswith("re:") else f'"{t}"'
    q = " AND ".join("(" + " OR ".join(term(t) for t in g[:4]) + ")" for g in groups)
    params = {"querytext": q, "max_records": 200}
    if min_year(cfg):
        params["start_year"] = min_year(cfg)  # IEEE Xplore API filter (v3.2.3)
    r = fetcher._ieee(params, cfg, store, log, api="ieee_topic", budget=budget)
    out = {}
    for a in (r or {}).get("articles") or []:
        v = _venue_of(a.get("publication_title", ""), cfg)
        if a.get("doi") and v:
            p = fetcher._from_ieee(a, v)
            p["cited_by"] = a.get("citing_paper_count") or 0
            out[p["doi"]] = p
    log(f"  IEEE boolean query: {len((r or {}).get('articles') or [])} hits, {len(out)} in our venues")
    return out


def ieee_active(cfg):
    """IEEE Stage B runs only with a key and at least one IEEE venue (ieee_query, or 'IEEE' in its name)."""
    import os
    return bool(os.environ.get("IEEE_API_KEY", "").strip()) and any(
        v.get("ieee_query") or "IEEE" in (v.get("name") or "") for v in cfg.get("venues") or [])


def pubmed_active(cfg):
    import presets
    return presets.key(cfg) == "bio_med" or any(v.get("source") == "pubmed" for v in cfg.get("venues") or [])


def pubmed_term(groups):
    """The concept groups as one PubMed boolean query: (a[tiab] OR b[tiab]) AND (…); regex-only groups dropped."""
    parts = []
    for g in groups:
        ts = [t for t in g if not str(t).startswith("re:")][:4]
        if ts:
            parts.append("(" + " OR ".join(f'"{t}"[tiab]' for t in ts) + ")")
    return " AND ".join(parts)


def pubmed_search(groups, cfg, log, retmax=100):
    """v3.0.6: one esearch (sort=relevance) + one efetch over all of PubMed for the topic's concept groups."""
    term = pubmed_term(groups)
    if not term:
        return {}
    v = next((x for x in cfg.get("venues") or [] if x.get("source") == "pubmed"),
             {"short": "PubMed", "name": "PubMed (NCBI E-utilities)", "source": "pubmed"})
    common = {"db": "pubmed", "tool": "paper-radar-topic", **({"email": cfg["contact_email"]} if cfg.get("contact_email") else {})}
    out = {}
    try:
        q = {**common, "term": term, "retmax": retmax, "sort": "relevance", "retmode": "json"}
        if min_year(cfg):  # v3.2.3: PubMed needs both ends of a date range
            q.update(datetype="pdat", mindate=str(min_year(cfg)), maxdate="3000")
        ids = json.loads(fetcher._get(fetcher.EUTILS + "esearch.fcgi?" + urllib.parse.urlencode(q), cfg))["esearchresult"].get("idlist", [])
        if ids:
            import time
            import xml.etree.ElementTree as ET
            time.sleep(0.4)  # 3 requests per second without an NCBI key
            xml = fetcher._get(fetcher.EUTILS + "efetch.fcgi?" + urllib.parse.urlencode({**common, "id": ",".join(ids), "retmode": "xml"}), cfg)
            for a in ET.fromstring(xml).iter("PubmedArticle"):
                p = fetcher._pubmed_record(a, v)
                if p and p.get("doi"):
                    out[p["doi"]] = {**p, "cited_by": 0}
    except Exception as e:
        log(f"  [topic/pubmed] {e}")
    log(f"  PubMed: {len(out)} papers for {term}")
    return out


def select(papers, cfg):
    tc = cfg["topic"]
    y0, lo = tc["recent_from_year"], min_year(cfg)
    year = year_of
    papers = [p for p in papers if in_years(p, cfg)]
    classic = sorted((p for p in papers if p["rel"] > 0 and max(0, lo - 1) < year(p) < y0),
                     key=lambda p: -(p["rel"] + math.log10(1 + (p.get("cited_by") or 0))))  # relevance first, citations break ties
    recent = sorted((p for p in papers if p["rel"] > 0 and year(p) >= y0), key=lambda p: -p["rel"])
    return classic[:tc["n_classic"]], recent[:tc["n_recent"]]


def mermaid(classic, recent):
    nodes = [(f"C{i}", p) for i, p in enumerate(classic, 1)] + [(f"R{i}", p) for i, p in enumerate(recent, 1)]
    keys = {nid: relations._keys(p) for nid, p in nodes}
    edges = [(a, b) for a, pa in nodes for b, _ in nodes if a != b and keys[b] & set(pa.get("refs") or [])]
    if not edges:
        return ""
    short = lambda t: re.sub(r'["\[\]()]', "", t)[:42]
    used = {n for e in edges for n in e}
    L = ["```mermaid", "graph LR"]
    L += [f'  {nid}["{nid} {p["venue"]} {(relations._ym(p["published"]) or 0) // 12}: {short(p["title"])}"]'
          for nid, p in nodes if nid in used]
    L += [f"  {a} -->|cites| {b}" for a, b in edges]
    return "\n".join(L + ["```"])


def _decorate(body, by_id, recalled):
    """After each '#### [id] …' digest heading: ♻️ tag + cached digest for recalled papers; then, before the
    next heading, the paper's ⭐/🗄️ curation line."""
    out, cur = [], None

    def close():
        if cur:
            out.append(curation.line(by_id[cur]))
            out.append("")
    for ln in body.splitlines():
        m = re.match(r"^#{3,4} \[([CR]\d+)\]", ln)
        if m or re.match(r"^#{1,3} ", ln):
            close()
            cur = None
        if m and m[1] in by_id:
            p = by_id[m[1]]
            cur = m[1]
            if p["doi"] in recalled:
                out += [ln + " ♻️ [Recalled from Archive]"] + ([p["digest"]] if p.get("digest") else [])
                continue
        out.append(ln)
    close()
    return "\n".join(out)


def local_candidates(store, groups):
    """The store's papers that can pass relevance(): with FTS5 only those matching any plain term of the groups
    (bm25 order, 'rank' set), else every paper. relevance() needs all groups but one, so the prefilter is exact
    when at most one group is regex-only (re:…); otherwise, or without groups, every paper is checked."""
    import store as store_mod
    n = len(groups)
    rx_only = sum(1 for g in groups if all(str(t).startswith("re:") for t in g))
    if not getattr(store, "fts", False) or not n or rx_only > (1 if n >= 2 else 0):
        return store.where("1=1")
    q = store_mod.fts_any([t for g in groups for t in g])
    return store.fts_search(None, limit=1_000_000, match=q) if q else store.where("1=1")


def recall(query, cfg, store, log=print, top=25, spec=None, title=None):
    """Local-only search over the whole store (archived marks and the backfilled corpus included):
    0 API calls, 0 tokens. Writes Topic_Dives/<date>_recall_<slug>.md with curation boxes."""
    if spec:  # v3.4.2: the planned core groups / boosters / exclude terms
        groups, limit = [list(g) for g in spec["core"]], spec.get("power_nw")
        query = title or spec.get("title") or query
    else:
        groups, limit = parse_query(query, cfg)
    hits = []
    for p in local_candidates(store, groups):  # v3.0: FTS5 prefilter (bm25 breaks ties)
        if not in_years(p, cfg):  # v3.2.3 topic.min_year
            continue
        r = relevance(p, groups, limit, cfg, spec)[0]
        if r > 0:
            hits.append((r, p))
    hits.sort(key=lambda x: (-x[0], x[1].get("rank") or 0, str(x[1].get("published") or "")))
    L = [f"# Recall: {query}", "", f"*{date.today():%d %b %Y} · local store only ({len(hits)} matches, top {top} shown"
         + (f", published {min_year(cfg)} or later" if min_year(cfg) else "") + ") · 0 API calls, 0 tokens*", ""]
    for r, p in hits[:top]:
        tag = " ♻️ [Recalled from Archive]" if (p.get("user_mark") == "archived") else (" 📚 corpus" if p["status"] == "archive" else "")
        L += [f"- **{r}** · {p['venue']} {str(p.get('published') or '')[:10]} · [{p['title']}](https://doi.org/{p['doi']}){tag}",
              f"  {curation.line(p)}"]
        log(f"  {r:>3} {p['venue']:<8} {str(p.get('published') or '')[:10]:<10} {p['title'][:80]}{tag}")
    from pathlib import Path
    out_dir = Path(cfg["paths"]["topic_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{date.today().isoformat()}_recall_{slugify(query)}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"Wrote {out}")
    return out


def _key_numbers(p):
    import catalog
    return catalog.key_numbers(p)


def _cell(x):
    return str(x or "").replace("|", "\\|").replace("\n", " ").strip()


def zero_token_body(classic, recent, sel, cfg):
    """v3.0.6: the dive without claude -p: ranked tables (evidence badge from the abstract cues, key numbers from the
    abstract) and the cached digests of papers that already have one. Sections 1–3; the caller adds 4 (lineage)."""
    import catalog
    import presets
    b = presets.badge(cfg)
    ids = {p["doi"]: pid for _, pid, p in sel}
    y0 = cfg["topic"]["recent_from_year"]

    def table(ps):
        if not ps:
            return ["_None found._"]
        rows = [f"| # | Year & Venue | Paper & Authors | {b['column']} | Key Numbers (from the abstract) | Cited | Relevance | DOI |",
                "|---|---|---|---|---|---|---|---|"]
        for p in ps:
            au = p.get("authors") or []
            who = (au[0] + (" … " + au[-1] if len(au) > 1 else "")) if au else ""
            yr = (relations._ym(p["published"]) or 0) // 12
            rows.append(f"| {ids[p['doi']]} | {yr} {_cell(p.get('venue'))} | {_cell(p['title'])}" + (f" — {_cell(who)}" if who else "")
                        + f" | {catalog.silicon(p, cfg)} | {_cell(_key_numbers(p)) or 'not stated'} | {p.get('cited_by') or 0} "
                        f"| {p.get('rel', 0)} | [{p['doi']}](https://doi.org/{p['doi']}) |")
        return rows
    L = ["## 1. Overview (0-token mode)", "",
         f"_No AI synthesis (--no-claude, no Claude CLI, or the call failed): papers are ranked by local relevance, the "
         f"{b['column']} badge ({b['si_icon']} {b['si']} · {b['sim_icon']} {b['sim']} · ❔ unclear) comes from the abstract's "
         f"cues and the key numbers are extracted from it. Run the dive again with the Claude CLI for the executive summary, "
         f"the master comparison table and per-paper digests._", "",
         f"## 2. 🏛️ Foundational Classics (before {y0}, relevance × citations)", "", *table(classic), "",
         f"## 3. 🚀 Recent SOTA ({y0}–, by relevance)", "", *table(recent), ""]
    cached = [(pid, p) for _, pid, p in sel if p.get("digest")]
    if cached:
        L += ["### Cached digests (from earlier runs, 0 tokens)", ""]
        for pid, p in cached:
            # a recalled (🗄️ archived) paper gets its digest from _decorate, after the heading
            L += [f"#### [{pid}] {p['title']} ({p.get('venue') or ''} {(relations._ym(p['published']) or 0) // 12})"]
            L += ([] if p.get("user_mark") == "archived" else [p["digest"]]) + [""]
    return "\n".join(L)


def run(query, cfg, store, max_ieee, claude, log=print, use_claude=True, spec=None, title=None):
    tc = cfg["topic"]
    today = date.today()
    if spec:  # v3.4.2: Short Title + planned spec (core groups gate, boosters / seeds rank, exclude terms filter)
        groups, limit = [list(g) for g in spec["core"]], spec.get("power_nw")
        title = clean_title(title) or spec.get("title") or clean_title(query)
        query = title or query
        log(f"Topic: {title}\n  core groups: {[g[:4] for g in groups]}  boosters: {spec.get('boosters')}  "
            f"exclude: {spec.get('exclude')}  seeds: {len(spec.get('seeds') or [])}  power ceiling: {limit} nW")
    else:
        groups, limit = parse_query(query, cfg)
        log(f"Topic: {query}\n  concept groups: {[g[:3] for g in groups]}  power ceiling: {limit} nW")

    # Stage A — local store + OpenAlex (0 IEEE calls)
    found = {p["doi"]: p for p in local_candidates(store, groups)}  # v3.0: FTS5 prefilter
    found = {d: p for d, p in found.items() if in_years(p, cfg) and relevance(p, groups, limit, cfg, spec)[0] > 0}
    log(f"  local store: {len(found)} relevant" + (f" (published {min_year(cfg)} or later)" if min_year(cfg) else ""))
    for d, p in openalex_search(groups, cfg, log).items():
        found.setdefault(d, p)
    if pubmed_active(cfg):  # v3.0.6: bio_med / a PubMed venue
        for d, p in pubmed_search(groups, cfg, log).items():
            found.setdefault(d, p)
    if arxiv_source and arxiv_source.enabled(cfg):  # preprints from arXiv's official API (config "arxiv")
        for d, p in arxiv_source.topic_search(groups, cfg, store, log).items():
            found.setdefault(d, p)

    # Stage B — IEEE reserve (skipped without IEEE_API_KEY or without IEEE venues: OpenAlex / PubMed results only)
    if hasattr(fetcher, "ieee_notice"):
        fetcher.ieee_notice(log)
    budget = min(tc["ieee_topic_reserve"], store.calls_today("ieee_topic") + (max_ieee or tc["default_max_ieee"]))
    use_ieee = ieee_active(cfg)
    if not use_ieee:
        log("  IEEE: skipped (no IEEE_API_KEY or no IEEE venue configured)")
        budget = 0
    for d, p in (ieee_search(groups, cfg, store, budget, log) if use_ieee else {}).items():
        q = found.setdefault(d, p)
        for k in ("abstract", "index_terms", "article_number"):
            if p.get(k) and not q.get(k):
                q[k] = p[k]
        q["cited_by"] = max(q.get("cited_by") or 0, p.get("cited_by") or 0)
    if min_year(cfg):  # v3.2.3: one filter for every source (arXiv, anchors and unknown dates included)
        n0 = len(found)
        found = {d: p for d, p in found.items() if in_years(p, cfg)}
        log(f"  topic.min_year {min_year(cfg)}: {n0 - len(found)} older papers dropped")
    for p in found.values():
        p["rel"] = relevance(p, groups, limit, cfg, spec)[0]
    pre_c, pre_r = select(list(found.values()), cfg)
    for p in pre_c + pre_r:  # fill missing abstracts of the shortlist, then re-rank
        if use_ieee and not p.get("abstract") and p["doi"].startswith("10.1109/"):
            r = fetcher._ieee({"doi": p["doi"]}, cfg, store, log, api="ieee_topic", budget=budget)
            arts = (r or {}).get("articles") or []
            if arts and arts[0].get("abstract"):
                p["abstract"] = fetcher._clean(arts[0]["abstract"])
                p["rel"] = relevance(p, groups, limit, cfg, spec)[0]
    classic, recent = select(list(found.values()), cfg)
    log(f"  IEEE topic calls today: {store.calls_today('ieee_topic')}/{tc['ieee_topic_reserve']}; "
        f"selected {len(classic)} classics + {len(recent)} recent")
    if not classic and not recent:  # v3.4.1: no empty "0 papers" dive in meta['topics'], no empty report
        hits = group_hits(groups, cfg)
        zero = [g[0] for g, n in zip(groups, hits) if n == 0]
        log(f"⚠️ No paper matched “{query}”: nothing saved (no topic dive, no report)."
            + (f" These words have 0 hits in your store, check the spelling: {', '.join(zero)}." if zero else
               " Try fewer or broader words (a paper must match all concepts but one)."))
        return None

    # store everything relevant (new rows: status 'topic'), remember the selection
    for p in found.values():
        if p["rel"] > 0:
            row = {k: v for k, v in p.items() if k != "rel"}
            row["score"], row["hits"] = fetcher.score(p, cfg)
            store.upsert({**row, "first_seen": today.isoformat()}, "topic")
    slug = slugify(query)
    sel = [("classic", f"C{i}", p) for i, p in enumerate(classic, 1)] + [("recent", f"R{i}", p) for i, p in enumerate(recent, 1)]
    for _, _, p in sel:  # curation state + cached digest live in the store
        row = store.get(p["doi"]) or {}
        p["user_mark"], p["digest"] = row.get("user_mark") or "normal", row.get("digest") or p.get("digest")
    recalled = {p["doi"] for _, _, p in sel if p["user_mark"] == "archived"}
    if recalled:
        log(f"  ♻️ recalled from archive: {len(recalled)}")
    topics = store.meta("topics") or {}
    topics[slug] = {"query": query, "date": today.isoformat(), "dois": [p["doi"] for _, _, p in sel],
                    "n_classic": len(classic),  # dois = classics, then recent (dashboard 🏛️/🚀 badges)
                    "years": {"min_year": min_year(cfg), "split_year": tc["recent_from_year"]}}
    if spec:  # v3.4.2: ✏️ Edit / Re-run loads these back into STARTUP.html
        topics[slug].update(title=query, prompt=spec.get("prompt") or "",
                            spec={k: spec.get(k) for k in ("core", "boosters", "exclude", "power_nw", "seeds")})
    store.meta("topics", topics)

    # Stage C — one claude call (worded for the domain preset), or the 0-token tables
    import presets
    ic = presets.key(cfg) not in presets.TOPIC
    payload = [{"id": pid, "group": g, "title": p["title"], "venue": p["venue"],
                "year": (relations._ym(p["published"]) or 0) // 12, "authors": p.get("authors", [])[:8],
                "citations": p.get("cited_by") or 0, "doi": p["doi"],
                "verification_hint": fetcher.verification(p, cfg) if p.get("abstract") else "unclear",
                **({"power_numbers": sorted({f"{x:g} nW" for x in _powers_nw(p.get('abstract') or '')})[:6]} if ic else
                   {"key_numbers": _key_numbers(p)}),
                **({"recalled": True, "cached_digest": p["digest"]} if p["doi"] in recalled and p.get("digest") else
                   {"abstract": p.get("abstract") or "(no abstract available — title only)"})} for g, pid, p in sel]
    body = None
    if use_claude and sel:
        log(f"Calling claude -p for the topic synthesis ({len(sel)} papers) ...")
        try:
            body = claude(presets.topic_prompt(PROMPT, cfg).format(query=query, papers=json.dumps(payload, ensure_ascii=False, indent=1)), cfg)
            body = body[body.find("## 1."):] if "## 1." in body else body
        except Exception as e:  # never lose the dive: fall back to the 0-token tables
            log(f"  claude -p failed ({e}): writing the 0-token report instead")
            body = None
    if body is None:
        body = zero_token_body(classic, recent, sel, cfg)
    else:
        # cache each per-paper digest (where none exists) so a later recall can reuse it at 0 tokens
        by_id = {pid: p for _, pid, p in sel}  # (claude branch only)
        for pid, text in re.findall(r"^#{3,4} \[([CR]\d+)\][^\n]*\n(.*?)(?=^#{1,4} |\Z)", body, flags=re.M | re.S):
            p = by_id.get(pid)
            row = store.get(p["doi"]) if p else None
            if row and not row.get("digest") and text.strip():
                store.update(p["doi"], {"digest": text.strip(), "digest_ver": presets.topic_ver(cfg)})
    body = _decorate(body, {pid: p for _, pid, p in sel}, recalled)
    graph = mermaid(classic, recent)
    L = [f"# Topic Deep-Dive: {query}", "",
         f"*{today:%d %b %Y} · {len(classic)} foundational classics ("
         + (f"{min_year(cfg)}–{tc['recent_from_year'] - 1}" if min_year(cfg) else f"before {tc['recent_from_year']}")
         + ", ranked by relevance × "
         f"citations) + {len(recent)} recent papers · sources: local store, OpenAlex"
         + (", PubMed" if pubmed_active(cfg) else "")
         + (f", IEEE Xplore API ({store.calls_today('ieee_topic')} topic calls today)" if use_ieee else "") + "*", "",
         *([f"**Scope:** {spec['prompt']}", ""] if spec and spec.get("prompt") else []),
         f"**{'Core concepts (all but one)' if spec else 'Query parsed as'}:** {' AND '.join('(' + ' | '.join(t.split('::')[0][3:] if t.startswith('re:') else t for t in g[:4]) + ')' for g in groups)}"
         + (f" · power ≤ {limit:g} nW boosts relevance" if limit else "")
         + (f" · 🚀 boosters: {', '.join(spec['boosters'])}" if spec and spec.get("boosters") else "")
         + (f" · 🚫 excluded: {', '.join(spec['exclude'])}" if spec and spec.get("exclude") else "")
         + (f" · 🌱 {len(spec['seeds'])} seed DOI(s)" if spec and spec.get("seeds") else ""), "", body, "",
         "## 4. Lineage & Citation Map", ""]
    L += ([graph, "", "Arrows: the left paper's reference list cites the right one (OpenAlex)."] if graph else
          ["_No citation links among the selected papers in OpenAlex (reference lists missing or no direct citations)._"])
    L += ["", "## Paper list", ""]
    for _, pid, p in sel:
        L += [f"- **[{pid}]**{' ♻️ [Recalled from Archive]' if p['doi'] in recalled else ''} {p['title']} — "
              f"*{p.get('venue_name') or p.get('venue') or ''}*, {p['published']} · cited {p.get('cited_by') or 0}× · "
              f"[{p['doi']}](https://doi.org/{p['doi']})",
              f"  {curation.line(p)}"]
    from pathlib import Path
    out_dir = Path(cfg["paths"]["topic_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{today.isoformat()}_{slug}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    log(f"Wrote {out}")
    return out
