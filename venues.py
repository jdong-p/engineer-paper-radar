"""Watched venues (v3.3.0): the curated catalog, the venue / container_regex guard and the live venue check.

STARTUP.html → Step 1 → 📚 Watched Venues edits config.json "venues" and catch_up.flagship_venues. Every entry
passes check_venues() (control_center.validate and, again, control_center.write_config):

  - short code, one-line name, type journal | conference;
  - ISSNs with a valid check digit; a journal needs one (Crossref is queried by ISSN);
  - a conference needs a container_regex that passes check_regex(): it compiles (re.I), is at most 200
    characters, has no nested repetition such as (a+)+ and no backreference (catastrophic backtracking), does
    not match an empty title or any SENTINELS title (so .*, .+, IEEE, \\d{4} or a single word like Circuits
    are refused), and names at least one distinctive word;
  - ieee_query / doi_prefix / source / PubMed and bioRxiv keys of the right shape; unique short codes; ≤ 40.

verify() (POST /api/verify_venue, 0 API keys) asks Crossref and OpenAlex before a venue is added:
  journal    → official title, publisher, print / electronic ISSN, OpenAlex work count, 3 recent papers;
  conference → the candidate regex run against recent Crossref container titles and OpenAlex source names:
               ✅ matched titles + 3 recent matching papers, 🛡️ excluded lookalikes.

ieee_params() is what fetcher.py sends to the IEEE API: the hand-made ieee_query, else (IEEE venues only) the
journal's ISSN or the conference's title, so a venue added here works without a looked-up publication_number.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

MAX_VENUES = 40
MAX_RX = 200
SHORT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&+/_-]{0,19}$")
ISSN_RX = re.compile(r"^\d{4}-?\d{3}[\dXx]$")
PREFIX_RX = re.compile(r"^10\.\d{4,9}$")
SOURCES = ("pubmed", "biorxiv", "medrxiv")
IEEE_KEYS = ("publication_number", "publication_title", "content_type", "issn")
KNOWN_KEYS = ("short", "name", "issn", "type", "publisher", "source", "query", "max_results", "categories",
              "max_pages", "ieee_query", "title_query", "container_regex", "doi_prefix")
# unrelated titles no watched-venue regex may match (anti-flood): generic words, a bare year, IEEE alone
SENTINELS = (
    "2026 IEEE International Workshop on General Computing",
    "2025 IEEE 3rd International Conference on Circuits, Devices, Sensors, Systems, Signals and Electronics (ICCDSSSE)",
    "Proceedings of the 2024 ACM Symposium on Miscellaneous Topics in Design, Automation, Power and Microwave Technology",
    "2023 International Symposium on Unrelated Studies (ISUS 2023)",
    "IEEE Access",
    "Journal of Physics: Conference Series",
)
GENERIC = {"ieee", "acm", "international", "conference", "symposium", "proceedings", "journal", "transactions",
           "letters", "annual", "workshop", "meeting", "digest", "technical", "papers", "society", "the", "and",
           "for", "world", "congress", "forum", "series", "national", "edition", "advances", "research", "science",
           "engineering", "technology", "systems", "studies", "applications", "review", "reviews"}
CONF_WORDS = re.compile(r"\b(conference|symposium|meeting|workshop|congress|proceedings|digest)\b", re.I)
TIMEOUT = 40


def _c(name, short, group, field, issn=(), regex=None, ieee=True, prefix=None, title_query=None, ieee_query=None):
    v = {"short": short, "name": name, "issn": list(issn), "type": "conference" if regex else "journal"}
    if ieee:
        v["publisher"] = "IEEE"
    if ieee_query:
        v["ieee_query"] = ieee_query
    if title_query:
        v["title_query"] = title_query
    if regex:
        v["container_regex"] = regex
    if prefix:
        v["doi_prefix"] = list(prefix)
    return {"group": group, "field": field, "venue": v}


_Y = r"^(\d{4} )?"  # the year Crossref puts in front of IEEE proceedings titles
# Quick-Pick catalog. ISSNs were checked against Crossref /journals and every container_regex against live
# Crossref container titles (tests: _venues_live_check in the scratch suite, see PAPER_RADAR.md v3.3.0).
CATALOG = [
    # --- Solid-state circuits (the default IC preset's venues carry their looked-up IEEE publication_number)
    _c("IEEE Journal of Solid-State Circuits", "JSSC", "sscs", "Solid-State Circuits", ("0018-9200", "1558-173X"),
       ieee_query={"publication_number": "4"}),
    _c("IEEE Solid-State Circuits Letters", "SSC-L", "sscs", "Solid-State Circuits", ("2573-9603",),
       ieee_query={"publication_number": "8011414"}),
    _c("IEEE Transactions on Circuits and Systems I: Regular Papers", "TCAS-I", "sscs", "Circuits & Systems",
       ("1549-8328", "1558-0806"), ieee_query={"publication_number": "8919"}),
    _c("IEEE Transactions on Circuits and Systems II: Express Briefs", "TCAS-II", "sscs", "Circuits & Systems",
       ("1549-7747", "1558-3791"), ieee_query={"publication_number": "8920"}),
    _c("IEEE Journal on Emerging and Selected Topics in Circuits and Systems", "JETCAS", "sscs", "Circuits & Systems",
       ("2156-3357", "2156-3365")),
    _c("IEEE International Solid-State Circuits Conference", "ISSCC", "sscs", "Solid-State Circuits",
       ("0193-6530", "2376-8606"), _Y + r"IEEE International Solid[- ]{1,2}State Circuits Conference",
       ieee_query={"publication_title": "International Solid-State Circuits Conference", "content_type": "Conferences"}),
    _c("IEEE Symposium on VLSI Technology and Circuits", "VLSI", "sscs", "Solid-State Circuits",
       ("2158-9682", "0743-1562", "2158-5601"), r"Symposium on VLSI (Technology and Circuits|Circuits)",
       title_query="Symposium on VLSI Technology and Circuits", prefix=("10.1109", "10.23919"),
       ieee_query={"publication_title": "Symposium on VLSI", "content_type": "Conferences"}),
    _c("IEEE Custom Integrated Circuits Conference", "CICC", "sscs", "Solid-State Circuits", ("0886-5930", "2152-3630"),
       _Y + r"IEEE Custom Integrated Circuits Conference",
       ieee_query={"publication_title": "Custom Integrated Circuits Conference", "content_type": "Conferences"}),
    _c("IEEE European Solid-State Electronics Research Conference (ESSERC/ESSCIRC)", "ESSERC", "sscs",
       "Solid-State Circuits", (), r"European Solid[- ]State (Circuits|Electronics Research) Conference|ESSCIRC|ESSERC",
       title_query="European Solid-State Circuits Conference",
       ieee_query={"publication_title": "European Solid-State", "content_type": "Conferences"}),
    _c("IEEE Asian Solid-State Circuits Conference", "A-SSCC", "sscs", "Solid-State Circuits", (),
       _Y + r"IEEE Asian Solid[- ]{1,2}State Circuits Conference"),
    _c("IEEE International Symposium on Circuits and Systems", "ISCAS", "sscs", "Circuits & Systems",
       ("0271-4302", "2158-1525"), _Y + r"IEEE International Symposium on Circuits and Systems"),
    # --- RF / mmWave
    _c("IEEE Transactions on Microwave Theory and Techniques", "TMTT", "rf", "RF / mmWave", ("0018-9480", "1557-9670")),
    _c("IEEE Microwave and Wireless Technology Letters", "MWTL", "rf", "RF / mmWave", ("2771-957X", "2771-9588")),
    _c("IEEE Radio Frequency Integrated Circuits Symposium", "RFIC", "rf", "RF / mmWave", ("1529-2517", "2375-0995"),
       _Y + r"IEEE Radio Frequency Integrated Circuits Symposium"),
    _c("IEEE/MTT-S International Microwave Symposium", "IMS", "rf", "RF / mmWave", ("0149-645X", "2576-7216"),
       _Y + r"IEEE(/| )MTT-S International Microwave Symposium", title_query="IEEE MTT-S International Microwave Symposium"),
    # --- Power electronics
    _c("IEEE Transactions on Power Electronics", "TPEL", "power", "Power", ("0885-8993", "1941-0107")),
    _c("IEEE Journal of Emerging and Selected Topics in Power Electronics", "JESTPE", "power", "Power",
       ("2168-6777", "2168-6785")),
    _c("IEEE Applied Power Electronics Conference and Exposition", "APEC", "power", "Power", ("1048-2334", "2470-6647"),
       _Y + r"IEEE Applied Power Electronics Conference"),
    # --- Digital / EDA (DAC and ICCAD proceedings are also published by ACM: 10.1145)
    _c("IEEE Transactions on Very Large Scale Integration (VLSI) Systems", "TVLSI", "eda", "Digital / EDA",
       ("1063-8210", "1557-9999")),
    _c("IEEE Transactions on Computer-Aided Design of Integrated Circuits and Systems", "TCAD", "eda", "Digital / EDA",
       ("0278-0070", "1937-4151")),
    _c("ACM/IEEE Design Automation Conference", "DAC", "eda", "Digital / EDA", ("0738-100X",),
       r"^(\d{4} )?(Proceedings of the )?(\d+(st|nd|rd|th) )?(ACM/IEEE |ACM/EDAC/IEEE |IEEE/ACM )?Design Automation Conference",
       prefix=("10.1109", "10.1145"), title_query="ACM/IEEE Design Automation Conference"),
    _c("IEEE/ACM International Conference on Computer-Aided Design", "ICCAD", "eda", "Digital / EDA",
       ("1092-3152", "1558-2434"),
       r"IEEE/ACM International Conference on Computer[- ]Aided Design", prefix=("10.1109", "10.1145"),
       title_query="International Conference on Computer-Aided Design"),
    # --- Devices / sensors / MEMS
    _c("IEEE Electron Device Letters", "EDL", "dev", "Devices / Sensors", ("0741-3106", "1558-0563")),
    _c("IEEE Transactions on Electron Devices", "TED", "dev", "Devices / Sensors", ("0018-9383", "1557-9646")),
    _c("IEEE International Electron Devices Meeting", "IEDM", "dev", "Devices / Sensors", ("0163-1918", "2156-017X"),
       _Y + r"(IEEE )?International Electron Devices Meeting"),
    _c("IEEE Sensors Journal", "JSEN", "dev", "Devices / Sensors", ("1530-437X", "1558-1748")),
    _c("IEEE Sensors Letters", "SensLett", "dev", "Devices / Sensors", ("2475-1472",),
       ieee_query={"publication_number": "7782634"}),
    _c("IEEE SENSORS Conference", "SENSORS", "dev", "Devices / Sensors", ("1930-0395", "2168-9229"),
       _Y + r"IEEE SENSORS$", title_query="IEEE SENSORS"),
    _c("Journal of Microelectromechanical Systems", "JMEMS", "dev", "Devices / Sensors", ("1057-7157", "1941-0158")),
    # --- Photonics, bio, instrumentation, beyond IEEE
    _c("Journal of Lightwave Technology", "JLT", "other", "Photonics", ("0733-8724", "1558-2213")),
    _c("IEEE Transactions on Biomedical Circuits and Systems", "TBioCAS", "other", "Biomedical",
       ("1932-4545", "1940-9990"), ieee_query={"publication_number": "4156126"}),
    _c("IEEE Transactions on Biomedical Engineering", "TBME", "other", "Biomedical", ("0018-9294", "1558-2531")),
    _c("IEEE Transactions on Instrumentation and Measurement", "TIM", "other", "Instrumentation",
       ("0018-9456", "1557-9662")),
    _c("Nature Electronics", "Nat Electron", "other", "Beyond IEEE", ("2520-1131",), ieee=False),
]
GROUPS = [("sscs", "🔬 Solid-State Circuits & Systems"), ("rf", "📡 RF / mmWave"), ("power", "⚡ Power Electronics"),
          ("eda", "🧮 Digital / EDA"), ("dev", "🧫 Devices, Sensors & MEMS"), ("other", "🌐 Photonics, Bio & Beyond IEEE")]


def catalog():
    """For STARTUP.html: the catalog entries (group, field, venue) and the group headings."""
    return {"groups": [{"key": k, "label": lbl} for k, lbl in GROUPS], "venues": json.loads(json.dumps(CATALOG))}


def catalog_venue(short):
    for c in CATALOG:
        if c["venue"]["short"].lower() == str(short or "").lower():
            return json.loads(json.dumps(c["venue"]))
    raise ValueError(f"{short!r} is not in the venue catalog")


# ---------- the regex guard ----------

def _nested_repeat(p):
    """True for a repeated group that itself contains a repetition, e.g. (a+)+, (\\w*x)*, ((ab)+c){2,}."""
    stack, i, n = [False], 0, len(p)

    def quant(j):  # the length of a repeating quantifier (*, +, {m,} or {m,n} with n > 1) at j, else 0
        if j < n and p[j] in "*+":
            return 1
        m = re.match(r"\{(\d*)(,(\d*))?\}", p[j:])
        if m and (m.group(2) and (not m.group(3) or int(m.group(3)) > 1) or (not m.group(2) and int(m.group(1) or 0) > 1)):
            return len(m.group(0))
        return 0

    while i < n:
        c = p[i]
        if c == "\\":
            i += 2
            q = quant(i)
            if q:
                stack[-1] = True
                i += q
            continue
        if c == "[":
            j = i + 1
            if j < n and p[j] == "^":
                j += 1
            if j < n and p[j] == "]":
                j += 1
            while j < n and p[j] != "]":
                j += 2 if p[j] == "\\" else 1
            i = j + 1
            q = quant(i)
            if q:
                stack[-1] = True
                i += q
            continue
        if c == "(":
            stack.append(False)
            i += 1
            if i < n and p[i] == "?":
                i += 1
            continue
        if c == ")":
            inner = stack.pop() if len(stack) > 1 else False
            i += 1
            q = quant(i)
            if q and inner:
                return True
            if q or inner:
                stack[-1] = True
            i += q
            continue
        q = quant(i)
        if q:
            stack[-1] = True
            i += q
            continue
        i += 1
    return False


def _words(p):
    """The literal words (3+ letters) a pattern names, without escapes like \\d \\b and character classes."""
    t = re.sub(r"\[(\\.|[^\]\\])*\]", " ", p)
    t = re.sub(r"\(\?[A-Za-z]+\)|\(\?[:=!]|\(\?<[=!]|\(\?P<\w+>", " ", t)
    t = re.sub(r"\\[A-Za-z]", " ", t)
    return re.findall(r"[A-Za-z]{3,}", t)


def check_regex(p, what="container_regex"):
    """The pattern, stripped, or ValueError with a plain-language reason."""
    if not isinstance(p, str) or not p.strip():
        raise ValueError(f"{what}: empty: a conference needs a pattern for its proceedings' container title")
    p = p.strip()
    if len(p) > MAX_RX or "\n" in p or "\r" in p:
        raise ValueError(f"{what}: one line of at most {MAX_RX} characters")
    try:
        rx = re.compile(p, re.I)
    except re.error as e:
        raise ValueError(f"{what}: not a valid regular expression ({e})")
    if re.search(r"\\[1-9]|\(\?P=", p):
        raise ValueError(f"{what}: backreferences (\\1, (?P=name)) are not allowed")
    if _nested_repeat(p):
        raise ValueError(f"{what}: a repeated group that repeats inside, like (a+)+ or (\\w*)*, can hang the radar "
                         "(catastrophic backtracking): remove the inner or the outer repetition")
    if rx.search(""):
        raise ValueError(f"{what}: it matches an empty title, so every paper in Crossref would match")
    for s in SENTINELS:
        if rx.search(s):
            raise ValueError(f"{what}: too broad: it also matches the unrelated title “{s}”. Name the venue more "
                             "specifically, e.g. its full title")
    if not [w for w in _words(p) if w.lower() not in GENERIC]:
        raise ValueError(f"{what}: too generic: name at least one distinctive word of the venue's title "
                         "(e.g. Solid-State, Microwave, Design Automation), not only IEEE / Conference / a year")
    return p


def core_title(name):
    """'2024 IEEE/MTT-S International Microwave Symposium (IMS)' → 'International Microwave Symposium'."""
    t = re.sub(r"\s*\([^)]*\)\s*$", "", str(name or "").strip())
    t = re.sub(r"^(\d{4}\s+)?(Proceedings of the\s+)?(\d+(st|nd|rd|th)\s+)?", "", t, flags=re.I)
    t = re.sub(r"^((IEEE|ACM|EDAC|MTT-S|IEEE-MTT-S)(/|\s+)?)+", "", t)
    return t.strip() or str(name or "").strip()


def suggest_regex(name):
    """A container_regex for a conference name: optional year / 'Proceedings of the' / ordinal / IEEE-or-ACM
    prefix, then the core title with '-' or ' ' between hyphenated words. Anchored at the start, so a sister
    event whose title only contains it (ASP-DAC for DAC, Midwest … for ISCAS) does not match."""
    core = core_title(name)
    toks = []
    for tok in core.split():
        toks.append("[- ]{1,2}".join(re.escape(x) for x in tok.split("-")))
    body = " ".join(toks)
    return (r"^(\d{4} )?(Proceedings of the )?(\d+(st|nd|rd|th) )?(\S*(IEEE|ACM)\S* )?([A-Z]{2,5}-S )?" + body)[:MAX_RX]


# ---------- one venue entry, the whole list ----------

def issn_ok(s):
    s = s.replace("-", "").upper()
    if not re.match(r"^\d{7}[\dX]$", s):
        return False
    tot = sum(int(d) * w for d, w in zip(s[:7], range(8, 1, -1)))
    chk = (11 - tot % 11) % 11
    return s[7] == ("X" if chk == 10 else str(chk))


def norm_issn(s):
    s = str(s or "").strip().upper().replace("-", "")
    return f"{s[:4]}-{s[4:]}" if len(s) == 8 else s


def _line(v, what, maxlen):
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > maxlen or "\n" in v or "\r" in v:
        raise ValueError(f"{what}: one line of 1–{maxlen} characters")
    return v.strip()


def check_venue(v, legacy=False):
    """One venue entry, normalised (key order kept), or ValueError. legacy: the entry is exactly as it already is
    in config.json, so keys this version does not know are kept (all known keys are still checked)."""
    if not isinstance(v, dict):
        raise ValueError("venues: every entry must be an object")
    short = _line(v.get("short"), "venue short code", 20)
    if not SHORT_RX.match(short):
        raise ValueError(f"venue {short!r}: the short code uses letters, digits, space . & + / _ - (≤ 20)")
    what = f"venue {short}"
    bad = [k for k in v if k not in KNOWN_KEYS and not str(k).startswith("_")]
    if bad and not legacy:
        raise ValueError(f"{what}: unknown key(s) {', '.join(map(str, bad[:4]))}")
    out = {}
    for k, x in v.items():
        if k == "short":
            out[k] = short
        elif k == "name":
            out[k] = _line(x, f"{what}: name", 200)
        elif k == "type":
            if x not in ("journal", "conference"):
                raise ValueError(f"{what}: type is journal or conference")
            out[k] = x
        elif k == "issn":
            if not isinstance(x, list) or len(x) > 4:
                raise ValueError(f"{what}: issn is a list of at most 4 ISSNs")
            got = []
            for s in x:
                if not isinstance(s, str) or not ISSN_RX.match(s.strip()) or not issn_ok(s.strip()):
                    raise ValueError(f"{what}: {str(s)[:20]!r} is not a valid ISSN (1234-567X, check digit)")
                if norm_issn(s) not in got:
                    got.append(norm_issn(s))
            out[k] = got
        elif k == "publisher":
            out[k] = _line(x, f"{what}: publisher", 120)
        elif k == "source":
            if x not in SOURCES:
                raise ValueError(f"{what}: source is one of {', '.join(SOURCES)}")
            out[k] = x
        elif k == "query":
            if not isinstance(x, str) or len(x) > 500 or "\n" in x:
                raise ValueError(f"{what}: query is one line of at most 500 characters")
            out[k] = x
        elif k in ("max_results", "max_pages"):
            if isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 1000:
                raise ValueError(f"{what}: {k} is a whole number from 1 to 1000")
            out[k] = x
        elif k == "categories":
            if not isinstance(x, list) or len(x) > 30 or not all(isinstance(c, str) and len(c) <= 60 for c in x):
                raise ValueError(f"{what}: categories is a list of short texts")
            out[k] = x
        elif k == "ieee_query":
            if not isinstance(x, dict) or not x or not set(x) <= set(IEEE_KEYS) \
                    or not all(isinstance(y, str) and y.strip() and len(y) <= 120 and "\n" not in y for y in x.values()):
                raise ValueError(f"{what}: ieee_query takes {', '.join(IEEE_KEYS)} (short texts)")
            if "publication_number" in x and not x["publication_number"].isdigit():
                raise ValueError(f"{what}: ieee_query.publication_number is a number")
            out[k] = x
        elif k == "title_query":
            out[k] = _line(x, f"{what}: title_query", 200)
        elif k == "container_regex":
            out[k] = check_regex(x, f"{what}: container_regex")
        elif k == "doi_prefix":
            if not isinstance(x, list) or not 1 <= len(x) <= 3 or not all(isinstance(p, str) and PREFIX_RX.match(p) for p in x):
                raise ValueError(f"{what}: doi_prefix is a list of 1–3 DOI prefixes like 10.1109")
            out[k] = x
        else:
            out[k] = x
    for k in ("name", "type"):
        if k not in out:
            raise ValueError(f"{what}: {k} is missing")
    out.setdefault("issn", [])
    if not out.get("source"):
        if out["type"] == "journal" and not out["issn"]:
            raise ValueError(f"{what}: a journal needs its ISSN (Crossref is asked by ISSN)")
        if out["type"] == "conference" and not out.get("container_regex"):
            raise ValueError(f"{what}: a conference needs a container_regex (🪄 Suggest writes one from its name)")
    return out


def check_venues(lst, current=None):
    """The whole list (STARTUP.html saves it whole), normalised, or ValueError listing every problem.
    current: config.json's list now; an entry identical to one of them keeps keys this version doesn't know."""
    if not isinstance(lst, list) or not 1 <= len(lst) <= MAX_VENUES:
        raise ValueError(f"venues: a list of 1–{MAX_VENUES} venues (remove one before adding more)")
    old = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in current or [] if isinstance(x, dict)}
    out, errs, seen = [], [], set()
    for v in lst:
        try:
            x = check_venue(v, legacy=json.dumps(v, sort_keys=True, ensure_ascii=False) in old)
        except ValueError as e:
            errs.append(str(e))
            continue
        if x["short"].lower() in seen:
            errs.append(f"venues: the short code {x['short']!r} is used twice")
        seen.add(x["short"].lower())
        out.append(x)
    if errs:
        raise ValueError("; ".join(errs))
    return out


def check_flagships(lst, venues):
    shorts = {str(v.get("short")): True for v in venues or [] if isinstance(v, dict)}
    if not isinstance(lst, list) or len(lst) > MAX_VENUES or not all(isinstance(x, str) for x in lst):
        raise ValueError("catch_up.flagship_venues: a list of venue short codes")
    out = []
    for x in lst:
        if x not in shorts:
            raise ValueError(f"catch_up.flagship_venues: {x!r} is not a watched venue")
        if x not in out:
            out.append(x)
    return out


# ---------- what fetcher.py asks the IEEE API ----------

def is_ieee(v):
    pub = str(v.get("publisher") or "")
    return "IEEE" in pub or "Institute of Electrical and Electronics Engineers" in pub \
        or bool(re.search(r"\bIEEE\b", str(v.get("name") or "")))


def ieee_params(v):
    """The IEEE Xplore query of a venue: its ieee_query; else, for an IEEE venue, the journal's print ISSN or the
    conference's title (with content_type Conferences; container_regex then filters the hits); else None."""
    q = v.get("ieee_query")
    if isinstance(q, dict) and q:
        return dict(q)
    if v.get("source") or not is_ieee(v):
        return None
    if v.get("type") == "journal" and v.get("issn"):
        return {"issn": v["issn"][0]}
    if v.get("type") == "conference" and v.get("container_regex"):
        return {"publication_title": v.get("title_query") or core_title(v.get("name")), "content_type": "Conferences"}
    return None


def crossref_prefixes(v):
    """DOI prefixes for the Crossref proceedings query of a conference (10.1109 unless the venue says otherwise)."""
    return list(v.get("doi_prefix") or ["10.1109"])


# ---------- the live check (Crossref + OpenAlex, no keys) ----------

def _http(url, cfg):
    mail = (cfg or {}).get("contact_email") or ""
    ua = "Paper-Radar-VenueCheck/1.0" + (f" (mailto:{mail})" if mail else "")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt or e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt:
                raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc} did not answer ({e})")
        time.sleep(2)
    return None


def _par(*calls):
    """Run the (fn, args…) calls at once (Crossref answers in 5–40 s): their results in order."""
    with ThreadPoolExecutor(max_workers=len(calls)) as ex:
        futs = [ex.submit(c[0], *c[1:]) for c in calls]
        return [f.result() for f in futs]


def _q(params, cfg):
    mail = (cfg or {}).get("contact_email")
    return urllib.parse.urlencode({**params, **({"mailto": mail} if mail else {})})


def _date(it):
    for k in ("published-print", "published-online", "issued", "published", "created"):
        parts = ((it.get(k) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
    return ""


def _paper(it):
    return {"title": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(it.get("title") or []))).strip()[:300],
            "date": _date(it), "doi": (it.get("DOI") or "").lower(),
            "container": " ".join(it.get("container-title") or [])[:200]}


def _issn_types(msg):
    p = [x["value"] for x in msg.get("issn-type") or [] if x.get("type") == "print"]
    e = [x["value"] for x in msg.get("issn-type") or [] if x.get("type") == "electronic"]
    return (p[0] if p else ""), (e[0] if e else "")


def _short_of(name):
    """An editable default short code: (ACRONYM) in the name, else the initials of its distinctive words."""
    m = re.search(r"\(([A-Z][A-Za-z0-9/-]{1,15})\)\s*$", name or "")
    if m:
        return m.group(1)[:20]
    ws = [w for w in re.findall(r"[A-Za-z][A-Za-z-]*", core_title(name)) if w.lower() not in GENERIC | {"of", "on", "in"}]
    kind = re.search(r"\b(Transactions|Journal|Letters)\b", name or "")
    core, k = "".join(w[0].upper() for w in ws), kind.group(1) if kind else ""
    return ((core + "L") if k == "Letters" else (k[:1] + core))[:8] or "VENUE"  # TMTT, JSC, EDL


def _sources(q, cfg, n=8):
    r = _http("https://api.openalex.org/sources?" + _q({"search": q, "per-page": n, "select":
              "id,display_name,issn_l,issn,type,host_organization_name,works_count"}, cfg), cfg) or {}
    return [{"name": s.get("display_name") or "", "issn": s.get("issn") or [], "issn_l": s.get("issn_l") or "",
             "type": s.get("type") or "", "publisher": s.get("host_organization_name") or "",
             "works_count": s.get("works_count") or 0} for s in r.get("results") or []]


def _journal(issn, cfg, short="", name=""):
    issn = norm_issn(issn)
    if not issn_ok(issn):
        raise ValueError(f"{issn!r} is not a valid ISSN (check digit)")
    cr, oa, w = _par((_http, f"https://api.crossref.org/journals/{issn}?" + _q({}, cfg), cfg),
                     (_http, f"https://api.openalex.org/sources/issn:{issn}?" + _q({}, cfg), cfg),
                     (_http, f"https://api.crossref.org/journals/{issn}/works?" + _q({
                         "sort": "published", "order": "desc", "rows": 12, "filter": "type:journal-article",
                         "select": "DOI,title,container-title,published,published-print,published-online,issued"}, cfg), cfg))
    cr, oa, w = (cr or {}).get("message"), oa or {}, w or {}
    if not cr and not oa:
        raise ValueError(f"ISSN {issn} is not known to Crossref or OpenAlex: check the number")
    p_issn, e_issn = _issn_types(cr or {})
    issns = [x for x in (p_issn, e_issn) if x] or [norm_issn(x) for x in (oa.get("issn") or [issn])][:2]
    if issn not in issns:
        issns = [issn] + issns
    recent = []
    if cr:
        recent = [_paper(it) for it in (w.get("message") or {}).get("items") or [] if it.get("title")][:3]
    title = oa.get("display_name") or (cr or {}).get("title") or name or issn
    publisher = oa.get("host_organization_name") or (cr or {}).get("publisher") or ""
    v = {"short": short or _short_of(title), "name": name or title, "issn": issns[:4], "type": "journal"}
    if publisher:
        v["publisher"] = ("IEEE" if "Electrical and Electronics Engineers" in publisher or re.search(r"\bIEEE\b", publisher)
                          else publisher[:120])
    warn = []
    if oa.get("type") and oa.get("type") != "journal":
        warn.append(f"OpenAlex lists this ISSN as a {oa['type']}, not a journal")
    if not recent:
        warn.append("Crossref returned no recent articles for this ISSN: new papers may not be found")
    return {"kind": "journal", "title": title, "publisher": publisher, "issn_print": p_issn, "issn_online": e_issn,
            "works_count": oa.get("works_count"), "crossref_dois": ((cr or {}).get("counts") or {}).get("total-dois"),
            "recent": recent, "venue": v, "warnings": warn}


def _norm_ct(t):
    t = re.sub(r"^\d{4}\s+", "", t.strip())
    t = re.sub(r"\b\d+(st|nd|rd|th)\b\s*", "", t)
    return re.sub(r"\s*(-\s*)?\b(IMS\s*)?(19|20)\d{2}\b", "", t).strip(" -")


def _conference(name, rx_text, cfg, short="", issn=(), prefix=None, title_query=None, base=None):
    try:
        rx_text = check_regex(rx_text)
    except ValueError as e:  # shown in the modal, the pattern still editable there (no network call)
        v = dict(base) if base else {"short": short or _short_of(name), "name": name,
                                     "issn": [norm_issn(i) for i in issn or []], "type": "conference"}
        v["container_regex"] = str(rx_text or "")
        return {"kind": "conference", "title": name, "regex": str(rx_text or ""), "matched": [], "excluded": [],
                "samples": [], "checked": 0, "prefixes": {}, "venue": v, "warnings": [], "errors": [str(e)]}
    rx = re.compile(rx_text, re.I)
    since = f"{date.today().year - 3}-01-01"
    q = title_query or core_title(name)
    sel = "DOI,title,container-title,published,published-print,published-online,issued"
    pre_f = ",".join(f"prefix:{p}" for p in (prefix or ["10.1109"]))
    calls = [  # what fetcher.py asks (its DOI prefixes, relevance order), the same without prefix (lookalikes)
        (_http, "https://api.crossref.org/works?" + _q({"query.container-title": q, "rows": 200, "select": sel,
         "filter": f"{pre_f},type:proceedings-article,from-pub-date:{since}"}, cfg), cfg),
        (_http, "https://api.crossref.org/works?" + _q({"query.container-title": core_title(name), "rows": 100,
         "select": sel, "filter": f"type:proceedings-article,from-pub-date:{since}"}, cfg), cfg),
        (_sources, core_title(name), cfg, 10)]
    calls += [(_http, f"https://api.crossref.org/journals/{norm_issn(i)}/works?" + _q({
        "rows": 40, "sort": "published", "order": "desc", "select": sel}, cfg), cfg) for i in (issn or [])[:2]]
    res = _par(*calls)
    srcs = res.pop(2)
    items, dois = [], set()
    for r in res:
        for it in ((r or {}).get("message") or {}).get("items") or []:
            d = (it.get("DOI") or "").lower()
            if d and d not in dois:
                dois.add(d)
                items.append(it)
    groups = {}  # (normalised title, matched) → {"title", "n", "years"}
    for it in items:
        ct = " ".join(it.get("container-title") or []).strip()
        if not ct:
            continue
        ok = bool(rx.search(ct))
        g = groups.setdefault((_norm_ct(ct), ok), {"title": ct, "n": 0, "years": set(), "source": "Crossref"})
        g["n"] += 1
        y = _date(it)[:4]
        if y:
            g["years"].add(y)
    for s in srcs:
        if s["name"] and not any(k[0].lower() == _norm_ct(s["name"]).lower() for k in groups):
            ok = bool(rx.search(s["name"]))
            groups[(_norm_ct(s["name"]), ok)] = {"title": s["name"], "n": s["works_count"], "years": set(),
                                                 "source": f"OpenAlex {s['type'] or 'source'}"}
    rows = lambda ok: sorted(({"title": g["title"], "n": g["n"], "years": sorted(g["years"])[-3:], "source": g["source"]}
                              for (t, m), g in groups.items() if m == ok), key=lambda x: -x["n"])
    matched, excluded = rows(True), rows(False)
    hits = sorted((_paper(it) for it in items if it.get("title") and rx.search(" ".join(it.get("container-title") or []))),
                  key=lambda p: p["date"], reverse=True)
    seen, samples = set(), []
    for p in hits:
        if p["doi"] not in seen:
            seen.add(p["doi"])
            samples.append(p)
        if len(samples) == 3:
            break
    pre = Counter(p["doi"].split("/")[0] for p in hits if p["doi"].startswith("10."))
    if base:
        v = dict(base)
    else:
        acr = Counter(m for p in hits for m in re.findall(r"\(([A-Z][A-Za-z0-9-]{1,15})\)", p["container"]))
        v = {"short": short or (acr.most_common(1)[0][0] if acr else _short_of(name)), "name": name,
             "issn": [norm_issn(i) for i in issn or []], "type": "conference"}
        if re.search(r"\bIEEE\b", name) or any("IEEE" in p["container"] for p in hits[:20]):
            v["publisher"] = "IEEE"
        if q != core_title(name) or title_query:
            v["title_query"] = q
        v["container_regex"] = rx_text
        pf = [p for p, _ in pre.most_common(3)]
        if prefix:
            v["doi_prefix"] = list(prefix)
        elif pf and pf != ["10.1109"]:
            v["doi_prefix"] = pf
    v["container_regex"] = rx_text
    warn = []
    if not matched:
        warn.append("The regex matched none of the container titles found: it is too strict, or the name is off")
    elif len(matched) > 6:
        warn.append(f"The regex matches {len(matched)} different series: check the ✅ list for strangers")
    if not samples:
        warn.append("No recent proceedings paper matched (Crossref, last 3 years)")
    return {"kind": "conference", "title": name, "regex": rx_text, "matched": matched[:15], "excluded": excluded[:15],
            "samples": samples, "checked": len(items), "prefixes": dict(pre.most_common(4)), "venue": v, "warnings": warn}


def verify(req, cfg, current=()):
    """POST /api/verify_venue. req: {"suggest": name} → a regex; {"catalog": short}; or {"query": ISSN / title /
    conference name, "type": auto|journal|conference, "short", "regex", "issn", "name"}. Returns a dict for the
    confirmation modal; ValueError for bad input, RuntimeError when Crossref / OpenAlex don't answer."""
    if not isinstance(req, dict):
        raise ValueError("expected a JSON object")
    if "suggest" in req:
        name = _line(req.get("suggest"), "name", 200)
        return {"ok": True, "regex": suggest_regex(name), "core": core_title(name)}
    taken = {str(v.get("short") or "").lower() for v in current or [] if isinstance(v, dict)}
    taken_issn = {i for v in current or [] if isinstance(v, dict) for i in v.get("issn") or []}
    if req.get("catalog"):
        v = catalog_venue(req["catalog"])
        if v["type"] == "journal":
            out = _journal(v["issn"][0], cfg, v["short"], v["name"])
            out["venue"] = v
        else:
            out = _conference(v["name"], req.get("regex") or v["container_regex"], cfg, v["short"], v["issn"],
                              v.get("doi_prefix"), v.get("title_query"), base=v)
        out["catalog"] = True
    else:
        qtext = _line(req.get("query") or req.get("name"), "venue", 200)
        kind = req.get("type") or "auto"
        if kind not in ("auto", "journal", "conference"):
            raise ValueError("type: auto, journal or conference")
        short = str(req.get("short") or "").strip()
        if short and not SHORT_RX.match(short):
            raise ValueError("short code: letters, digits, space . & + / _ - (≤ 20)")
        issn = [norm_issn(x) for x in (req.get("issn") or []) if str(x).strip()]
        is_issn = bool(ISSN_RX.match(qtext.replace(" ", "")))
        if is_issn:
            issn = [norm_issn(qtext.replace(" ", ""))] + [i for i in issn if i != norm_issn(qtext.replace(" ", ""))]
        alternates = []
        if kind == "auto":
            kind = "journal" if is_issn else "conference" if (req.get("regex") or CONF_WORDS.search(qtext)) else "journal"
        if kind == "journal":
            if not issn:
                alternates = [s for s in _sources(qtext, cfg) if s["issn_l"] and s["type"] in ("journal", "")][:5]
                if not alternates:
                    raise ValueError(f"OpenAlex knows no journal called {qtext!r}: try its ISSN")
                issn = [alternates[0]["issn_l"]]
            out = _journal(issn[0], cfg, short, req.get("name") or "")
            out["alternates"] = alternates[1:]
        else:
            name = str(req.get("name") or qtext).strip()
            rx = req.get("regex") or suggest_regex(name)
            out = _conference(name, rx, cfg, short, issn)
            out["suggested_regex"] = not req.get("regex")
    v = out["venue"]
    if v["short"].lower() in taken:
        out["warnings"].insert(0, f"The short code {v['short']} is already watched: pick another code below")
        out["duplicate"] = True
    if v["type"] == "journal" and taken_issn & set(v.get("issn") or []):
        out["warnings"].insert(0, "A watched venue already has this ISSN")
        out["duplicate"] = True
    if not out.get("errors"):
        try:
            check_venue(v)
            out["errors"] = []
        except ValueError as e:
            out["errors"] = [str(e)]
    out["ok"] = True
    return out
