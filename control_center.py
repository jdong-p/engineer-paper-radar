"""Setup & Radar Control Center (v2.21): STARTUP.html next to DASHBOARD.html, and the small local API behind it.

  python briefing.py --gui [PORT]   local server on http://127.0.0.1:18766 (another free port if that one is taken);
                                    opens STARTUP.html in the default browser. Ctrl+C stops it.
  Start_Radar_GUI.cmd / .command    the same by double-click (Starter Edition)
  radar_sync_server.py              serves the same /api/* on the Minisforum radar (X-Radar-Token, like /mark)

STARTUP.html is rewritten together with DASHBOARD.html (html_renderer.write_all). It shows a setup checklist (paper
store, pillars, IEEE key / claude CLI / contact e-mail as yes-no flags only), edits the research settings of
config.json (pillars, keywords, negative keywords, VIP authors, anchors, sharing, arXiv), shows the shared corpus
repo and its peers, and runs four fixed actions. Opened as a plain file it still shows all of that, saves the edited
config.json as a download and lists the commands to run; served by --gui or the sync server it runs them itself.

API (JSON):
  GET  /api/config        → {"ok", "editable", "status", "job"}: the editable settings and the read-only status
  POST /api/config        ← {"config": {...}}: validated, then only the changed keys are written into config.json
                            (unknown keys and the file's own layout are kept; the previous file goes to
                            _system/config.backup.json)
  POST /api/run           ← {"action": "rescore_and_seed" | "pull_data_repo" | "sync_data_repo" | "daily",
                             "config"?: {...}, "clone_url"?, "clone_dir"?}: saves the config, then runs briefing.py
                            in a background worker (one job at a time)
  GET  /api/check_updates[?force=1] → {"code": {...}, "repos": [...]}: is the code repo behind its GitHub main, and
                            do the primary data repo / read_repos have commits this clone lacks (v3.2.0; git ls-remote
                            with a 4 s timeout, cached 15 min in memory and in the store's meta table)
  POST /api/plan_keywords ← {"pillars": [{"name", "seeds"}], "anchors": [...]} → the 0-token keyword & weight plan
                            (keyword_planner.py, read only; STARTUP.html 🪄 Auto-Plan shows it in a preview)
  GET  /api/job?since=N   → {"id", "running", "action", "rc", "lines": [...], "next"}: the job's output from line N
  GET  /api/active?since=B → {"active", "id", "what", "lines", "next"}: a run this server didn't start (07:00 task,
                            Friday sync, a terminal) from .run.log byte B (run_lock.py)
POST /api/run never overlaps a run: while one holds .run.lock (run_lock.py) it answers 409 {"error": "already_running",
"already_running": true, "job" | "active": …}, and the page streams that run instead.

Secrets never leave the machine: the status holds only booleans (has_ieee_key, has_claude), never a key's value.
The --gui server listens on 127.0.0.1 only and checks the Host header (no DNS rebinding). It needs a key made at
each launch: the launch link (/?k=<key>) stores it in an HttpOnly, SameSite=Strict cookie, and every /api call
must also carry the header X-Radar-GUI. It sends no CORS headers (except on /ping, which returns no data), so
another website can neither read its pages nor send it a request with that header.
"""

from __future__ import annotations

import http.cookies
import json
import shlex
import time
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STARTER = (HERE / "config_file.py").exists()  # Starter Edition (public repo); else the Minisforum radar
# the public repo was renamed jdong-p/ic-paper-radar → jdong-p/engineer-paper-radar (2026-11-14); the
# Starter Edition carries the new name, the Minisforum radar stays "IC Paper Radar"
NAME = "Engineer Paper Radar" if STARTER else "IC Paper Radar"
REPO_URL = "https://github.com/jdong-p/engineer-paper-radar"
PAGE = "STARTUP.html"
GUI_PORT = 18766
DATA_URL = "https://github.com/jdong-p/ic-paper-radar-data.git"
MAX_PILLARS = 9

# editable field → its path in config.json ("data_repo" and "arxiv" are objects of such fields)
PATHS = {"pillars": ["pillars"], "pillar_short": ["pillar_short"], "positive_keywords": ["positive_keywords"],
         "negative_keywords": ["negative_keywords"], "negative_override": ["negative_override"],
         "key1_themes": ["beyond_ieee", "key1_themes"], "min_score": ["min_score"], "vip_authors": ["vip_authors"],
         "vip_boost": ["vip_boost"], "anchors": ["anchors"], "contact_email": ["contact_email"],
         "data_repo_dir": ["paths", "data_repo_dir"], "domain_preset": ["domain_preset"], "deployment": ["deployment"],
         "sync_hosts": ["sync_server", "hosts"], "schedule": ["schedule"]}
DATA_REPO_KEYS = ("peer_id", "share_curation", "share_tags", "share_notes", "adopt_external", "remote_url", "read_repos")
# v3.2.0: the two Friday slots (config.json "schedule"); the day is locked to Friday for now
SCHEDULE_DEFAULTS = {"auto_update_local": True, "update_time": "07:00", "auto_upload_repo": True, "upload_time": "12:00"}
TIME_RX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
UPDATE_TTL = 15 * 60   # s: GET /api/check_updates answers from its cache for this long
GIT_TIMEOUT = 4        # s: one git ls-remote; a slow or offline network never holds the page
DEPLOYMENTS = ("local", "mesh_lan")  # 🏢 enterprise is only an easter egg in STARTUP.html
HOST_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$")
ARXIV_KEYS = ("enabled", "share", "queries", "categories", "must_all", "exclude_any")
STARTER_ONLY = {"arxiv"}  # v3.0.7: the Minisforum radar edits pillars too (P1–P4 fixed, P5+ added after them)


def fixed_pillars():
    """How many leading pillars can't be renamed / moved / removed: 0 in the Starter, catalog.FIXED_PILLARS (4) on
    the Minisforum radar, whose en-v3 digests and prompts name them."""
    import catalog
    return 0 if STARTER else getattr(catalog, "FIXED_PILLARS", 0)

DOI_RX = re.compile(r"^10\.\d{4,9}/[^\s\"<>]+$")
EMAIL_RX = re.compile(r"^[^@\s\"<>]+@[^@\s\"<>]+\.[A-Za-z]{2,}$")
ARXIV_CAT_RX = re.compile(r"^[a-z-]+(\.[A-Za-z-]+)?$")
MARKER_RX = re.compile(r"^_pillar(\d+)")
# a clone URL without user info (no token can end up in .git/config); the repo name must be the data repo's
CLONE_RX = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?/|git@[A-Za-z0-9.-]+:)[\w.-]+/[\w.-]+?(\.git)?/?$")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flashes under pythonw


# ---------- config.json: read, validate, write only what changed ----------

def raw_config():
    """config.json exactly as stored (Starter Edition: created from config.example.json first if missing)."""
    if STARTER:
        import config_file
        config_file.ensure()
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_cfg():
    import fetcher
    return fetcher.load_config()


def deployment(cfg):
    """local | mesh_lan: config.json "deployment", else mesh_lan when sync_server.hosts has a non-loopback host."""
    d = cfg.get("deployment")
    if d in DEPLOYMENTS:
        return d
    hosts = (cfg.get("sync_server") or {}).get("hosts") or []
    return "mesh_lan" if any(not str(h).startswith("127.") and h != "localhost" for h in hosts) else "local"


def _lan_ip(h):
    """A private-LAN or Tailscale IP address (the shared-address range of RFC 6598, where Tailscale puts its
    nodes): the only extra addresses --gui may bind."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(str(h))
    except ValueError:
        return False
    cgnat = ipaddress.IPv4Network((0x64400000, 10))  # RFC 6598 shared address space (Tailscale)
    return not ip.is_loopback and (ip.is_private or (ip.version == 4 and ip in cgnat))


def _read_entries(cfg):
    import data_repo
    return data_repo.read_repo_entries(cfg)


def share_tags_on(cfg):
    """v3.2.0 data_repo.share_tags (older name: share_curation; default true): publish peers/<peer_id>.json."""
    dr = cfg.get("data_repo") or {}
    return dr.get("share_tags", dr.get("share_curation", True)) is not False


def schedule_cfg(cfg):
    """config.json "schedule" over SCHEDULE_DEFAULTS (bad times fall back to the default)."""
    s = cfg.get("schedule") or {}
    out = dict(SCHEDULE_DEFAULTS)
    for k in ("auto_update_local", "auto_upload_repo"):
        out[k] = s.get(k, True) is not False
    for k in ("update_time", "upload_time"):
        if isinstance(s.get(k), str) and TIME_RX.match(s[k].strip()):
            out[k] = s[k].strip()
    return out


def editable(cfg):
    """The fields STARTUP.html edits, from the effective config (Starter: config.json over the template)."""
    import catalog
    import presets
    dr, ax = cfg.get("data_repo") or {}, cfg.get("arxiv") or {}
    ks = sorted(k for k in catalog.PILLARS if k)
    out = {"pillars": list(cfg.get("pillars") or []) if STARTER else catalog.pillar_names(),
           "pillar_short": (list(cfg.get("pillar_short") or []) if STARTER
                            else [getattr(catalog, "PILLAR_SHORT", {}).get(k, "") for k in ks]),
           "positive_keywords": [[k, v] for k, v in (cfg.get("positive_keywords") or {}).items()],
           "negative_keywords": list(cfg.get("negative_keywords") or []),
           "negative_override": cfg.get("negative_override") or "",
           "key1_themes": list((cfg.get("beyond_ieee") or {}).get("key1_themes") or []),
           "min_score": cfg.get("min_score", 5), "vip_authors": list(cfg.get("vip_authors") or []),
           "vip_boost": cfg.get("vip_boost", 3), "anchors": list(cfg.get("anchors") or []),
           "contact_email": cfg.get("contact_email") or "",
           "data_repo": {"peer_id": dr.get("peer_id") or "", "share_tags": share_tags_on(cfg),
                         "share_notes": dr.get("share_notes", True) is not False,
                         "adopt_external": dr.get("adopt_external", True) is not False,
                         "remote_url": dr.get("remote_url") or "",
                         # v3.0.4: the repos as chips, and per repo its merge options (folded back by collect())
                         "read_repos": [e["spec"] for e in _read_entries(cfg)],
                         "read_opts": {e["spec"]: {"merge_corpus": e["merge"], "force_merge": e["force"]}
                                       for e in _read_entries(cfg) if e["merge"] or e["force"]}},
           "data_repo_dir": (cfg.get("paths") or {}).get("data_repo_dir") or "",
           "domain_preset": presets.key(cfg), "deployment": deployment(cfg),
           "sync_hosts": list((cfg.get("sync_server") or {}).get("hosts") or []),
           "schedule": schedule_cfg(cfg)}
    if STARTER:
        out["arxiv"] = {k: ax.get(k, False if k in ("enabled", "share") else []) for k in ARXIV_KEYS}
    return out


def _texts(v, what, maxn=2000, maxlen=300, dedupe=True, allow_empty=False):
    if not isinstance(v, list) or len(v) > maxn:
        raise ValueError(f"{what}: expected a list of at most {maxn} texts")
    out = []
    for x in v:
        if not isinstance(x, str) or "\n" in x or "\r" in x or len(x.strip()) > maxlen:
            raise ValueError(f"{what}: {str(x)[:40]!r} is not a one-line text of at most {maxlen} characters")
        x = x.strip()
        if not x and not allow_empty:
            continue
        if not dedupe or x not in out:
            out.append(x)
    return out


def _num(v, what, lo, hi):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"{what}: expected a number from {lo} to {hi}")
    return int(v) if float(v).is_integer() else float(v)


def _bool(v, what):
    if not isinstance(v, bool):
        raise ValueError(f"{what}: expected true or false")
    return v


def _fail(msg):
    raise ValueError(msg)


def _regex(s, what):
    try:
        re.compile(s)
    except re.error as e:
        raise ValueError(f"{what}: not a valid regular expression ({e})")
    return s


def _keywords(v):
    """[[key, weight], …] in the order shown (pillar headings _pillarN_… included) → an ordered dict."""
    if not isinstance(v, list) or len(v) > 3000:
        raise ValueError("positive_keywords: expected a list of [keyword, weight] pairs")
    out = {}
    for item in v:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
            raise ValueError("positive_keywords: every entry must be [keyword, weight]")
        k = item[0].strip()
        if not k or len(k) > 300 or "\n" in k:
            raise ValueError(f"positive_keywords: {k[:40]!r} is empty, too long or has a line break")
        if k in out:
            raise ValueError(f"positive_keywords: {k!r} appears twice (a keyword belongs to one pillar)")
        w = _num(item[1], f"positive_keywords[{k!r}] weight", -50, 50)
        if k.startswith("re:"):
            label, sep, rx = k[3:].partition("::")
            if not (sep and label.strip() and rx):
                raise ValueError(f"positive_keywords: {k!r} must look like re:<label>::<regex>")
            _regex(rx, f"positive_keywords[{label}]")
        out[k] = 0 if MARKER_RX.match(k) else w
    return out


def validate(upd, cfg):
    """STARTUP.html's fields → [(config path, value)], or ValueError listing every problem."""
    if not isinstance(upd, dict):
        raise ValueError("config must be a JSON object")
    errs, out = [], []

    def put(path, fn):
        try:
            out.append((path, fn()))
        except ValueError as e:
            errs.append(str(e))

    import catalog
    n_pillars = (len(upd["pillars"]) if isinstance(upd.get("pillars"), list)
                 else len(cfg.get("pillars") or []) if STARTER else len([k for k in catalog.PILLARS if k]))
    for k, v in upd.items():
        if k in STARTER_ONLY and not STARTER:
            errs.append(f"{k}: fixed on this radar (catalog.py), not editable here")
        elif k == "pillars":
            def pillars(v=v):
                names = _texts(v, "pillars", maxn=MAX_PILLARS, maxlen=120, dedupe=False)
                if len(set(n.lower() for n in names)) < len(names):
                    raise ValueError("pillars: two pillars have the same name")
                fx = fixed_pillars()
                if fx and names[:fx] != list(catalog.DEFAULT_PILLARS):
                    raise ValueError(f"pillars: pillars 1–{fx} are fixed on this radar (their cached digests name them); "
                                     "add new pillars after them")
                return names
            put(PATHS[k], pillars)
        elif k == "pillar_short":
            def short(v=v):
                s = _texts(v, "pillar_short", maxn=MAX_PILLARS, maxlen=24, dedupe=False, allow_empty=True)
                if len(s) > n_pillars:
                    raise ValueError("pillar_short: more labels than pillars")
                while s and not s[-1]:
                    s.pop()
                return s
            put(PATHS[k], short)
        elif k == "positive_keywords":
            put(PATHS[k], lambda v=v: _keywords(v))
        elif k in ("negative_keywords", "key1_themes", "vip_authors"):
            put(PATHS[k], lambda v=v, k=k: _texts(v, k, maxlen=200))
        elif k == "negative_override":
            put(PATHS[k], lambda v=v: _regex(v.strip(), "negative_override") if isinstance(v, str) and "\n" not in v
                else _fail("negative_override: expected a one-line regular expression"))
        elif k in ("min_score", "vip_boost"):
            put(PATHS[k], lambda v=v, k=k: _num(v, k, 0, 1000 if k == "min_score" else 50))
        elif k == "anchors":
            def anchors(v=v):
                dois = _texts(v, "anchors", maxn=500, maxlen=200)
                bad = [d for d in dois if not DOI_RX.match(d)]
                if bad:
                    raise ValueError(f"anchors: not a DOI: {', '.join(bad[:3])}")
                return dois
            put(PATHS[k], anchors)
        elif k == "contact_email":
            put(PATHS[k], lambda v=v: v.strip() if isinstance(v, str) and (not v.strip() or EMAIL_RX.match(v.strip()))
                else _fail("contact_email: not an e-mail address"))
        elif k == "data_repo_dir":
            put(PATHS[k], lambda v=v: v.strip() if isinstance(v, str) and len(v) < 500 and not re.search(r"[\r\n\0]", v)
                else _fail("data_repo_dir: expected a folder path"))
        elif k == "schedule":
            if not isinstance(v, dict):
                errs.append("schedule: expected an object")
                continue
            for kk, vv in v.items():
                if kk in ("auto_update_local", "auto_upload_repo"):
                    put(["schedule", kk], lambda vv=vv, kk=kk: _bool(vv, f"schedule.{kk}"))
                elif kk in ("update_time", "upload_time"):
                    put(["schedule", kk], lambda vv=vv, kk=kk: vv.strip() if isinstance(vv, str) and TIME_RX.match(vv.strip())
                        else _fail(f"schedule.{kk}: a time like 07:00 (24 h)"))
                else:
                    errs.append(f"schedule.{kk}: not editable here")
        elif k == "data_repo":
            if not isinstance(v, dict):
                errs.append("data_repo: expected an object")
                continue
            for kk, vv in v.items():
                if kk == "remote_url":
                    def remote(vv=vv):
                        import data_repo
                        if not isinstance(vv, str) or not vv.strip():
                            return ""
                        data_repo.repo_url(vv)  # ValueError for anything but owner/name, a clean URL or a folder
                        return vv.strip()
                    put(["data_repo", kk], remote)
                elif kk == "read_repos":
                    def reads(vv=vv):
                        import data_repo
                        if not isinstance(vv, list) or len(vv) > 20:
                            return _fail("data_repo.read_repos: expected a list of at most 20 repos")
                        out = []
                        for x in vv:  # "owner/name" (isolated) or {"repo", "merge_corpus", "force_merge"} (v3.0.4)
                            obj = isinstance(x, dict)
                            s = x.get("repo") if obj else x
                            if not isinstance(s, str) or not s.strip() or len(s) > 400 or re.search(r"[\r\n\0]", s) \
                                    or (obj and not set(x) <= {"repo", "merge_corpus", "force_merge"}):
                                return _fail("data_repo.read_repos: each entry is owner/name, a URL or a folder, or "
                                             '{"repo": …, "merge_corpus": true|false}')
                            data_repo.repo_url(s.strip())
                            m = obj and _bool(x.get("merge_corpus", False), "data_repo.read_repos.merge_corpus")
                            f = obj and _bool(x.get("force_merge", False), "data_repo.read_repos.force_merge")
                            out.append({"repo": s.strip(), "merge_corpus": True, **({"force_merge": True} if f else {})}
                                       if m else s.strip())
                        return out
                    put(["data_repo", kk], reads)
                elif kk == "peer_id":
                    import store as store_mod
                    put(["data_repo", kk], lambda vv=vv: vv.strip() if isinstance(vv, str) and (
                        not vv.strip() or store_mod.PEER_ID_RX.match(vv.strip())) else _fail(
                            "data_repo.peer_id: letters, digits, '.', '_' or '-' (≤ 39), starting with a letter or digit"))
                elif kk in DATA_REPO_KEYS:
                    put(["data_repo", kk], lambda vv=vv, kk=kk: _bool(vv, f"data_repo.{kk}"))
                else:
                    errs.append(f"data_repo.{kk}: not editable here")
        elif k == "arxiv":
            if not isinstance(v, dict):
                errs.append("arxiv: expected an object")
                continue
            for kk, vv in v.items():
                if kk in ("enabled", "share"):
                    put(["arxiv", kk], lambda vv=vv, kk=kk: _bool(vv, f"arxiv.{kk}"))
                elif kk in ("queries", "exclude_any"):
                    put(["arxiv", kk], lambda vv=vv, kk=kk: _texts(vv, f"arxiv.{kk}", maxn=50, maxlen=500))
                elif kk == "categories":
                    def cats(vv=vv):
                        c = _texts(vv, "arxiv.categories", maxn=40, maxlen=30)
                        bad = [x for x in c if not ARXIV_CAT_RX.match(x)]
                        if bad:
                            raise ValueError(f"arxiv.categories: not an arXiv category: {', '.join(bad[:3])}")
                        return c
                    put(["arxiv", kk], cats)
                elif kk == "must_all":
                    def groups(vv=vv):
                        if not isinstance(vv, list) or len(vv) > 10:
                            raise ValueError("arxiv.must_all: expected up to 10 groups of terms")
                        g = [_texts(x, "arxiv.must_all group", maxn=100, maxlen=100) for x in vv]
                        return [x for x in g if x]
                    put(["arxiv", kk], groups)
                else:
                    errs.append(f"arxiv.{kk}: not editable here")
        elif k == "domain_preset":
            import presets
            put(PATHS[k], lambda v=v: v if v in presets.PRESETS else _fail(
                f"domain_preset: one of {', '.join(presets.PRESETS)}"))
        elif k == "apply_preset":  # {"key": …, "replace_venues": bool} → domain_preset + evidence cues (+ venues)
            import presets
            try:
                out.extend(presets.apply_changes((v or {}).get("key"), cfg, bool((v or {}).get("replace_venues"))))
            except (ValueError, AttributeError) as e:
                errs.append(f"apply_preset: {e}")
        elif k == "deployment":
            put(PATHS[k], lambda v=v: v if v in DEPLOYMENTS else _fail(
                "deployment: local or mesh_lan (🏢 enterprise needs coffee, not a config key)"))
        elif k == "sync_hosts":
            def hosts(v=v):
                h = _texts(v, "sync_server.hosts", maxn=10, maxlen=100)
                bad = [x for x in h if not HOST_RX.match(x)]
                if bad:
                    raise ValueError(f"sync_server.hosts: not a host name or IP address: {', '.join(bad[:3])}")
                return h
            put(PATHS[k], hosts)
        else:
            errs.append(f"{k}: not an editable setting")
    if errs:
        raise ValueError("; ".join(errs))
    return out


MISSING = object()


def _get(obj, path):
    for k in path:
        if not isinstance(obj, dict) or k not in obj:
            return MISSING
        obj = obj[k]
    return obj


def _set(obj, path, value):
    for k in path[:-1]:
        obj = obj.setdefault(k, {})
    obj[path[-1]] = value


def _same(a, b):
    """Equal including the order of dict keys (moving a keyword to another pillar heading is a change)."""
    return a is not MISSING and json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def _dump_style(raw, text):
    """(indent, ensure_ascii) when the file is exactly json.dumps output (a fresh Starter config.json), else None."""
    t = text.replace("\r\n", "\n")
    for indent in (4, 2):
        for ea in (True, False):
            s = json.dumps(raw, indent=indent, ensure_ascii=ea)
            if t in (s, s + "\n"):
                return indent, ea
    return None


_WS = re.compile(r"\s*")


def _members(text, start):
    """The members of the JSON object whose '{' is at `start`: [(key, key_start, value_start, value_end)], close."""
    dec, out = json.JSONDecoder(), []
    i = _WS.match(text, start + 1).end()
    if text[i] == "}":
        return out, i
    while True:
        ks = i
        key, i = dec.raw_decode(text, i)
        i = _WS.match(text, i).end()
        if text[i] != ":":
            raise ValueError("config.json: unexpected layout")
        vs = _WS.match(text, i + 1).end()
        _, ve = dec.raw_decode(text, vs)
        out.append((key, ks, vs, ve))
        i = _WS.match(text, ve).end()
        if text[i] == ",":
            i = _WS.match(text, i + 1).end()
            continue
        if text[i] != "}":
            raise ValueError("config.json: unexpected layout")
        return out, i


def _col(text, pos):
    return pos - (text.rfind("\n", 0, pos) + 1)


def _lines(tokens, width):
    """tokens joined by ', ' into lines of at most `width` characters."""
    lines, cur = [], ""
    for t in tokens:
        if cur and len(cur) + 2 + len(t) > width:
            lines.append(cur)
            cur = ""
        cur = f"{cur}, {t}" if cur else t
    return lines + [cur] if cur else lines


def _fmt(value, indent, col, width=118):
    """A JSON value laid out like the hand-written config.json: short values on one line, long lists wrapped,
    positive_keywords as one paragraph per pillar heading (_pillarN_… on its own line)."""
    dumps = lambda x: json.dumps(x, ensure_ascii=False)
    one, inner = dumps(value), indent + "  "
    avail = max(width - len(inner), 40)
    if isinstance(value, dict) and value:
        marked = any(MARKER_RX.match(k) for k in value)
        if col + len(one) <= width and not marked:
            return one
        paras, cur = [], []
        for k, v in value.items():
            if MARKER_RX.match(k) and cur:
                paras.append(cur)
                cur = []
            cur.append((k, f"{dumps(k)}: {dumps(v)}"))
        paras.append(cur)
        out = []
        for n, para in enumerate(paras):
            head = [para[0][1]] if MARKER_RX.match(para[0][0]) else []
            out += ([""] if n else []) + head + _lines([t for _, t in para[len(head):]], avail)
        last = max(i for i, ln in enumerate(out) if ln)
        body = "\n".join(inner + ln + ("," if i != last else "") if ln else "" for i, ln in enumerate(out))
        return "{\n" + body + "\n" + indent + "}"
    if isinstance(value, list) and value and col + len(one) > width:
        body = ",\n".join(inner + ln for ln in _lines([dumps(x) for x in value], avail))
        return "[\n" + body + "\n" + indent + "]"
    return one


def _paragraphs(items):
    """[(key, value)] split at the pillar headings: each _pillarN_… key starts a new paragraph."""
    out = []
    for k, v in items:
        if MARKER_RX.match(k) or not out:
            out.append([])
        out[-1].append((k, v))
    return out


def _fmt_paragraphs(text, vs, value, indent):
    """positive_keywords with every paragraph (pillar heading + its keywords) that didn't change kept exactly as
    it was written, so adding one keyword shows up as a change to that pillar only."""
    members, _ = _members(text, vs)
    old = {}
    for para in _paragraphs([(k, (ks, s, e)) for k, ks, s, e in members]):
        sig = json.dumps([(k, json.loads(text[s:e])) for k, (_, s, e) in para], ensure_ascii=False)
        old.setdefault(sig, text[para[0][1][0]:para[-1][1][2]])
    inner = indent + "  "
    blocks = []
    for para in _paragraphs(list(value.items())):
        sig = json.dumps(para, ensure_ascii=False)
        if sig in old:
            blocks.append(old[sig])
            continue
        toks = [f"{json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}" for k, v in para]
        head = toks[:1] if MARKER_RX.match(para[0][0]) else []
        lines = head + _lines(toks[len(head):], max(118 - len(inner), 40))
        blocks.append((",\n" + inner).join(lines))
    return "{\n" + inner + (",\n\n" + inner).join(blocks) + "\n" + indent + "}"


def patch_text(text, path, value):
    """config.json text with the value at `path` replaced (or the member added), everything else byte for byte."""
    m = re.search(r"\{", text)
    start = m.start()
    for depth, key in enumerate(path):
        members, close = _members(text, start)
        hit = next((x for x in members if x[0] == key), None)
        if hit is None:
            sub = value
            for k in reversed(path[depth + 1:]):
                sub = {k: sub}
            if members:
                ks, ve = members[-1][1], members[-1][3]
                indent = " " * _col(text, ks)
                if text.rfind("\n", 0, ks) < start:  # all members on the '{' line: next one on a new line
                    indent = " " * (_col(text, start) + 2)
                ins = f",\n{indent}{json.dumps(key, ensure_ascii=False)}: {_fmt(sub, indent, len(indent) + len(key) + 4)}"
                return text[:ve] + ins + text[ve:]
            indent = " " * (_col(text, start) + 2)
            ins = f"\n{indent}{json.dumps(key, ensure_ascii=False)}: {_fmt(sub, indent, len(indent) + len(key) + 4)}\n" \
                  + " " * _col(text, start)
            return text[:start + 1] + ins + text[close:]
        _, ks, vs, ve = hit
        if depth == len(path) - 1:
            indent = " " * _col(text, ks)
            if isinstance(value, dict) and value and text[vs] == "{" and any(MARKER_RX.match(k) for k in value):
                return text[:vs] + _fmt_paragraphs(text, vs, value, indent) + text[ve:]
            return text[:vs] + _fmt(value, indent, _col(text, vs)) + text[ve:]
        if text[vs] != "{":
            raise ValueError(f"config.json: {'.'.join(path[:depth + 1])} is not an object")
        start = vs


def write_config(changes, cfg=None):
    """Write [(path, value)] into config.json; returns the dotted paths that really changed. Only those values are
    rewritten (a json.dumps-formatted file is re-dumped in its own style), so unknown keys, comments-as-keys and a
    hand-made layout survive. The previous file goes to _system/config.backup.json."""
    with open(CONFIG, encoding="utf-8", newline="") as fh:
        text = fh.read()
    raw = json.loads(text)
    todo = [(p, v) for p, v in changes if not _same(_get(raw, p), v)]
    if not todo:
        return []
    crlf = "\r\n" in text
    style = _dump_style(raw, text)
    if style:
        for p, v in todo:
            _set(raw, p, v)
        new = json.dumps(raw, indent=style[0], ensure_ascii=style[1]) + "\n"
    else:
        new = text.replace("\r\n", "\n")
        for p, v in todo:
            new = patch_text(new, p, v)
    check = json.loads(new)
    if any(not _same(_get(check, p), v) for p, v in todo):
        raise RuntimeError("config.json not written: the edited file did not read back the same")
    if crlf:
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        import curation
        with open(curation.system_dir(cfg or load_cfg()) / "config.backup.json", "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except Exception:
        pass
    tmp = CONFIG.with_name("config.json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    os.replace(tmp, CONFIG)
    return [".".join(p) for p, _ in todo]


# ---------- read-only status (booleans only for secrets) ----------

def _ro(path):
    """Read-only, and patient while a run or a click writes (the store is never created or locked by the page)."""
    return sqlite3.connect(f"file:{urllib.parse.quote(Path(path).as_posix(), safe='/:')}?mode=ro", uri=True, timeout=10)


def store_status(cfg):
    sp = HERE / cfg["paths"]["store_file"]
    out = {"file": sp.name, "exists": sp.exists(), "papers": 0, "by_status": {}, "candidates": 0, "external": 0,
           "selected": 0, "cited": 0, "notes": 0, "anchors": {}}
    if not sp.exists():
        return out
    c = _ro(sp)
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='papers'").fetchone():
            return out
        by = dict(c.execute("SELECT COALESCE(status,'?'), COUNT(*) FROM papers GROUP BY status").fetchall())
        cols = {r[1] for r in c.execute("PRAGMA table_info(papers)")}
        out.update(papers=sum(by.values()), by_status=by, candidates=by.get("candidate", 0),
                   external=by.get("external", 0) + by.get("external_ro", 0))
        one = lambda sql: c.execute(sql).fetchone()[0]
        if "user_mark" in cols:
            out["selected"] = one("SELECT COUNT(*) FROM papers WHERE user_mark='selected'")
        if "cited" in cols:
            out["cited"] = one("SELECT COUNT(*) FROM papers WHERE cited=1")
        if "user_note" in cols:
            out["notes"] = one("SELECT COUNT(*) FROM papers WHERE TRIM(COALESCE(user_note,''))<>''")
        if {"user_mark", "cited", "pin"} <= cols:  # the taste model's training examples (scorer.py)
            import scorer
            out["taste_pos"] = one(f"SELECT COUNT(*) FROM papers WHERE {scorer.POS_SQL}")
            out["taste_neg"] = one(f"SELECT COUNT(*) FROM papers WHERE {scorer.NEG_SQL}")
            out["taste_implicit"] = one(f"SELECT COUNT(*) FROM papers WHERE {scorer.IMPLICIT_SQL}")
        out["fts"] = bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='papers_fts'").fetchone())
        dois = [str(d).lower() for d in cfg.get("anchors") or []]
        if dois:
            pin = "pin" if "pin" in cols else "NULL"
            q = f"SELECT doi, title, venue, published, {pin} FROM papers WHERE lower(doi) IN ({','.join('?' * len(dois))})"
            for d, t, v, pub, p in c.execute(q, dois):
                out["anchors"][str(d).lower()] = {"title": t, "venue": v, "year": (pub or "")[:4], "pin": p}
    finally:
        c.close()
    return out


def data_repo_status(cfg):
    import data_repo
    import store as store_mod
    repo = data_repo.repo_dir(cfg)
    out = {"dir": str(repo), "custom_dir": bool((cfg.get("paths") or {}).get("data_repo_dir")), "connected": False,
           "reason": "", "papers": 0, "missing_here": None, "commit": None, "peers": [], "me": "",
           "default_dir": str(HERE.parent / data_repo.REMOTE_MARK)}
    try:
        why = data_repo.check_clone(repo, cfg)
    except RuntimeError as e:
        why = str(e)
    out["reason"], out["connected"] = why or "", not why
    root = Path(cfg["paths"]["onedrive_root"])
    seen = {}
    if not why:
        out["me"] = data_repo.peer_id(cfg, repo)
        r = data_repo._git(repo, "log", "-1", "--format=%h%x09%cI%x09%s", check=False)
        parts = r.stdout.strip().split("\t", 2)
        if len(parts) == 3:
            out["commit"] = {"hash": parts[0], "date": parts[1], "subject": parts[2][:160]}
        db = repo / data_repo.DB_NAME
        if db.exists():
            out["papers"] = data_repo._count(db)
            sp = HERE / cfg["paths"]["store_file"]
            if sp.exists():
                c = _ro(sp)
                try:
                    c.execute("ATTACH DATABASE ? AS s",
                              (f"file:{urllib.parse.quote(db.as_posix(), safe='/:')}?mode=ro",))
                    out["missing_here"] = c.execute("SELECT COUNT(*) FROM s.papers WHERE lower(doi) NOT IN "
                                                    "(SELECT lower(doi) FROM main.papers)").fetchone()[0]
                except sqlite3.Error:
                    pass
                finally:
                    c.close()
        for f in sorted((repo / data_repo.PEERS_DIR).glob("*.json")):
            if not store_mod.PEER_ID_RX.match(f.stem):
                continue
            try:
                ov = json.loads(f.read_text(encoding="utf-8"))
                rows = ov.get("rows") or {}
                seen[f.stem] = {"id": f.stem, "rows": len(rows), "updated": str(ov.get("updated") or "")[:19],
                                "pillars": [str(x)[:80] for x in (ov.get("pillars") or [])][:MAX_PILLARS],
                                "me": f.stem == out["me"],
                                "selected": sum(1 for r in rows.values() if r.get("user_mark") == "selected"),
                                "cited": sum(1 for r in rows.values() if r.get("cited"))}
            except (OSError, ValueError, AttributeError):
                continue
    else:
        out["me"] = data_repo.peer_id(cfg, None)
    for f in sorted((root / "Peers").glob("DASHBOARD_*.html")) if (root / "Peers").exists() else []:
        pid = f.stem[len("DASHBOARD_"):]
        seen.setdefault(pid, {"id": pid, "rows": None, "updated": "", "pillars": [], "me": False})
    for p in seen.values():
        page = root / "Peers" / f"DASHBOARD_{p['id']}.html"
        p["href"] = f"Peers/DASHBOARD_{p['id']}.html" if page.exists() else None
    out["peers"] = sorted(seen.values(), key=lambda p: (not p["me"], p["id"].lower()))
    dcfg = cfg.get("data_repo") or {}
    out["remote_url"] = dcfg.get("remote_url") or (_origin_of(repo) if not why else "")
    out["read_repos"] = []
    store_path = HERE / cfg["paths"]["store_file"]
    for e in data_repo.read_repo_entries(cfg):  # v3.0.4: Domain Similarity Check and mode per repo (no network)
        spec = e["spec"]
        try:
            d = data_repo.read_repo_dir(cfg, spec)
            ready = (d / data_repo.DB_NAME).exists()
            sim = data_repo.similarity(cfg, d, store_path) if ready else None
            out["read_repos"].append({"spec": spec, "label": data_repo.repo_label(spec), "dir": str(d), "ready": ready,
                                      "papers": data_repo._count(d / data_repo.DB_NAME), "merge": e["merge"],
                                      "force": e["force"], "sim": sim,
                                      "mode": data_repo.repo_mode(e, sim) if ready else "isolated"})
        except Exception as err:
            out["read_repos"].append({"spec": spec, "label": spec, "dir": "", "ready": False, "papers": 0,
                                      "error": str(err), "merge": e["merge"], "force": e["force"], "sim": None,
                                      "mode": "isolated"})
    return out


def _origin_of(repo):
    import data_repo
    return data_repo._git(repo, "remote", "get-url", "origin", check=False).stdout.strip()


def has_claude(cfg):
    return shutil.which((cfg.get("claude") or {}).get("command") or "claude") is not None


def status(cfg):
    import fetcher
    try:
        st = store_status(cfg)
    except sqlite3.Error as e:
        st = {"file": cfg["paths"]["store_file"], "exists": True, "papers": 0, "by_status": {}, "candidates": 0,
              "external": 0, "selected": 0, "cited": 0, "notes": 0, "anchors": {}, "error": str(e)}
    pk = cfg.get("positive_keywords") or {}
    n_kw = sum(1 for k, w in pk.items() if not k.startswith("_") and w)
    ready = fetcher.pillars_ready(cfg) if hasattr(fetcher, "pillars_ready") else bool(n_kw)
    try:
        dr = data_repo_status(cfg)
    except Exception as e:  # never let the status page fail on the optional repo
        dr = {"dir": "", "connected": False, "reason": f"status not available: {e}", "papers": 0, "peers": [],
              "me": "", "commit": None, "missing_here": None, "custom_dir": False, "default_dir": ""}
    import catalog
    return {"edition": "starter" if STARTER else "radar", "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "store": st, "pillars": {"count": len([k for k in catalog.PILLARS if k]), "ready": bool(ready),
                                     "keywords": n_kw},
            "env": {"has_ieee_key": bool(os.environ.get("IEEE_API_KEY", "").strip()), "has_claude": has_claude(cfg),
                    "claude_command": (cfg.get("claude") or {}).get("command") or "claude",
                    "has_git": shutil.which("git") is not None,
                    "contact_email_set": bool(EMAIL_RX.match((cfg.get("contact_email") or "").strip()))},
            "data_repo": dr,
            "topics": _topic_status(cfg),
            "taste": _taste_status(cfg, st),
            "deploy": deploy_status(cfg),
            "schedule": {"tasks": [{k: t[k] for k in ("slot", "name", "enabled", "time", "daily")} for t in schedule_tasks(cfg)],
                         "os": "windows" if os.name == "nt" else "cron"},
            "sharing": {"selected": st["selected"], "cited": st["cited"], "notes": st["notes"],
                        "share_curation": share_tags_on(cfg), "share_tags": share_tags_on(cfg),
                        "share_notes": (cfg.get("data_repo") or {}).get("share_notes", True) is not False}}


def _topic_status(cfg):
    """v3.0.6, STARTUP.html → 🔎 Topic Deep-Dive: meta['topics'] (newest first, with report links relative to the page)
    and the latest Local Recall pages. Read only."""
    root, td = Path(cfg["paths"]["onedrive_root"]), Path(cfg["paths"]["topic_dir"])
    try:
        rel = Path(os.path.relpath(td, root)).as_posix()
    except ValueError:
        rel = td.name
    out = {"dir": rel, "dives": [], "recalls": [], "antique_pillars": []}
    sp = HERE / cfg["paths"]["store_file"]
    topics, ant = {}, {}
    if sp.exists():
        c = _ro(sp)
        try:
            r = c.execute("SELECT value FROM meta WHERE key='topics'").fetchone()
            topics = json.loads(r[0]) if r and r[0] else {}
            r = c.execute("SELECT value FROM meta WHERE key='retired_pillars'").fetchone()  # v3.0.8: 🏺 antique pillars
            ant = json.loads(r[0]) if r and r[0] else {}
        except (sqlite3.Error, ValueError):
            topics = {}
        finally:
            c.close()
    out["antique_pillars"] = [{"key": k, **v} for k, v in sorted(ant.items(), key=lambda kv: kv[1].get("retired_at", ""))]
    for slug, t in sorted(topics.items(), key=lambda kv: str(kv[1].get("date") or ""), reverse=True):
        dois, nc = t.get("dois") or [], int(t.get("n_classic") or 0)
        f = td / f"{t.get('date')}_{slug}.html"
        out["dives"].append({"slug": slug, "query": t.get("query") or slug, "date": t.get("date") or "", "n_classic": nc,
                             "n_recent": max(len(dois) - nc, 0), "classics": dois[:nc][:6], "retired": bool(t.get("retired")),
                             "href": f"{rel}/{f.name}" if f.exists() else ""})
    if td.is_dir():
        fs = sorted(td.glob("*_recall_*.html"), key=lambda f: f.stat().st_mtime, reverse=True)[:5]
        out["recalls"] = [{"name": f.stem, "href": f"{rel}/{f.name}"} for f in fs]
    return out


def _taste_status(cfg, st):
    """What scorer.prepare trains on: explicit marks, plus passed-over papers while archives are below `need`."""
    import scorer
    t = scorer.settings(cfg)
    pos, neg, need = st.get("taste_pos", 0), st.get("taste_neg", 0), t["taste_min_examples"]
    imp = min(st.get("taste_implicit", 0), max(0, t["taste_max_negatives"] - neg)) if neg < need and pos >= need else 0
    return {"pos": pos, "neg": neg, "implicit": imp, "weight": t["taste_implicit_weight"], "need": need,
            "enabled": t["taste"], "active": bool(t["taste"] and pos >= need and neg + imp >= need)}


def deploy_status(cfg):
    """The 💻 / 🌐 deployment card: mode, hosts, the phone/LAN link files and Task Scheduler commands to copy."""
    import curation
    root, sysd = Path(cfg["paths"]["onedrive_root"]), Path(cfg["paths"].get("system_dir") or Path(cfg["paths"]["onedrive_root"]) / "_system")
    sc = cfg.get("sync_server") or {}
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe") if py.with_name("pythonw.exe").exists() else py
    pyc = py.with_name("python.exe") if py.with_name("python.exe").exists() else py
    b = HERE / "briefing.py"
    if STARTER:
        q = '\\"'  # schtasks /TR needs the inner quotes escaped as \"
        tasks = [f'schtasks /Create /TN IC_Paper_Radar_GUI /TR "{q}{pyw}{q} {q}{b}{q} --gui --no-browser" /SC ONLOGON /IT /F',
                 f'schtasks /Create /TN IC_Paper_Radar_Daily /TR "{q}{pyc}{q} {q}{b}{q}" /SC DAILY /ST 07:00 /IT /F',
                 "schtasks /Query /TN IC_Paper_Radar_GUI /V /FO LIST"]
    else:
        tasks = ["schtasks /Query /TN PaperRadar_SyncServer /V /FO LIST",
                 "schtasks /End /TN PaperRadar_SyncServer & schtasks /Run /TN PaperRadar_SyncServer",
                 "schtasks /Query /TN PaperRadar_Daily /V /FO LIST",
                 "schtasks /Query /TN PaperRadar_FridayNoonSync /V /FO LIST"]
    return {"mode": deployment(cfg), "hosts": list(sc.get("hosts") or []), "port": sc.get("port", 18765),
            "gui_port": GUI_PORT, "lan_ok": [h for h in sc.get("hosts") or [] if _lan_ip(h)],
            "phone_link": "Open_on_iPhone.txt" if (root / "Open_on_iPhone.txt").exists() else None,
            "gui_link": str(sysd / "gui_link.txt") if (sysd / "gui_link.txt").exists() else None,
            "server": "radar_sync_server.py (:18765, PaperRadar_SyncServer)" if not STARTER else "python briefing.py --gui",
            "tasks": tasks}


# ---------- the actions, one at a time, in a background worker ----------

ACTIONS = {"rescore_and_seed": "▶ Run & Update Dashboard", "pull_data_repo": "📥 Read / Pull Shared Repos",
           "sync_data_repo": "🔄 Full Sync (Pull + Push)", "daily": "🌐 Fetch New Papers Now",
           "init_data_repo": "✨ Create / Init My Private Repo",
           "topic_recall": "⚡ Instant Local Recall (0 API)", "topic_dive": "🔎 Run Full Topic Dive (6 Classics + 8 SOTA)",
           "drop_topic": "🗑 Delete Topic Dive", "add_pillar": "✅ Create Pillar & Seed Papers",
           "retire_topic": "🏺 Retire Topic Dive", "unretire_topic": "↺ Restore Topic Dive", "retire_pillar": "🏺 Retire Pillar",
           "self_update": "⬆️ Update Radar Now", "clone_data_repo": "📦 Clone Shared Repo",
           "apply_schedule": "⏰ Save & Apply Friday Schedule"}
TOPIC_ACTIONS = ("topic_recall", "topic_dive")
SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


def plan(action, cfg, opts=None):
    """briefing.py argument lists for an action (run in order; a failed step doesn't stop the next one).
    Run & Update first adopts the merged ('external') papers that score ≥ min_score with the keywords just saved
    (data_repo.adopt_external), so a collaborator who read the shared corpus before setting pillars gets them."""
    adopt = [["--adopt-external"]] if (cfg.get("data_repo") or {}).get("adopt_external", True) is not False else []
    o = opts or {}
    q = str(o.get("query") or "<your research question>")  # --topic=… / --recall=…: a query can't pose as a flag
    return {"topic_recall": [["--recall=" + q]],
            "topic_dive": [["--topic=" + q] + ([] if has_claude(cfg) and not o.get("zero") else ["--no-claude"])],
            "drop_topic": [["--drop-topic=" + str(o.get("slug") or "<slug>")]],
            "retire_topic": [["--retire-topic=" + str(o.get("slug") or "<slug>")]],  # v3.0.8: 🏺 Retire as Antique
            "unretire_topic": [["--unretire-topic=" + str(o.get("slug") or "<slug>")]],
            "retire_pillar": [["--retire-pillar", str(o.get("pillar") or "N")]],
            # v3.0.7 ＋ Add New Pillar: re-score with the new keywords, file its anchors under it, seed ≥ 10 candidates
            "add_pillar": [["--rescore", "--seed-pillars", "5", "--no-claude"]
                           + (["--pillar", str(o.get("pillar") or "N"), "--pin-anchors", *o["anchors"]] if o.get("anchors") else [])],"rescore_and_seed": adopt + [["--rescore", "--seed-pillars", "5", "--no-claude"]],
            "pull_data_repo": [["--pull-data-repo"]],
            "self_update": [["--self-update"]], "clone_data_repo": [["--clone-data-repo"]],
            "apply_schedule": [["--apply-schedule"]],
            "sync_data_repo": [["--sync"], ["--sync-data-repo"]],
            "init_data_repo": [["--init-data-repo", str((opts or {}).get("spec") or "<owner/name>")]],
            "daily": [[] if has_claude(cfg) else ["--no-claude"]]}[action]


def python_exe():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))  # the sync server runs under pythonw: children need a stdout
    return str(exe)


def python_hint():
    if STARTER:
        return "python3" if sys.platform == "darwin" else "python"
    try:
        import env_guard
        return f'"{env_guard.MINISFORUM_PYTHON}"'
    except Exception:
        return "python"


def commands(cfg):
    """The same actions as command lines, for the static page (copy & paste)."""
    py = python_hint()
    return {a: [f"{py} briefing.py {' '.join(_shell_arg(x) for x in args)}".rstrip() for args in plan(a, cfg)] for a in ACTIONS}


def _shell_arg(x):
    """--topic=sub-100nW sensor → --topic "sub-100nW sensor" (for the copy & paste commands)."""
    k, eq, v = x.partition("=")
    if eq and k.startswith("--") and (" " in v or "<" in v):
        return f'{k} "{v}"'
    return x


class Jobs:
    """One background job at a time; its output lines are kept for GET /api/job."""

    def __init__(self):
        self.lock = threading.Lock()
        self.n = 0
        self.child = None  # pid of the briefing.py this worker is running now
        self.st = {"id": 0, "action": None, "running": False, "rc": None, "lines": [], "base": 0,
                   "started": None, "finished": None}

    def view(self, since=0):
        with self.lock:
            st = self.st
            i = max(int(since) - st["base"], 0)
            return {**{k: st[k] for k in ("id", "action", "running", "rc", "started", "finished")}, **{
                "label": ACTIONS.get(st["action"] or "", ""), "lines": st["lines"][i:],
                "next": st["base"] + len(st["lines"])}}

    def log(self, line):
        with self.lock:
            self.st["lines"].append(str(line).rstrip("\r\n")[:4000])
            if len(self.st["lines"]) > 4000:
                del self.st["lines"][:1000]
                self.st["base"] += 1000

    def start(self, action, opts=None):
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        with self.lock:
            if self.st["running"]:
                raise RuntimeError(f"busy: {ACTIONS[self.st['action']]} is still running")
            self.n += 1
            self.st = {"id": self.n, "action": action, "running": True, "rc": None, "lines": [], "base": 0,
                       "started": datetime.now().isoformat(timespec="seconds"), "finished": None}
        threading.Thread(target=self._run, args=(action, opts or {}), daemon=True).start()
        return self.view()

    def _proc(self, cmd, show):
        self.log(f"$ {show}")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", GIT_TERMINAL_PROMPT="0")
        p = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
        self.child = p.pid
        try:
            for line in p.stdout:
                self.log(line)
            return p.wait()
        finally:
            self.child = None

    def _clone(self, opts):
        import data_repo
        url = str(opts.get("clone_url") or "").strip()
        if not CLONE_RX.match(url):
            raise ValueError("not a clone URL (https://… or git@…, no user name or token)")
        cfg = load_cfg()
        d = str(opts.get("clone_dir") or "").strip()
        target = Path(d).expanduser() if d else data_repo.repo_dir(cfg)
        if not target.is_absolute():
            target = HERE / target
        target = target.resolve()
        if target.exists() and any(target.iterdir()):
            why = data_repo.check_clone(target, cfg)
            if why:
                raise ValueError(f"{target} exists and is not a clone of {data_repo.REMOTE_MARK}: choose another folder")
            self.log(f"Already cloned: {target}")
        else:
            rc = self._proc(["git", "clone", "--", url, str(target)], f"git clone {url} {target}")
            if rc:
                self.log("git clone failed: check the URL and that your GitHub account can read the repo "
                         "(on Windows, Git Credential Manager asks you to sign in once)")
                return rc
        why = data_repo.check_clone(target, dict(cfg, data_repo=dict(cfg.get("data_repo") or {}, remote_url=url)))
        if why:
            self.log(f"[WARN] {why}: is this an IC Paper Radar data repo?")
        changes = [(["data_repo", "remote_url"], url)]
        if target != data_repo.repo_dir(cfg).resolve():
            changes.append((["paths", "data_repo_dir"], str(target)))
        changed = write_config(changes, cfg)
        if changed:
            self.log(f"config.json: {', '.join(changed)}")
        return 0

    def _run(self, action, opts):
        rc = 0
        try:
            if action == "pull_data_repo" and opts.get("clone_url"):
                rc = self._clone(opts)
            if not rc:
                for args in plan(action, load_cfg(), opts):
                    r = self._proc([python_exe(), "-u", str(HERE / "briefing.py"), *args],
                                   f"python briefing.py {' '.join(args)}".rstrip())
                    rc = r or rc  # like run_data_repo_sync.cmd: the last failure's code, else 0
                    self.log(f"(exit {r})")
        except Exception as e:
            self.log(f"[ERROR] {e}")
            rc = 1
        finally:
            if action in ("self_update", "pull_data_repo", "sync_data_repo", "clone_data_repo", "init_data_repo"):
                forget_updates()
            if action == "self_update" and not rc:
                try:  # v3.2.0: this server's own code is new too (the same module objects, re-executed)
                    import importlib
                    for m in ("data_repo", "html_renderer", __name__):
                        if m in sys.modules:
                            importlib.reload(sys.modules[m])
                    self.log("Server code reloaded: the page shows the new version.")
                except Exception as e:
                    self.log(f"[WARN] the new server code loads at the next start of the Control Center: {e}")
            with self.lock:
                self.st.update(running=False, rc=rc, finished=datetime.now().isoformat(timespec="seconds"))
            try:
                write_startup(load_cfg(), log=lambda m: None)  # the status on the static page, after the job
            except Exception:
                pass


def clone_needed(cfg):
    """v3.2.0: data_repo.remote_url is set but there is no clone at paths.data_repo_dir (or the default folder)."""
    import data_repo
    url = ((cfg.get("data_repo") or {}).get("remote_url") or "").strip()
    return bool(url) and not (data_repo.repo_dir(cfg) / ".git").exists()


def _save(req):
    cfg = load_cfg()
    changed = write_config(validate(req.get("config") or {}, cfg), cfg) if req.get("config") else []
    if changed:
        cfg = load_cfg()
        try:
            write_startup(cfg, log=lambda m: None)
        except Exception:
            pass
    return cfg, changed


def external_run(jobs):
    """The run holding .run.lock when it isn't this server's own job: {id, what, pid, args, started}, else None."""
    import run_lock
    if jobs.view()["running"]:
        return None
    h = run_lock.holder()
    if not h or (jobs.child and h.get("pid") == jobs.child):
        return None
    return {"id": f"{h.get('pid')}-{h.get('started')}", "what": run_lock.describe(h), "pid": h.get("pid"),
            "args": h.get("args") or [], "started": h.get("started")}


def search_store(cfg, q, limit=30):
    """GET /api/search?q=…: the store's FTS5 index (bm25, title ×5 …, store.FTS_WEIGHTS), read only; LIKE
    without it. Returns (mode, rows)."""
    import store as store_mod
    sp = HERE / cfg["paths"]["store_file"]
    q = (q or "").strip()[:200]
    if not sp.exists() or not q:
        return "none", []
    c = _ro(sp)
    c.row_factory = sqlite3.Row
    cols = "p.doi, p.title, p.venue, p.published, p.status, p.user_mark, p.cited, p.score, p.user_note"
    try:
        if store_mod.fts5_available() and c.execute("SELECT 1 FROM sqlite_master WHERE name='papers_fts'").fetchone():
            fq = store_mod.fts_query(q, prefix=True)
            if not fq:
                return "fts5", []
            w = ", ".join(str(x) for x in store_mod.FTS_WEIGHTS)
            rows = c.execute(f"SELECT {cols}, snippet(papers_fts, 2, '[', ']', '…', 14) AS snip FROM papers_fts "
                             "JOIN papers p ON p.rowid = papers_fts.rowid WHERE papers_fts MATCH ? "
                             f"ORDER BY bm25(papers_fts, {w}) LIMIT ?", (fq, int(limit))).fetchall()
            return "fts5", [dict(r) for r in rows]
        words = re.findall(r"[^\W_]+", q)[:8]
        cond = " AND ".join("(p.title LIKE ? OR p.abstract LIKE ?)" for _ in words) or "0"
        rows = c.execute(f"SELECT {cols}, '' AS snip FROM papers p WHERE {cond} ORDER BY p.published DESC LIMIT ?",
                         (*[f"%{x}%" for x in words for _ in range(2)], int(limit))).fetchall()
        return "like", [dict(r) for r in rows]
    except sqlite3.Error as e:
        return f"error: {e}", []
    finally:
        c.close()


def api_get(path, query, jobs):
    """(http code, JSON object) for GET /api/config, /api/job, /api/active and /api/search."""
    if path == "/api/search":
        mode, rows = search_store(load_cfg(), (query.get("q") or [""])[0])
        return 200, {"ok": True, "mode": mode, "results": rows}
    if path == "/api/topic_parse":  # v3.0.7: how topic.py splits a question into concept groups (read only)
        import topic
        cfg = load_cfg()
        groups, limit = topic.parse_query(str((query.get("q") or [""])[0])[:300], cfg)
        show = [[t.split("::")[0][3:] if t.startswith("re:") else t for t in g[:4]] for g in groups]
        return 200, {"ok": True, "groups": show, "limit_nw": limit}
    if path == "/api/check_updates":  # v3.2.0: 🚀 new radar version / 🔔 shared repo update banners
        return 200, {"ok": True, **check_updates(load_cfg(), force=(query.get("force") or [""])[0] == "1")}
    if path == "/api/config":
        cfg = load_cfg()
        return 200, {"ok": True, "editable": editable(cfg), "status": status(cfg), "job": jobs.view(10 ** 9),
                     "active": external_run(jobs)}
    if path == "/api/job":
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        return 200, {"ok": True, **jobs.view(since)}
    if path == "/api/active":
        import run_lock
        try:
            since = int((query.get("since") or ["0"])[0])
        except ValueError:
            since = 0
        ext = external_run(jobs)
        lines, nxt = run_lock.read_log(since, finished=not ext)
        return 200, {"ok": True, "active": bool(ext), "id": ext and ext["id"], "what": ext["what"] if ext else "",
                     "lines": lines, "next": nxt}
    return 404, {"ok": False, "error": "not found"}


def api_post(path, req, jobs):
    """(http code, JSON object) for POST /api/config and /api/run."""
    if not isinstance(req, dict):
        return 400, {"ok": False, "error": "expected a JSON object"}
    try:
        if path == "/api/plan_keywords":  # v3.0.5: 🪄 the 0-token keyword & weight planner (read only)
            import keyword_planner
            return keyword_planner.api(load_cfg(), req)
        if path == "/api/config":
            cfg, changed = _save(req)
            return 200, {"ok": True, "changed": changed, "editable": editable(cfg), "status": status(cfg),
                         "clone_needed": clone_needed(cfg)}
        if path == "/api/run":
            action = req.get("action")
            if action not in ACTIONS:
                return 400, {"ok": False, "error": f"unknown action {action!r}"}
            if jobs.view()["running"]:  # a double click, or a second tab
                return 409, {"ok": False, "error": "already_running", "already_running": True, "job": jobs.view()}
            ext = external_run(jobs)
            if ext:  # the 07:00 run, the Friday sync, a terminal or another Control Center window
                return 409, {"ok": False, "error": "already_running", "already_running": True, "active": ext}
            if action == "pull_data_repo" and req.get("clone_url"):
                import data_repo
                url = str(req["clone_url"]).strip()
                if not CLONE_RX.match(url):
                    return 400, {"ok": False, "error": "not a clone URL (https://… or git@…, without a user name or token)"}
            if action in TOPIC_ACTIONS:  # v3.0.6: 🔎 Topic Deep-Dive & Local Recall
                qq = re.sub(r"\s+", " ", str(req.get("query") or "")).strip()
                if not 3 <= len(qq) <= 300 or not re.search(r"[^\W\d_]{2}", qq):
                    return 400, {"ok": False, "error": "type a topic or research question (3–300 characters)"}
                req["query"], req["zero"] = qq, bool(req.get("zero"))
            if action == "retire_pillar":  # v3.0.8: an existing, non-fixed pillar, and nothing else unsaved with it
                import catalog
                load_cfg()
                n = req.get("pillar")
                if not isinstance(n, int) or isinstance(n, bool) or n not in catalog.PILLARS or n <= fixed_pillars() or not n:
                    return 400, {"ok": False, "error": "retire_pillar: not a pillar that can be retired"}
                req.pop("config", None)
            if action in ("drop_topic", "retire_topic", "unretire_topic") and not SLUG_RX.match(str(req.get("slug") or "")):
                return 400, {"ok": False, "error": "unknown topic dive"}
            _, changed = _save(req)
            if action == "init_data_repo":
                import data_repo
                spec = str(req.get("spec") or "").strip()
                try:
                    data_repo.repo_url(spec)
                except ValueError as e:
                    return 400, {"ok": False, "error": str(e)}
            if action == "add_pillar":  # the new pillar must be in the config just saved
                import catalog
                load_cfg()
                n = req.get("pillar")
                if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= MAX_PILLARS) or n not in catalog.PILLARS:
                    return 400, {"ok": False, "error": "add_pillar: that pillar is not in config.json"}
                an = req.get("anchors") or []
                if not isinstance(an, list) or len(an) > 10 or not all(isinstance(d, str) and DOI_RX.match(d) for d in an):
                    return 400, {"ok": False, "error": "add_pillar: anchors must be up to 10 DOIs"}
            job = jobs.start(action, {k: req.get(k) for k in ("clone_url", "clone_dir", "spec", "query", "zero", "slug",
                                                              "pillar", "anchors")})
            return 200, {"ok": True, "changed": changed, "job": job}
    except ValueError as e:
        return 400, {"ok": False, "error": str(e)}
    except RuntimeError as e:
        return 409, {"ok": False, "error": str(e)}
    return 404, {"ok": False, "error": "not found"}


# ---------- v3.2.0: update checks, self-update, Friday schedule ----------
GIT_ENV = dict(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="", SSH_ASKPASS="")
_UPD = {"t": 0.0, "res": None}
_UPD_LOCK = threading.Lock()


def _gitq(cwd, *args, timeout=GIT_TIMEOUT):
    """git in `cwd` that never prompts and never hangs: (returncode, stdout); (-1, '') on a timeout or no git."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=dict(os.environ, **GIT_ENV), stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return -1, ""


def code_root():
    """The git work tree this radar's code lives in (the Starter folder, or the projects repo), else None (a ZIP)."""
    rc, out = _gitq(HERE, "rev-parse", "--show-toplevel", timeout=10)
    return Path(out) if rc == 0 and out else None


def _code_remote(top):
    rc, url = _gitq(top, "remote", "get-url", "origin", timeout=10)
    return url if rc == 0 and url else (REPO_URL + ".git" if STARTER else "")


def _remote_sha(cwd, remote, ref):
    rc, out = _gitq(cwd, "ls-remote", remote, ref)
    return out.split()[0] if rc == 0 and out else None


def _have_ancestor(cwd, sha):
    """True when commit `sha` is already in this clone's HEAD history (so the remote has nothing new)."""
    return (_gitq(cwd, "cat-file", "-e", sha + "^{commit}", timeout=10)[0] == 0
            and _gitq(cwd, "merge-base", "--is-ancestor", sha, "HEAD", timeout=10)[0] == 0)


def _check_code():
    top = code_root()
    if not top:
        return {"state": "zip" if STARTER else "unknown", "behind": 0, "remote": REPO_URL if STARTER else ""}
    remote = _code_remote(top)
    head = _gitq(top, "rev-parse", "HEAD", timeout=10)[1]
    sha = _remote_sha(top, remote, "refs/heads/main") if remote else None
    out = {"state": "current", "behind": 0, "remote": re.sub(r"\.git$", "", remote), "head": head[:7], "head_full": head}
    if not sha:
        return dict(out, state="offline")
    if sha == head or _have_ancestor(top, sha):
        return out
    n = 0
    if _gitq(top, "fetch", "--quiet", remote, "main", timeout=15)[0] == 0:
        rc, cnt = _gitq(top, "rev-list", "--count", "HEAD..FETCH_HEAD", timeout=10)
        n = int(cnt) if rc == 0 and cnt.isdigit() else 0
        if rc == 0 and n == 0:
            return out
    return dict(out, state="behind", behind=n)


def _check_repo(label, d, kind, ref):
    """One data repo clone: has its origin got commits this clone lacks?"""
    out = {"label": label, "kind": kind, "dir": str(d), "state": "current"}
    if not (d / ".git").exists():
        return dict(out, state="no_clone")
    head = _gitq(d, "rev-parse", "HEAD", timeout=10)[1]
    out["head_full"] = head
    sha = _remote_sha(d, "origin", ref)
    if not sha:
        return dict(out, state="offline")
    return out if sha == head or _have_ancestor(d, sha) else dict(out, state="behind")


def _check_all(cfg):
    import data_repo
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    url = ((cfg.get("data_repo") or {}).get("remote_url") or "").strip()
    prim = data_repo.repo_dir(cfg)
    if (prim / ".git").exists() or url:
        jobs.append((data_repo.primary_label(cfg), prim, "primary", "refs/heads/main"))
    for e in data_repo.read_repo_entries(cfg):
        try:
            d = data_repo.read_repo_dir(cfg, e["spec"])
        except Exception:
            continue
        if Path(e["spec"]).expanduser().exists():
            continue  # a local folder: nothing to ask a remote
        jobs.append((data_repo.repo_label(e["spec"]), d, "read", "HEAD"))
    with ThreadPoolExecutor(max_workers=6) as ex:
        code = ex.submit(_check_code)
        repos = [ex.submit(_check_repo, *j) for j in jobs]
        return {"code": code.result(), "repos": [f.result() for f in repos]}


def _meta_cache(cfg, value=None):
    """The last check in the store's meta table (key update_check), so a restarted server doesn't ask again."""
    sp = HERE / cfg["paths"]["store_file"]
    if not sp.exists():
        return None
    try:
        c = sqlite3.connect(sp, timeout=1)
        try:
            if value is None:
                r = c.execute("SELECT value FROM meta WHERE key='update_check'").fetchone()
                return json.loads(r[0]) if r else None
            c.execute("INSERT OR REPLACE INTO meta VALUES ('update_check', ?)", (json.dumps(value, ensure_ascii=False),))
            c.commit()
        finally:
            c.close()
    except (sqlite3.Error, ValueError):
        return None


def forget_updates():
    with _UPD_LOCK:
        _UPD.update(t=0.0, res=None)


def _heads_moved(res):
    """True when a clone's local HEAD is no longer the one the cached check saw (a pull, an update, a new clone
    by any process): the cache is then stale."""
    c = res.get("code") or {}
    if c.get("head_full"):
        top = code_root()
        if not top or _gitq(top, "rev-parse", "HEAD", timeout=10)[1] != c["head_full"]:
            return True
    for r in res.get("repos") or []:
        d = Path(r.get("dir") or "")
        if r.get("state") == "no_clone":
            if (d / ".git").exists():
                return True
        elif r.get("head_full") is not None and _gitq(d, "rev-parse", "HEAD", timeout=10)[1] != r["head_full"]:
            return True
    return False


def check_updates(cfg, force=False):
    """{"code": {state: current|behind|offline|zip|unknown, behind: N, remote}, "repos": [{label, kind, state}],
    "checked": iso, "cached": bool}. Cached UPDATE_TTL seconds (memory, then meta); force=True asks again."""
    now = time.time()
    with _UPD_LOCK:
        res, t = _UPD["res"], _UPD["t"]
    if not force and not res:
        m = _meta_cache(cfg)
        if isinstance(m, dict) and isinstance(m.get("t"), (int, float)) and isinstance(m.get("res"), dict):
            res, t = m["res"], m["t"]
    if not force and res and now - t < UPDATE_TTL and not _heads_moved(res):
        return dict(res, cached=True)
    res = dict(_check_all(cfg), checked=datetime.now().isoformat(timespec="seconds"))
    with _UPD_LOCK:
        _UPD.update(t=now, res=res)
    _meta_cache(cfg, {"t": now, "res": res})
    return dict(res, cached=False)


PROTECTED = ("config.json", "config.local.json", ".env", "output", "radar.log", ".run.lock", ".run.log", ".radar_token")


def self_update(cfg, log=print):
    """⬆️ Update Radar Now: `git pull --ff-only` in the code repo. A Starter folder that came from a ZIP (no .git)
    becomes a clone of the public repo: git init, fetch main, then only the repo's files are checked out
    (config.json, the paper store, output/ and other ignored files are never touched). Returns 0 / 1."""
    def run(*args, timeout=180):
        rc, out = _gitq(cwd, *args, timeout=timeout)
        for line in out.splitlines()[-20:]:
            log("  " + line)
        return rc
    top = code_root()
    if top:
        cwd, remote = top, _code_remote(top)
        if not remote:
            log("No origin remote: nothing to update from")
            return 1
        log(f"git pull --ff-only {re.sub(r'.git$', '', remote)} main in {top}")
        if run("pull", "--ff-only", remote, "main"):
            log("[ERROR] not updated: local changes or local commits are in the way (git status shows them). "
                "Nothing was changed.")
            return 1
        log("Updated to " + _gitq(top, "log", "-1", "--format=%h %s", timeout=10)[1])
        return 0
    if not STARTER:
        log("[ERROR] this radar's folder is not a git clone")
        return 1
    cwd = HERE
    store_name = Path(cfg["paths"]["store_file"]).name
    log(f"This folder has no .git (a ZIP copy): turning it into a clone of {REPO_URL}")
    if run("init", "-q") or run("remote", "add", "origin", REPO_URL + ".git") and \
            run("remote", "set-url", "origin", REPO_URL + ".git"):
        return 1
    if run("fetch", "-q", "origin", "main", timeout=600):
        log("[ERROR] could not fetch the public repo (offline?)")
        return 1
    rc, names = _gitq(cwd, "ls-tree", "-r", "--name-only", "FETCH_HEAD", timeout=30)
    keep = set(PROTECTED) | {store_name}
    files = [n for n in names.splitlines() if n and n.split("/")[0] not in keep and not n.endswith((".sqlite", ".db"))]
    if rc or not files:
        log("[ERROR] the fetched tree is empty")
        return 1
    run("symbolic-ref", "HEAD", "refs/heads/main")
    if run("reset", "-q", "FETCH_HEAD"):  # HEAD and index only: the working folder is untouched
        return 1
    for i in range(0, len(files), 40):
        if run("checkout", "FETCH_HEAD", "--", *files[i:i + 40]):
            return 1
    run("branch", "--set-upstream-to=origin/main", "main")
    log(f"Updated: {len(files)} code files from {REPO_URL} (config.json, {store_name} and output/ untouched); "
        "from now on ⬆️ Update Radar Now is a git pull")
    return 0


def schedule_tasks(cfg):
    """The OS tasks behind the two Friday slots. Minisforum radar: the existing PaperRadar_Daily (every day at
    update_time; on Fridays it pulls first and finalizes) and PaperRadar_FridayNoonSync (Fri, upload_time).
    Starter: IC_Paper_Radar_FridayUpdate (briefing.py --friday) and IC_Paper_Radar_FridayUpload
    (briefing.py --friday-upload), both weekly on Friday."""
    sc = schedule_cfg(cfg)
    b = str(HERE / "briefing.py")
    if STARTER:
        up = [b, "--friday"] + ([] if has_claude(cfg) else ["--no-claude"])
        t = [("update", "IC_Paper_Radar_FridayUpdate", False, python_exe(), up),
             ("upload", "IC_Paper_Radar_FridayUpload", False, python_exe(), [b, "--friday-upload"])]
    else:
        t = [("update", "PaperRadar_Daily", True, str(HERE / "run_radar.cmd"), []),
             ("upload", "PaperRadar_FridayNoonSync", False, str(HERE / "run_data_repo_sync.cmd"), [])]
    key = {"update": ("auto_update_local", "update_time"), "upload": ("auto_upload_repo", "upload_time")}
    return [{"slot": slot, "name": name, "daily": daily, "exe": exe, "args": args,
             "enabled": sc[key[slot][0]], "time": sc[key[slot][1]]} for slot, name, daily, exe, args in t]


def _ps_q(x):
    return "'" + str(x).replace("'", "''") + "'"


def _win_arg(x):
    return f'"{x}"' if re.search(r"\s", str(x)) else str(x)


def schtasks_script(tasks):
    """PowerShell that creates / updates / disables the tasks (StartWhenAvailable, runs on battery). Each task in
    its own try: one refused task (e.g. created as Administrator) is reported and the others still apply."""
    out = ["$ErrorActionPreference = 'Stop'", "$failed = $false"]
    for t in tasks:
        n = _ps_q(t["name"])
        trig = (f"New-ScheduledTaskTrigger -Daily -At {_ps_q(t['time'])}" if t["daily"]
                else f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At {_ps_q(t['time'])}")
        when = ("every day" if t["daily"] else "Fridays") + " at " + t["time"]
        if t["enabled"]:
            args = " ".join(_win_arg(a) for a in t["args"])
            act = (f"New-ScheduledTaskAction -Execute {_ps_q(t['exe'])} -WorkingDirectory {_ps_q(HERE)}"
                   + (f" -Argument {_ps_q(args)}" if args else ""))
            body = (f"$t = Get-ScheduledTask -TaskName {n} -ErrorAction SilentlyContinue\n"
                    f"$tr = {trig}\n"
                    "$st = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
                    "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)\n"
                    f"if ($t) {{ Set-ScheduledTask -TaskName {n} -Trigger $tr -Settings $st | Out-Null; "
                    f"Enable-ScheduledTask -TaskName {n} | Out-Null; 'updated  {t['name']}: {when}' }}\n"
                    f"else {{ Register-ScheduledTask -TaskName {n} -Action ({act}) -Trigger $tr -Settings $st "
                    f"-Description 'Paper Radar Friday {t['slot']} (STARTUP.html Step 2)' | Out-Null; "
                    f"'created  {t['name']}: {when}' }}")
        else:
            body = (f"$t = Get-ScheduledTask -TaskName {n} -ErrorAction SilentlyContinue\n"
                    f"if ($t) {{ Disable-ScheduledTask -TaskName {n} | Out-Null; 'disabled {t['name']}' }} "
                    f"else {{ 'off      {t['name']} (not installed)' }}")
        out.append(f"try {{\n{body}\n}} catch {{ $failed = $true; 'FAILED   {t['name']}: ' + $_.Exception.Message.Trim() }}")
    out.append("if ($failed) { exit 3 }")
    return "\n".join(out)


CRON_MARK = "# paper-radar:"


def cron_text(tasks, current):
    """The new crontab: the radar's own lines (CRON_MARK) replaced, every other line kept."""
    keep = [ln for ln in (current or "").splitlines() if CRON_MARK not in ln]
    for t in tasks:
        if not t["enabled"]:
            continue
        hh, mm = t["time"].split(":")
        cmd = " ".join(shlex.quote(str(a)) for a in [t["exe"], *t["args"]])
        keep.append(f"{int(mm)} {int(hh)} * * {'*' if t['daily'] else 5} cd {shlex.quote(str(HERE))} && "
                    f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH {cmd} "
                    f">> radar.log 2>&1 {CRON_MARK}{t['slot']}")
    return "\n".join(keep).strip("\n") + "\n"


def apply_schedule(cfg, log=print):
    """⏰ Save & Apply Friday Schedule: the OS tasks to match config.json "schedule". Returns 0 / 1."""
    tasks = schedule_tasks(cfg)
    sc = schedule_cfg(cfg)
    if sc["auto_update_local"] and sc["auto_upload_repo"] and sc["upload_time"] <= sc["update_time"]:
        log(f"[WARN] upload {sc['upload_time']} is not after the local update {sc['update_time']}: "
            "the upload then carries last week's marks")
    if os.name == "nt":
        try:
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-Command", schtasks_script(tasks)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"[ERROR] PowerShell: {e}")
            return 1
        for line in (r.stdout or "").splitlines():
            if line.strip():
                log("  " + line.rstrip())
        if r.returncode:
            if "denied" in (r.stdout or "").lower():
                log("[ERROR] Windows refused to change a task that was created as Administrator. Run this once from "
                    f"an Administrator terminal in {HERE}:  {python_hint()} briefing.py --apply-schedule")
            else:
                log("[ERROR] " + ((r.stderr or "").strip()[-600:] or "a task was not applied (see above)"))
            return 1
    else:
        try:
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
            new = cron_text(tasks, cur.stdout if cur.returncode == 0 else "")
            r = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"[ERROR] crontab: {e}")
            return 1
        if r.returncode:
            log("[ERROR] crontab: " + (r.stderr or "").strip()[-400:])
            return 1
        for t in tasks:
            log(f"  {'set ' if t['enabled'] else 'off '} {t['slot']}: " + (f"Fridays at {t['time']}" if t["enabled"] else "removed"))
        if sys.platform == "darwin":
            log("  macOS: the Mac must be awake at those times (cron skips a sleeping Mac's runs)")
    log("Friday schedule applied.")
    return 0


# ---------- STARTUP.html ----------

def _preset_changes(cfg):
    """{key: {"base": [[path, value]…], "venues": […] or None}} for applying a preset on the static page."""
    import presets
    out = {}
    for k in presets.PRESETS:
        try:
            v = presets.apply_changes(k, cfg, True)
        except ValueError:
            v = None
        out[k] = {"base": presets.apply_changes(k, cfg, False), "venues": v}
    return out


def page_data(cfg):
    import curation
    import presets
    sync = curation.page_sync_config(cfg)
    return {"edition": "starter" if STARTER else "radar", "editable": editable(cfg), "status": status(cfg),
            "raw": raw_config(), "paths": PATHS, "here": str(HERE), "gui_port": GUI_PORT, "data_url": DATA_URL,
            "commands": commands(cfg), "actions": ACTIONS, "max_pillars": MAX_PILLARS, "fixed_pillars": fixed_pillars(),
            "presets": presets.summary(), "preset_changes": _preset_changes(cfg),
            "sync": sync if sync.get("hosts") else {}, "name": NAME, "report_day": cfg.get("report_day") or "Friday",
            "venues": [{"short": v.get("short") or v.get("name") or "?", "name": v.get("name") or "",
                        "source": v.get("source") or ""} for v in cfg.get("venues") or [] if isinstance(v, dict)]}


def write_startup(cfg, log=print):
    """IC_Paper_Radar/STARTUP.html (Starter: output/STARTUP.html), next to DASHBOARD.html."""
    path = Path(cfg["paths"]["onedrive_root"]) / PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(PAGE + ".tmp")
    tmp.write_text(render(page_data(cfg)), encoding="utf-8")
    os.replace(tmp, path)
    log(f"Control center: {path}")
    return path


def render(data):
    import html_renderer
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=5">
<title>{NAME} — Setup &amp; Control Center</title><style>{html_renderer.CSS}{CSS}</style></head>
<body class="ccp"><header class="bar"><h1>⚙️ Setup &amp; Control Center <span class="muted small">v3.2.0</span></h1>
<span id="mode" class="btn pill" title="">⏳ Connecting…</span>
<span id="dirty" class="dirty" hidden>● unsaved changes</span>
<button class="btn sec" onclick="theme()" title="light/dark">◐</button></header>
<main class="cc">
<div id="updbanner"></div>
<section class="card cockpit" id="c-status"></section>
<section class="card" id="c-console" hidden></section>
<div class="stepbar"><h2>🧩 Setup in 4 steps</h2><span class="muted small">finished steps fold into one line; click a step to open it</span>
<button class="btn small" id="toggleall" onclick="toggleAll()">⊞ Expand All</button></div>
<details class="step-card" id="s1"><summary><span class="snum">1</span><span class="stitle">Step 1 · Basic Profile &amp; Discipline Preset <em>(Required)</em></span>
<span class="sbadge" id="s1b"></span><span class="sarrow">▸</span><span class="ssum" id="s1s"></span></summary>
<div class="sbody"><section id="c-profile"></section><section id="c-preset"></section></div></details>
<details class="step-card" id="s2"><summary><span class="snum">2</span><span class="stitle">Step 2 · Private Shared Repo &amp; Team Sync <em>(Recommended for Teams)</em></span>
<span class="sbadge" id="s2b"></span><span class="sarrow">▸</span><span class="ssum" id="s2s"></span></summary>
<div class="sbody"><section id="c-repo"></section><section id="c-schedule"></section></div></details>
<details class="step-card" id="s3"><summary><span class="snum">3</span><span class="stitle">Step 3 · Research Pillars &amp; 0-Token Keyword Planner <em>(Required)</em></span>
<span class="sbadge" id="s3b"></span><span class="sarrow">▸</span><span class="ssum" id="s3s"></span></summary>
<div class="sbody"><section id="c-plan"></section><section id="c-people"></section>
<details class="fine" id="fine"><summary>🎛️ Fine-tune keywords &amp; weights per pillar</summary><section id="c-themes"></section></details>
<section id="s3act"></section></div></details>
<details class="step-card" id="s4"><summary><span class="snum">4</span><span class="stitle">Step 4 · Topic Deep-Dive &amp; Advanced Settings <em>(Optional)</em></span>
<span class="sbadge" id="s4b"></span><span class="sarrow">▸</span><span class="ssum" id="s4s"></span></summary>
<div class="sbody"><section id="c-topic"></section><section id="c-search"></section><section id="c-advanced"></section><section id="c-deploy"></section></div></details>
<p class="muted foot">Generated <span id="gen"></span>. Secrets (IEEE key, tokens, <code>.env</code>) are never shown or
stored here: only whether they are set.</p>
<footer class="ccfoot"><a href="{REPO_URL}" target="_blank" rel="noopener">⭐ Star on GitHub</a> ·
<a href="{REPO_URL}/blob/main/README.md#call-for-co-maintainers" target="_blank" rel="noopener">🤝 Co-Maintain / Submit a Domain Preset (PR)</a> ·
<a href="#" onclick="citeModal(); return false;">📖 Cite Paper Radar</a></footer>
</main><div class="bottombar"><span id="dirty2" class="dirty" hidden>● unsaved changes</span>
<button class="btn" onclick="save()">💾 Save Config</button>
<button class="btn primary go" data-run="rescore_and_seed" onclick="run('rescore_and_seed')">▶ Run &amp; Update Dashboard</button></div>
<div id="modal" class="modal" hidden></div><div id="toast" class="toast" hidden></div>
<script id="cc-data" type="application/json">{blob}</script>
<script>{JS}</script></body></html>"""


CSS = r"""
main.cc{max-width:1180px}.cc .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
.cc h2{font-size:16px;margin:0 0 10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.cc h3{font-size:14px;margin:14px 0 6px}
.muted{color:var(--muted)}.ok{color:var(--ok)}.warn{color:var(--warn)}.small{font-size:12.5px}.foot{font-size:12.5px;text-align:center}
.dirty{color:var(--warn);font-size:13px;font-weight:600}.btn.small{padding:2px 8px;font-size:12.5px}.btn[disabled]{opacity:.5;cursor:default}
.btn.danger:hover{border-color:var(--warn);color:var(--warn)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px 18px}
.steps{list-style:none;margin:0;padding:0;display:grid;gap:6px}.steps li{display:flex;gap:8px;align-items:baseline}
.steps .ic{width:1.4em;text-align:center;flex:none}.steps .sub{color:var(--muted);font-size:13px}
.prog{height:9px;background:var(--chip);border-radius:99px;overflow:hidden;margin:6px 0 12px}.prog>i{display:block;height:100%;background:var(--ok);transition:width .3s}
.runbar{display:flex;flex-wrap:wrap;gap:8px}.runbar .btn{padding:8px 14px;font-size:14px}
pre.console{background:#0f1115;color:#d8dee9;border-radius:10px;padding:10px 12px;max-height:380px;overflow:auto;margin:0;
font:12.5px/1.45 ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chip{display:inline-flex;align-items:center;gap:2px;background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:1px 3px 1px 10px;font-size:13px;max-width:100%}
.chip .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chip.rx .t{font-style:italic}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;padding:0 4px;font:inherit;font-size:12px;border-radius:99px}
.chip button:hover{color:var(--fg)}.chip button.w{background:var(--card);border:1px solid var(--line);min-width:24px;color:var(--fg);font-weight:600;margin-left:4px}
.chip button.w.neg{color:var(--warn)}.adder{display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap}
.in{padding:4px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;font-size:13px;min-width:0}
.in.w{width:62px}.in.wide{width:100%}.in.mono,textarea.in{font-family:ui-monospace,SFMono-Regular,Consolas,Menlo,monospace;font-size:12.5px}
textarea.in{width:100%;min-height:58px;resize:vertical}
.pillar{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
.pillar .ph{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:8px}.pillar .ph .num{font-weight:700;min-width:1.6em}
.pillar .ph .name{flex:1 1 260px}.pillar .ph .short{width:120px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:5px 14px;font-size:14px;align-items:baseline}.kv>b{font-weight:600}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}.row label{display:inline-flex;gap:6px;align-items:center}
table.mini{border-collapse:collapse;width:100%;background:none}table.mini td,table.mini th{padding:5px 6px;font-size:13.5px;border-bottom:1px solid var(--line)}
.tag{display:inline-block;font-size:11.5px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted)}
.tip{cursor:help;border-bottom:1px dotted var(--muted)}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:10px;z-index:50;font-size:14px;max-width:90vw}
.cmd{display:flex;gap:6px;align-items:flex-start;margin:4px 0}.cmd code{flex:1;white-space:pre-wrap;word-break:break-all;padding:4px 8px;display:block}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.dcard{display:flex;flex-direction:column;gap:4px;align-items:flex-start;text-align:left;border:1px solid var(--line);border-radius:12px;
padding:12px;background:var(--bg);color:var(--fg);font:inherit;cursor:pointer}.dcard:hover{border-color:var(--accent)}
.dcard.on{border:2px solid var(--accent);background:var(--card)}.dcard .di{font-size:24px}div.dcard{cursor:default}
.dmesh{margin-top:12px;border-top:1px dashed var(--line);padding-top:8px}
.bottombar{position:sticky;bottom:0;z-index:30;display:flex;gap:8px;justify-content:flex-end;align-items:center;padding:10px 16px;
background:var(--bg);border-top:1px solid var(--line)}
.wpill{display:inline-block;min-width:2.3em;text-align:center;border-radius:6px;padding:0 6px;font-weight:700;color:#fff}
.w5{background:#c0392b}.w4{background:#d35400}.w3{background:#a67c00}.w2{background:#2e86c1}.w1{background:#7f8c8d}
input.wsel{color:#fff;font-weight:700;border:0}.legendbox{border:1px solid var(--line);border-radius:10px;padding:4px 12px 2px;margin:6px 0 10px}
table.legend td{padding:3px 8px;vertical-align:top}.planrow input.pname{flex:0 1 300px}.planrow input.seeds{flex:1;min-width:220px}
.topictab td{padding:4px 6px;vertical-align:top}.nowrap{white-space:nowrap}.topicres{margin:6px 0;padding:6px 8px;border-left:3px solid var(--ok);background:var(--chip);border-radius:6px}
.mbox.wide{max-width:1000px}.planbody{max-height:64vh;overflow:auto}.plantab td{padding:3px 6px;vertical-align:top}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:60;display:flex;align-items:center;justify-content:center;padding:16px}
.modal[hidden]{display:none}.mbox{background:var(--card);color:var(--fg);border-radius:14px;max-width:620px;width:100%;padding:18px 20px;
border:2px solid var(--warn);box-shadow:0 10px 40px rgba(0,0,0,.3)}
mark{background:var(--citebg);color:inherit;border-radius:3px;padding:0 1px}
.ccfoot{text-align:center;font-size:12.5px;color:var(--muted);padding:6px 0 18px}.ccfoot a{color:var(--muted)}.ccfoot a:hover{color:var(--accent)}
pre.bib{background:var(--chip);border-radius:8px;padding:10px;font-size:12.5px;white-space:pre-wrap;user-select:all}
@media (max-width:700px){.pillar .ph .short{width:90px}.kv{grid-template-columns:1fr}}
/* v3.1.0: larger type and a calmer layout */
body.ccp{font-size:16px;line-height:1.55}.ccp .small,.ccp .foot,.ccp .ccfoot{font-size:14px}.ccp .steps .sub{font-size:14.5px}
.ccp .cc h2{font-size:19px}.ccp .cc h3{font-size:16.5px;margin:16px 0 8px}.ccp .btn{font-size:15px;padding:8px 14px}
.ccp .btn.small{font-size:14px;padding:4px 10px}.ccp .in{font-size:15px;padding:7px 10px}.ccp .in.mono,.ccp textarea.in{font-size:14px}
.ccp .chip{font-size:14.5px;padding:2px 4px 2px 11px}.ccp .chip button{font-size:13.5px}.ccp table.mini td,.ccp table.mini th{font-size:14.5px;padding:6px 7px}
.ccp .kv{font-size:15.5px}.ccp .tag{font-size:13px}.ccp .dirty{font-size:14.5px}.ccp pre.console{font-size:13.5px}
.btn.go{background:#1e8e3e;border-color:#1e8e3e;color:#fff;font-weight:700}.btn.go:hover{filter:brightness(1.08)}
.cc .cockpit{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(250px,1fr);gap:16px 22px;border-width:2px;padding:16px 18px}
.pods{display:grid;gap:10px;align-content:start}.pod{border:1px solid var(--line);border-radius:12px;padding:10px 14px;background:var(--bg)}
.ccp .pod h3{margin:0 0 8px;font-size:16.5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.pod.quiet{opacity:.92}
.pod .prog{margin:8px 0 4px}.pod p{margin:6px 0 0}
.spill{display:inline-flex;gap:6px;align-items:center;border-radius:999px;padding:5px 13px;font:inherit;font-size:15px;font-weight:600;
border:1.5px solid var(--line);background:var(--chip);color:var(--fg);cursor:default;text-align:left}
button.spill{cursor:pointer}button.spill:hover{border-color:var(--accent)}.spill.ok{border-color:var(--ok)}.spill.bad{border-color:var(--warn);color:var(--warn)}
.spill.opt{font-weight:500;font-size:14px;color:var(--muted);padding:3px 11px}.spill.opt.on{color:var(--fg)}
.count{font-size:15px;font-weight:700;border-radius:99px;padding:1px 11px;background:var(--ok);color:#fff}.count.bad{background:var(--warn)}
.qa{display:flex;flex-direction:column;gap:8px}.qah{font-weight:700;font-size:16.5px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ccp .qa .btn{width:100%;text-align:left;margin:0;font-size:15.5px;padding:10px 14px}.ccp .qa .btn.small{font-size:14px;padding:6px 12px}
.stepbar{display:flex;align-items:center;gap:10px 14px;flex-wrap:wrap;margin:24px 0 6px}.ccp .stepbar h2{margin:0;font-size:20px}.stepbar #toggleall{margin-left:auto}
.step-card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:12px 0;scroll-margin-top:72px}
.step-card>summary{list-style:none;cursor:pointer;display:flex;gap:8px 12px;align-items:center;flex-wrap:wrap;padding:14px 18px;border-radius:12px}
.step-card>summary::-webkit-details-marker{display:none}.step-card>summary:hover{background:var(--chip)}
.step-card[open]>summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}
.snum{display:inline-flex;width:32px;height:32px;border-radius:50%;align-items:center;justify-content:center;background:var(--accent);color:#fff;font-weight:700;font-size:16px;flex:none}
.step-card.done .snum{background:var(--ok)}.stitle{font-size:19px;font-weight:700}.stitle em{font-style:normal;font-weight:500;color:var(--muted);font-size:15px}
.sbadge{font-size:14px;font-weight:700;border-radius:99px;padding:2px 11px;border:1.5px solid var(--line);color:var(--muted)}
.sbadge.ok{color:var(--ok);border-color:var(--ok)}.sbadge.need{color:var(--warn);border-color:var(--warn)}
.sarrow{margin-left:auto;color:var(--muted);font-size:18px;transition:transform .2s}.step-card[open]>summary .sarrow{transform:rotate(90deg)}
.ssum{flex-basis:100%;padding-left:44px;color:var(--muted);font-size:15px}.step-card[open]>summary .ssum{display:none}
.sbody{padding:4px 18px 16px}.sbody>section{padding:12px 0 4px;border-top:1px dashed var(--line);scroll-margin-top:72px}
.sbody>section:first-child{border-top:0}.sbody>section:empty{display:none}.ccp .sbody h2{font-size:18px}
details.how{margin:6px 0 8px;font-size:14.5px}details.how>summary{cursor:pointer;color:var(--accent);font-weight:600;list-style:none;width:max-content;max-width:100%}
details.how>summary::-webkit-details-marker{display:none}details.how[open]{background:var(--chip);border-radius:10px;padding:8px 12px}
details.how[open]>summary{margin-bottom:6px}details.how.legendbox{border:0;padding:0}details.how.legendbox[open]{padding:8px 12px}
details.fine{border:1px solid var(--line);border-radius:12px;padding:0 14px;margin:12px 0;scroll-margin-top:72px}
details.fine>summary{cursor:pointer;padding:12px 0;font-weight:700;font-size:16.5px}details.fine[open]>summary{border-bottom:1px dashed var(--line)}
.emailbox{border:2px solid var(--warn);border-radius:12px;padding:12px 14px;margin:10px 0;background:var(--chip)}.emailbox.ok{border-color:var(--ok)}
.emailbox label{display:block;font-weight:700;font-size:16.5px;margin-bottom:6px}.ccp .emailbox .in{font-size:16px;width:min(100%,380px)}
/* v3.1.1: nothing may push the page wider than a phone (long C:\... paths, .sqlite names, commit lines) */
html,body.ccp{max-width:100%;overflow-x:hidden}.ccp *,.ccp *::before,.ccp *::after{box-sizing:border-box}
main.cc,.cc .card,.step-card,.sbody,.sbody>section,details.fine,.pods,.pod,.qa,.emailbox,.legendbox{max-width:100%;min-width:0}
.cc .card,.step-card,.sbody>section,details.fine{overflow-x:hidden}
.ccp .kv{grid-template-columns:max-content minmax(0,1fr)}.grid2{grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr))}
.row>*,.chips>*,.runbar>*,.kv>*,.grid2>*,.dgrid>*,.cockpit>*,.pods>*,.ph>*,.step-card>summary>*,.adder>*{min-width:0}
.ccp code,.ccp .mono,.ccp .tag,.ccp .kv>*,.ccp .ssum,.ccp .small,.ccp .spill,.ccp .dcard,.ccp table.mini td,.ccp table.mini th,.ccp .topicres{
overflow-wrap:anywhere;word-break:break-word}.ccp code,.ccp .tag,.ccp .spill{white-space:normal}
.ccp .in{max-width:100%}.ccp .row label{flex-wrap:wrap;max-width:100%}.ccp table.mini{table-layout:auto;max-width:100%}.ccp .nowrap{white-space:normal}
/* v3.2.0: update banners, Friday schedule */
.updb{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:0 0 12px;padding:12px 16px;border-radius:12px;
border:2px solid var(--accent);background:var(--chip);font-size:16px}.updb.repo{border-color:#d4a72c}.updb b{font-weight:700}
.updb .muted{flex-basis:100%}.slot{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;align-items:center;margin:10px 0;
padding:10px 12px;border:1px solid var(--line);border-radius:10px}.slot>.small{grid-column:1/-1}.slot input[type=time]{width:8.5em}
.daybadge{display:inline-block;padding:2px 10px;border-radius:999px;background:var(--chip);border:1px solid var(--line);font-size:14px}
/* v3.1.2: long text boxes wrap and grow (textarea.auto-grow); labels stack above them (.field) */
.ccp textarea.in.auto-grow{display:block;width:100%;max-width:100%;min-height:0;overflow-y:hidden;overflow-x:hidden;white-space:pre-wrap;
overflow-wrap:anywhere;word-break:break-word;resize:vertical;line-height:1.45}
.ccp textarea.in.auto-grow:not(.mono){font-family:inherit;font-size:15px}
.ccp .field{display:flex;flex-direction:column;align-items:stretch;gap:4px;width:100%;min-width:0;margin:8px 0}
.ccp .field>.flabel{font-size:14.5px;font-weight:600}.ccp .field>.flabel .muted{font-weight:400}
.ccp .field>input.in:not(.w){width:100%}.ccp .field>.row{margin:4px 0 0}
.ccp .adder.long{display:flex;width:100%;align-items:flex-start}.ccp .adder.long>textarea{flex:1 1 260px}
.ccp .planrow{align-items:flex-start}.ccp .planrow>textarea.pname{flex:1 1 240px}.ccp .planrow>textarea.seeds{flex:2 1 300px}
.ccp .ph>textarea.name{flex:1 1 240px;width:auto}.ccp .ph>input.short{flex:0 1 150px}
@media (max-width:560px){.ccp .planrow>textarea.pname,.ccp .planrow>textarea.seeds,.ccp .ph>textarea.name{flex-basis:100%}}
@media (max-width:860px){.cc .cockpit{grid-template-columns:1fr}.ssum{padding-left:0}.stitle{font-size:17px}}
@media (max-width:560px){.ccp .kv{grid-template-columns:minmax(0,1fr)}.ccp .kv>b{margin-top:4px}.ccp main.cc{padding-left:10px;padding-right:10px}
.cc .card,.sbody{padding-left:12px;padding-right:12px}.step-card>summary{padding:12px}}
"""

JS = r"""
const D = JSON.parse(document.getElementById('cc-data').textContent);
const STARTER = D.edition === 'starter';
const clone = x => JSON.parse(JSON.stringify(x));
const $ = (s, el) => (el || document).querySelector(s);
const fmt = n => (n == null ? '–' : Number(n).toLocaleString('en-US'));
let S = clone(D.editable), ST = D.status, API = null, KW = null, BASE = '', JOB = {id: null, next: 0, running: false}, ACT = null;

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'html') e.innerHTML = v;
    else if (k === 'value') e.value = v;
    else if (k === 'checked') e.checked = !!v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(9)) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
function fill(el, ...kids) {  // replaceChildren, but nested arrays flattened and null/false skipped (like h)
  el.replaceChildren(...kids.flat(9).filter(c => c != null && c !== false).map(c => c.nodeType ? c : document.createTextNode(String(c))));
  return el;
}
// v3.1.2: an auto-growing, wrapping text box. o.enter = Enter (no Shift) runs it; o.single = text that replaces newlines.
function ta(attrs, o) {
  o = o || {};
  const e = h('textarea', Object.assign({rows: 1, spellcheck: 'false'}, attrs, {class: ((attrs && attrs.class) || 'in') + ' auto-grow'}));
  if (o.single != null) e.addEventListener('input', () => { if (/[\r\n]/.test(e.value)) { const p = e.selectionStart; e.value = e.value.replace(/\r?\n/g, o.single); e.selectionStart = e.selectionEnd = Math.min(p, e.value.length); } }, true);
  if (o.enter || o.single != null) e.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); if (o.enter) o.enter(); } });
  return e;
}
function grow(el) {
  if (!el.getClientRects().length) return;  // hidden (closed step): sized when it opens
  el.style.height = 'auto'; el.style.height = (el.scrollHeight + 2) + 'px';
}
let GROW_Q = 0;
function growAll() { GROW_Q = 0; document.querySelectorAll('textarea.auto-grow').forEach(grow); }
function growSoon() { if (!GROW_Q) GROW_Q = setTimeout(growAll, 30); }  // a timer, not rAF: rAF pauses in background tabs
document.addEventListener('input', e => { if (e.target.matches && e.target.matches('textarea.auto-grow')) grow(e.target); });
document.addEventListener('toggle', growSoon, true);
window.addEventListener('resize', growSoon);
new MutationObserver(growSoon).observe(document.documentElement, {childList: true, subtree: true});
function theme() {
  const r = document.documentElement;
  const dark = r.dataset.theme ? r.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  r.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('paper_radar_theme', r.dataset.theme); } catch (e) {}
}
try { const t = localStorage.getItem('paper_radar_theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
function toast(msg, ms) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, ms || 3500);
}

// ---------- keywords grouped under their pillar headings (_pillarN_…) ----------
function slug(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40) || 'pillar'; }
function buildKW() {
  const by = {}, markers = {}, extra = [], loose = []; let cur = 0;
  for (const [k, w] of S.positive_keywords) {
    const m = /^_pillar(\d+)/.exec(k);
    if (m) { cur = +m[1]; if (!markers[cur]) markers[cur] = k; by[cur] = by[cur] || []; continue; }
    if (k.startsWith('_')) { extra.push([k, w]); continue; }
    (cur ? (by[cur] = by[cur] || []) : loose).push([k, w]);
  }
  const n = S.pillars.length;
  KW = {extra, loose,
        pillars: S.pillars.map((name, i) => ({name, short: S.pillar_short[i] || '', marker: markers[i + 1] || null, kws: by[i + 1] || []})),
        orphans: Object.keys(by).map(Number).filter(k => k > n || k < 1).sort((a, b) => a - b).map(k => ({n: k, marker: markers[k], kws: by[k]}))};
}
function kwPairs() {
  const out = [...KW.extra, ...KW.loose];
  KW.pillars.forEach((p, i) => {
    const n = i + 1, keep = p.marker && (p.marker === `_pillar${n}` || p.marker.startsWith(`_pillar${n}_`));
    out.push([keep ? p.marker : `_pillar${n}_${slug(p.name)}`, 0], ...p.kws);
  });
  KW.orphans.forEach(o => out.push([o.marker, 0], ...o.kws));
  return out;
}
function collect() {
  const u = {positive_keywords: kwPairs(), negative_keywords: S.negative_keywords, negative_override: S.negative_override,
             key1_themes: S.key1_themes, min_score: S.min_score, vip_authors: S.vip_authors, vip_boost: S.vip_boost,
             anchors: S.anchors, contact_email: S.contact_email, data_repo: dataRepoOut(), data_repo_dir: S.data_repo_dir,
             domain_preset: S.domain_preset, deployment: S.deployment, sync_hosts: S.sync_hosts, schedule: S.schedule};
  u.pillars = KW.pillars.map(p => p.name.trim());  // v3.0.7: both editions (P1–P4 fixed on the Minisforum radar)
  const sh = KW.pillars.map(p => (p.short || '').trim());
  while (sh.length && !sh[sh.length - 1]) sh.pop();
  u.pillar_short = sh;
  if (STARTER) u.arxiv = S.arxiv;
  return u;
}
let baseline = '';
function snapshot() { return JSON.stringify(collect()); }
function changed() { const d = snapshot() !== baseline; $('#dirty').hidden = !d; $('#dirty2').hidden = !d; return d; }
function isDirty() { return snapshot() !== baseline; }

// ---------- a chip list: strings, or [key, weight] pairs ----------
function chips(list, o) {
  o = o || {};
  const box = h('div', {class: 'chips'});
  const draw = (focus) => {
    box.replaceChildren(...list.map((it, i) => {
      const k = o.weights ? it[0] : it, rx = o.weights && k.startsWith('re:');
      const shown = rx ? k.slice(3).split('::')[0] : (o.label ? o.label(k) : k);
      return h('span', {class: 'chip' + (rx ? ' rx' : ''), title: rx ? 'regex: ' + k.split('::').slice(1).join('::') : (o.tip ? o.tip(k) : k)},
        h('span', {class: 't'}, shown),
        o.weights ? h('button', {class: 'w' + (it[1] < 0 ? ' neg' : ''), title: 'Weight (click to change)', onclick: () => {
          const v = prompt(`Weight for “${shown}” (−20 … 20; higher = stronger match):`, it[1]);
          if (v === null) return; const n = Number(v);
          if (v.trim() === '' || !isFinite(n) || n < -20 || n > 20) return alert('Please enter a number from −20 to 20.');
          it[1] = n; draw(); changed(); }}, String(it[1])) : null,
        o.move ? h('button', {title: 'Move to another pillar', onclick: () => o.move(i)}, '⇄') : null,
        h('button', {title: 'Remove', onclick: () => { list.splice(i, 1); draw(); changed(); if (o.after) o.after(); }}, '✕'));
    }), adder());
    if (focus) { const f = box.querySelector('.adder .in'); if (f) f.focus(); }
  };
  const adder = () => {
    const inp = o.long ? ta({class: 'in mono', placeholder: o.placeholder || 'add…'}, {single: '', enter: () => go()})
      : h('input', {class: 'in', placeholder: o.placeholder || 'add…', size: o.size || 22});
    const w = o.weights ? h('input', {class: 'in w', type: 'number', value: o.defw == null ? 3 : o.defw, min: -20, max: 20, step: 1, title: 'weight'}) : null;
    const go = () => {
      let v = inp.value.trim(); if (!v) return;
      if (o.clean) v = o.clean(v);
      const err = o.check && o.check(v); if (err) return alert(err);
      const keys = list.map(x => (o.weights ? x[0] : x).toLowerCase());
      if (keys.includes(v.toLowerCase())) { inp.value = ''; return toast('Already in the list'); }
      if (o.weights) {
        const n = Number(w.value);
        if (w.value === '' || !isFinite(n) || n < -20 || n > 20) return alert('The weight must be a number from −20 to 20.');
        if (o.taken) { const t = o.taken(v); if (t) return alert(t); }
        list.push([v, n]);
      } else list.push(v);
      draw(true); changed(); if (o.after) o.after();
    };
    if (!o.long) inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
    return h('span', {class: 'adder' + (o.long ? ' long' : '')}, inp, w, h('button', {class: 'btn small', onclick: go}, '＋ Add'));
  };
  draw(false);
  return box;
}

// ---------- 1. setup progress ----------
function tasteOn() { return !!(ST.taste && ST.taste.active); }
function repoName(u) { return String(u || '').replace(/^https?:\/\/[^/]+\//, '').replace(/^git@[^:]+:/, '').replace(/\.git$/, ''); }
function tasteText() {
  if (!ST.taste) return '⚪ –';
  return tasteOn() ? `🟢 Trained on ${ST.taste.pos}+ / ${ST.taste.neg}− marks${ST.taste.implicit ? ` + ${ST.taste.implicit} passed-over papers (×${ST.taste.weight})` : ''} (taste:−2…+4 on every score)`
    : `⚪ ${ST.taste.pos}+ / ${ST.taste.neg}− marks: needs ${ST.taste.need} ✅/⭐/📌, and ${ST.taste.need} 🗄️ archived or passed-over reported papers${ST.taste.enabled ? '' : ' (off in scoring.taste)'}`;
}
function spill(cls, text, title, go) { return h(go ? 'button' : 'span', {class: 'spill ' + cls, title: title || '', onclick: go || null}, text); }
function howBox(...kids) { return h('details', {class: 'how'}, h('summary', {}, '💡 How this works'), h('div', {}, kids)); }
function renderStatus() {  // v3.1.0: the top cockpit, 3 status pods + the quick actions
  const st = ST.store, env = ST.env, dr = ST.data_repo, p = ST.pillars;
  const req = [st.papers > 0, !!p.ready, !!env.contact_email_set], done = req.filter(Boolean).length;
  const rr = (dr.read_repos || []).length, np = dr.peers.length, adv = () => openTo('#c-advanced');
  const pods = h('div', {class: 'pods'},
    h('div', {class: 'pod'}, h('h3', {}, '🎯 Required Setup', h('span', {class: 'count' + (done < 3 ? ' bad' : '')}, `${done}/3 Ready`)),
      h('div', {class: 'chips'},
        spill(req[0] ? 'ok' : 'bad', `📚 Paper Store (${fmt(st.papers)} papers)`, req[0] ? `${st.file} · ${fmt(st.candidates)} active candidates · ${fmt(st.external)} external`
          : 'Empty: 🌐 Fetch New Papers Now, or load a shared corpus in Step 2', req[0] ? null : () => openTo('#c-repo')),
        spill(req[1] ? 'ok' : 'bad', `🧭 Pillars (${p.ready ? p.count : 0} active)`, req[1] ? `${fmt(p.keywords)} weighted keywords` : 'Step 3: name your pillars and plan their keywords', () => openTo('#c-plan')),
        spill(req[2] ? 'ok' : 'bad', `✉️ Contact Email (${req[2] ? 'Set' : 'Missing'})`, 'Step 1: Crossref and OpenAlex answer faster with a mailto', () => openTo('#c-profile', '#email'))),
      h('div', {class: 'prog'}, h('i', {style: `width:${Math.round(100 * done / 3)}%`})),
      dr.connected && dr.missing_here ? h('p', {class: 'warn small'}, `⚠️ ${fmt(dr.missing_here)} shared papers not loaded yet: 📥 Read / Pull Shared Repos`) : null,
      !env.has_git ? h('p', {class: 'warn small'}, '⚠️ git is not on PATH: the private data repo needs Git (https://git-scm.com).') : null),
    h('div', {class: 'pod'}, h('h3', {}, '🤝 Shared Data Repo & Peers'),
      h('div', {class: 'chips'}, dr.connected
        ? spill('ok', `🟢 Connected: ${repoName(dr.remote_url) || 'data repo'} · ${np} peer${np === 1 ? '' : 's'}`, `${fmt(dr.papers)} papers in the shared corpus`, () => openTo('#c-repo'))
        : spill('opt', '⚪ Not connected', 'Optional: create or clone a private data repo in Step 2', () => openTo('#c-repo')),
        rr ? spill('opt on', `📚 ${rr} shared repo${rr === 1 ? '' : 's'} to read`, 'data_repo.read_repos', () => openTo('#c-repo')) : null)),
    h('div', {class: 'pod quiet'}, h('h3', {}, '⚙️ Optional Boosters', h('span', {class: 'muted small'}, env.has_claude ? '(Optional)' : '(Optional — 0-Token mode active)')),
      h('div', {class: 'chips'},
        spill('opt' + (env.has_ieee_key ? ' on' : ''), (env.has_ieee_key ? '🟢' : '⚪') + ' IEEE API', env.has_ieee_key ? 'IEEE_API_KEY is set' : 'Not set: Crossref / OpenAlex are used instead', adv),
        spill('opt' + (env.has_claude ? ' on' : ''), (env.has_claude ? '🟢' : '⚪') + ' Claude AI', env.has_claude ? `Ready (${env.claude_command})` : 'No Claude CLI: scoring, dashboard and seeding work; digests and the weekly synthesis are skipped', adv),
        spill('opt' + (tasteOn() ? ' on' : ''), (tasteOn() ? '🟢' : '⚪') + ' Taste Model', tasteText(), adv))));
  const qa = h('div', {class: 'qa'}, h('div', {class: 'qah'}, '⚡ Quick Actions', h('span', {class: 'muted small'}, API ? 'run on this computer; output below' : 'static page: each shows its command')),
    runBtn('rescore_and_seed', null, true, null, 'go'), runBtn('sync_data_repo'), runBtn('pull_data_repo'),
    h('a', {class: 'btn', href: 'DASHBOARD.html'}, '📊 Open Dashboard ↗'),
    h('button', {class: 'btn', onclick: () => save()}, '💾 Save Config'),
    runBtn('daily', null, false, null, 'small'),
    h('button', {class: 'btn small', title: 'Ask GitHub for a new radar version and new commits in your shared repos', onclick: () => checkUpdates(true)}, '🔍 Check Updates'));
  fill($('#c-status'), pods, qa);
  $('#gen').textContent = ST.generated + (API ? ' (live)' : ' (snapshot in this file)');
}

// ---------- v3.1.0: Step 1 · profile, Step 4 · venues / schedule / keys, and the step accordions ----------
function renderProfile() {
  const ok = /^[^@\s"<>]+@[^@\s"<>]+\.[A-Za-z]{2,}$/.test(S.contact_email || '');
  fill($('#c-profile'), h('h2', {}, '👤 Basic Profile'),
    h('div', {class: 'kv'}, h('b', {}, 'Radar'), h('span', {}, D.name || 'Paper Radar', ' ', h('span', {class: 'tag'}, STARTER ? 'Starter Edition' : 'Minisforum radar')),
      h('b', {}, 'Your name'), h('span', {}, (S.data_repo.peer_id || ST.data_repo.me || '–'), h('span', {class: 'muted small'}, '  (peer_id, set in Step 2)'))),
    h('div', {class: 'emailbox' + (ok ? ' ok' : '')}, h('label', {for: 'email'}, '✉️ Contact E-mail ', h('span', {class: ok ? 'ok' : 'warn'}, ok ? '✅' : '(required)')),
      h('input', {class: 'in', id: 'email', type: 'email', value: S.contact_email, placeholder: 'you@example.org',
        oninput: e => { S.contact_email = e.target.value.trim(); changed(); }, onchange: () => renderProfile()}),
      h('div', {class: 'muted small'}, 'Sent only to Crossref / OpenAlex (their “polite pool”: faster answers), never to anyone else.')),
    h('div', {class: 'row'}, h('button', {class: 'btn primary', onclick: () => save()}, '💾 Save Step 1'),
      h('span', {class: 'muted small'}, 'then pick your discipline below (Apply saves it right away)')));
}
function renderAdvanced() {
  const env = ST.env, V = D.venues || [];
  fill($('#c-advanced'), h('h2', {}, '📚 Venues, Schedule & Optional API Keys'),
    h('h3', {}, `Venues (${V.length})`),
    V.length ? h('div', {class: 'chips'}, V.map(v => h('span', {class: 'chip', title: v.name + (v.source ? ' · source: ' + v.source : '')}, h('span', {class: 't'}, '☑ ' + v.short))))
      : h('p', {class: 'muted small'}, 'No venues configured.'),
    h('p', {class: 'muted small'}, 'A 🧭 domain preset (Step 1) can replace this list; for single venues edit "venues" in config.json.'),
    h('h3', {}, '🗓️ Schedule'),
    h('p', {class: 'small'}, `Weekly report day: ${D.report_day || 'Friday'}. `, 'Friday times: ⏰ Friday Schedule in Step 2.'),
    h('h3', {}, '🔑 Optional boosters'),
    h('div', {class: 'kv'}, h('b', {}, 'IEEE API'), h('span', {}, env.has_ieee_key ? '🟢 IEEE_API_KEY is set' : '⚪ not set (optional): set the IEEE_API_KEY environment variable; README → “Free IEEE API key”'),
      h('b', {}, 'Claude AI'), h('span', {}, env.has_claude ? `🟢 Ready (${env.claude_command})` : '⚪ not found: 0-token mode (scores, dashboard, seeding and topic tables all work)'),
      h('b', {}, 'Taste model'), h('span', {}, tasteText())),
    h('p', {class: 'muted small'}, 'Keys are never shown or stored on this page: only whether they are set.'));
}
let AUTO = true;  // smart auto-collapse until the user opens or closes a step, or the live data has arrived
function stepInfo() {
  const env = ST.env, dr = ST.data_repo, p = ST.pillars, T = ST.topics || {dives: []}, rr = (dr.read_repos || []).length;
  const pr = (D.presets || []).find(x => x.key === S.domain_preset);
  return [
    {id: 's1', req: true, done: !!env.contact_email_set, sum: `${pr ? pr.icon + ' ' + pr.label : 'no preset'} · ✉️ ${env.contact_email_set ? 'e-mail set' : 'e-mail missing'}`},
    {id: 's2', req: false, done: !!dr.connected || rr > 0, sum: dr.connected ? `🟢 ${repoName(dr.remote_url) || 'data repo'} · ${dr.peers.length} peer(s)` + (rr ? ` · ${rr} shared repo(s) to read` : '')
      : (rr ? `📚 ${rr} shared repo(s) to read · no primary repo` : '⚪ Not connected: solo use works fine')},
    {id: 's3', req: true, done: !!p.ready, sum: p.ready ? `${p.count} pillars · ${fmt(p.keywords)} weighted keywords · ${S.anchors.length} anchors · ${S.vip_authors.length} VIP authors` : 'name your pillars and plan their keywords'},
    {id: 's4', req: false, opt: true, done: false, sum: `🔎 ${T.dives.length} topic dive(s) · 📚 ${(D.venues || []).length} venues · ${S.deployment === 'mesh_lan' ? '🌐 Mesh LAN' : '💻 Single PC'}`}];
}
function renderSteps() {
  const fresh = !(ST.store.papers > 0) && !ST.pillars.ready;
  for (const x of stepInfo()) {
    const el = $('#' + x.id), b = $('#' + x.id + 'b'); if (!el) continue;
    el.classList.toggle('done', x.done);
    b.className = 'sbadge' + (x.done ? ' ok' : (x.req ? ' need' : ''));
    b.textContent = x.done ? '✅ Complete' : (x.req ? '⚠️ Needs setup' : (x.opt ? '⚪ Optional' : '⚪ Not set up'));
    $('#' + x.id + 's').textContent = x.sum;
    if (AUTO) el.open = x.opt ? false : (fresh ? true : (x.req && !x.done));
  }
  fill($('#s3act'), h('div', {class: 'runbar'}, h('button', {class: 'btn', onclick: () => save()}, '💾 Save Config'),
    runBtn('rescore_and_seed', null, true, '💾 ▶ Save, Rescore & Update Dashboard', 'go'),
    h('span', {class: 'muted small'}, 'saves, re-scores every paper, tops up each pillar, rebuilds DASHBOARD.html (0 tokens)')));
  updToggle();
}
function openTo(sel, focus) {
  const e = $(sel); if (!e) return;
  for (let d = e.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) d.open = true;
  AUTO = false; updToggle();
  e.scrollIntoView({behavior: 'smooth', block: 'start'});
  if (focus) { const f = $(focus); if (f) setTimeout(() => f.focus({preventScroll: true}), 350); }
}
function updToggle() {
  const b = $('#toggleall'); if (b) b.textContent = [...document.querySelectorAll('.step-card')].every(d => d.open) ? '⊟ Collapse All' : '⊞ Expand All';
}
function toggleAll() {
  const all = [...document.querySelectorAll('.step-card')], open = !all.every(d => d.open);
  all.forEach(d => { d.open = open; }); AUTO = false; updToggle();
}
document.querySelectorAll('.step-card').forEach(d => d.addEventListener('toggle', updToggle));
document.querySelectorAll('.step-card>summary').forEach(x => x.addEventListener('click', () => { AUTO = false; }));

// ---------- run bar + console ----------
const RUN_TIPS = {
  rescore_and_seed: 'Saves your settings, re-scores every paper with your keywords/authors (--rescore), tops up each pillar to 10 candidates (--seed-pillars 5 --no-claude) and rebuilds DASHBOARD.html. 0 tokens, no API calls except a small Beyond-IEEE search.',
  daily: 'One collection pass now (IEEE / Crossref / OpenAlex) and this week\'s report' + (ST.env.has_claude ? ' with digests (claude -p).' : ' (--no-claude: no Claude CLI found).') + ' On the report day it also finalizes the week.',
  pull_data_repo: 'Read-only: fetch origin/main of ic-paper-radar-data, import every peer overlay, merge new/enriched papers into your store, adopt the external ones ≥ min_score, rebuild the dashboards. Nothing is pushed.',
  sync_data_repo: '--sync (offline Save-Marks files) then --sync-data-repo: pull, merge, export the clean union, write peers/<you>.json and push a real gain to your private data repo; then read the shared repos.',
  init_data_repo: '--init-data-repo: create (gh) or clone your private repo into paths.data_repo_dir, write the protocol files, your clean corpus and your overlay, push main, and set data_repo.remote_url.',
  topic_recall: '--recall: search the whole local store (archived papers and the corpus included) for this topic. 0 API calls, 0 tokens; writes Topic_Dives/<date>_recall_<topic>.html.',
  topic_dive: '--topic: local store + OpenAlex (+ IEEE reserve / PubMed), 6 classics + 8 recent papers, Claude synthesis (or 0-token tables), lineage map, and the dashboard\'s 🔎 Topic Dives.',
  drop_topic: '--drop-topic: remove a dive from 🔎 Topic Dives.'};
function runBtn(a, extra, primary, label, cls) {
  return h('button', {class: 'btn' + (primary ? ' primary' : '') + (cls ? ' ' + cls : ''), title: RUN_TIPS[a], 'data-run': a,
    onclick: () => run(a, extra)}, label || D.actions[a]);
}
function lockRun() { document.querySelectorAll('[data-run]').forEach(b => { b.disabled = !!(API && (JOB.running || ACT)); }); }
// a run this page didn't start (scheduled task, terminal, another window): stream .run.log until it ends
function streamActive(a) {
  ACT = {id: a.id, next: 0};
  showConsole(`⏳ Already running: ${a.what}`, ['Another radar run holds the lock, so nothing new was started. Its output:']);
  lockRun(); pollActive();
}
async function pollActive() {
  if (!ACT) return;
  let r;
  try { r = await call('GET', '/api/active?since=' + ACT.next); } catch (e) { setTimeout(pollActive, 3000); return; }
  const pre = $('#console');
  if (r.active && r.id !== ACT.id) { ACT = {id: r.id, next: 0}; if (pre) pre.textContent = ''; return pollActive(); }
  if (pre && r.lines.length) {
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += (pre.textContent ? '\n' : '') + r.lines.join('\n');
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  }
  ACT.next = r.next;
  if (r.active) return setTimeout(pollActive, 1500);
  ACT = null; lockRun();
  const t = $('#jobtitle'); if (t) t.textContent = '✅ The other run finished: the buttons work again';
  refresh(true);
}
function showConsole(title, lines, cmds) {
  const c = $('#c-console'); c.hidden = false;
  const pre = h('pre', {class: 'console', id: 'console'}, (lines || []).join('\n'));
  fill(c, h('h2', {}, h('span', {id: 'jobtitle'}, title), h('button', {class: 'btn small', onclick: () => { c.hidden = true; }}, 'Hide')),
    cmds ? h('div', {}, cmds) : null, cmds ? null : pre);
  c.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
function cmdRow(text) {
  return h('div', {class: 'cmd'}, h('code', {}, text), h('button', {class: 'btn small', onclick: () => {
    navigator.clipboard && navigator.clipboard.writeText(text).then(() => toast('Copied'), () => toast('Copy failed: select the text'));
  }}, '📋 Copy'));
}
function showCommands(a, extra) {
  const isWin = /Win/.test(navigator.platform || navigator.userAgent);
  const cd = isWin ? `cd /d "${D.here}"` : `cd "${D.here}"`;
  const lines = [];
  if (extra && extra.clone_url) lines.push(`git clone ${extra.clone_url} "${extra.clone_dir || ST.data_repo.default_dir || '../ic-paper-radar-data'}"`);
  lines.push(...D.commands[a]);
  showConsole(`${D.actions[a]}: run these commands`, null, [
    h('p', {class: 'small'}, isDirty() ? '⚠️ First click 💾 Save Config and put the file in the radar folder (replace config.json there). ' : '',
      STARTER ? h('span', {}, 'Easiest: double-click ', h('b', {}, isWin ? 'Start_Radar_GUI.cmd' : 'Start_Radar_GUI.command'),
        ' in the radar folder: it opens this page with working buttons. Or, in a terminal:')
        : 'This page could not reach radar_sync_server.py (PaperRadar_SyncServer). Start it, open this page from the server, or run on the Minisforum:'),
    cmdRow(cd), lines.map(cmdRow)]);
}
async function call(method, path, body) {
  const hd = {};
  if (body) hd['Content-Type'] = 'application/json';
  if (API === location.origin) hd['X-Radar-GUI'] = '1';
  if (D.sync && D.sync.token) hd['X-Radar-Token'] = D.sync.token;
  const r = await fetch(API + path, {method, headers: hd, body: body ? JSON.stringify(body) : undefined,
                                     credentials: 'same-origin', cache: 'no-store'});
  let j = {}; try { j = await r.json(); } catch (e) { j = {ok: false, error: `HTTP ${r.status}`}; }
  if (!r.ok && j.ok !== false) j.ok = false;
  return j;
}
async function run(a, extra) {
  if (!API) return showCommands(a, extra);
  if (JOB.running || ACT) return toast('Another radar run is still running');
  if (a === 'sync_data_repo' && !confirm('Full sync pushes your clean corpus' + (S.data_repo.share_tags !== false ? ' and your overlay (peers/' + (ST.data_repo.me || 'you') + '.json)' : ' (tag sharing is off: no peers/' + (ST.data_repo.me || 'you') + '.json)') + ' to your shared repo. Continue?')) return;
  if (a === 'self_update' && !confirm('Update the radar code from GitHub now? config.json, your paper store and output/ are not touched.')) return;
  const body = Object.assign({action: a}, extra || {});
  const dirty = isDirty();
  if (dirty) body.config = collect();
  let r;
  try { r = await call('POST', '/api/run', body); } catch (e) { return alert('The radar server did not answer: ' + e); }
  if (!r.ok && r.already_running) {  // never two runs at once: show the one that is running
    if (r.active) return streamActive(r.active);
    if (r.job) { JOB = {id: r.job.id, next: 0, running: true}; showConsole(`⏳ Already running: ${r.job.label}`, []); lockRun(); return poll(); }
  }
  if (!r.ok) return alert(r.error || 'Failed');
  if (dirty) { baseline = snapshot(); changed(); }
  JOB = {id: r.job.id, next: 0, running: true};
  showConsole(`⏳ ${D.actions[a]}…`, []);
  lockRun(); poll();
}
async function poll() {
  let r;
  try { r = await call('GET', '/api/job?since=' + JOB.next); } catch (e) { setTimeout(poll, 3000); return; }
  if (r.id !== JOB.id) { JOB.id = r.id; JOB.next = 0; return poll(); }
  const pre = $('#console');
  if (pre && r.lines.length) {
    const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += (pre.textContent ? '\n' : '') + r.lines.join('\n');
    if (atEnd) pre.scrollTop = pre.scrollHeight;
  }
  JOB.next = r.next; JOB.running = r.running; JOB.rc = r.rc; JOB.action = r.action; lockRun();
  if (r.running) return setTimeout(poll, 1000);
  const t = $('#jobtitle'); if (t) t.textContent = (r.rc ? '❌ ' : '✅ ') + (r.label || '') + (r.rc ? ` failed (exit ${r.rc})` : ' finished');
  if (r.action === 'self_update' && !r.rc) { toast('⬆️ Updated: reloading…', 3000); return setTimeout(() => location.reload(), 1500); }
  refresh(true);
  if (['pull_data_repo', 'sync_data_repo', 'clone_data_repo', 'init_data_repo'].includes(r.action)) checkUpdates(false);
}
async function refresh(quiet) {
  if (!API) return;
  const j = await call('GET', '/api/config');
  if (!j.ok) return quiet ? null : toast(j.error || 'Could not reload');
  ST = j.status;
  if (!isDirty()) { S = j.editable; buildKW(); }
  renderAll(); if (!isDirty()) { baseline = snapshot(); changed(); }
}

// ---------- 2. themes & keywords ----------
function movePrompt(list, i) {
  const names = KW.pillars.map((p, n) => `${n + 1} = ${p.name || '(unnamed)'}`).join('\n');
  const v = prompt(`Move “${list[i][0]}” to pillar number (0 = not under a pillar):\n${names}`);
  if (v === null) return; const n = parseInt(v, 10);
  if (!(n >= 0 && n <= KW.pillars.length)) return alert('No such pillar');
  const [it] = list.splice(i, 1);
  (n ? KW.pillars[n - 1].kws : KW.loose).push(it); renderThemes(); changed();
}
function allKeys() { return [KW.loose, ...KW.pillars.map(p => p.kws), ...KW.orphans.map(o => o.kws)].flat().map(x => x[0]); }
function kwOpts(list) {
  return {weights: true, placeholder: 'keyword or re:label::regex', size: 24, move: i => movePrompt(list, i),
    taken: v => allKeys().includes(v) ? `“${v}” is already under another pillar: a keyword belongs to one pillar.` : null,
    check: v => v.startsWith('re:') && !/^re:[^:]+::.+/.test(v) ? 'A regex keyword looks like re:<label>::<regex>' : null,
    after: () => renderPillarCounts()};
}
function renderPillarCounts() { document.querySelectorAll('[data-pcount]').forEach(e => { const k = +e.dataset.pcount; e.textContent = `${(k ? KW.pillars[k - 1].kws : KW.loose).length} keywords`; }); }
function renderThemes() {
  const FIX = D.fixed_pillars || 0;
  const ps = KW.pillars.map((p, i) => {
    const head = i >= FIX
      ? [h('span', {class: 'num'}, `P${i + 1}`),
         ta({class: 'in name', value: p.name, placeholder: 'Pillar name, e.g. Precision SAR ADCs', oninput: e => { p.name = e.target.value; changed(); }}, {single: ' '}),
         h('input', {class: 'in short', value: p.short, maxlength: 24, placeholder: 'short label', title: '1–2 words for phones and the Pillar column (optional)', oninput: e => { p.short = e.target.value; changed(); }}),
         h('button', {class: 'btn small', title: 'Move up', disabled: i === FIX, onclick: () => { KW.pillars.splice(i - 1, 0, ...KW.pillars.splice(i, 1)); renderThemes(); changed(); }}, '↑'),
         h('button', {class: 'btn small', title: 'Move down', disabled: i === KW.pillars.length - 1, onclick: () => { KW.pillars.splice(i + 1, 0, ...KW.pillars.splice(i, 1)); renderThemes(); changed(); }}, '↓'),
         savedPillar(i) ? h('button', {class: 'btn small', 'data-run': 'retire_pillar', title: '🏺 Retire as Antique: its papers keep a 🏺 badge, marks, notes and digests; its keywords leave config.json (saved in _system/retired_pillars_config.json); pillars after it move up one number',
                                       onclick: () => retirePillar(i)}, '🏺 Retire')
           : h('button', {class: 'btn small danger', title: 'Remove this new (unsaved) pillar', onclick: () => {
           if (!confirm(`Remove the new pillar ${i + 1} “${p.name}” and its ${p.kws.length} keywords?`)) return;
           KW.pillars.splice(i, 1); renderThemes(); changed(); }}, '✕')]
      : [h('span', {class: 'num'}, `P${i + 1}`), h('b', {class: 'name'}, p.name.replace(/^Pillar\s*\d+\s*[—–-]\s*/, '')), p.short ? h('span', {class: 'tag'}, p.short) : null];
    return h('div', {class: 'pillar'}, h('div', {class: 'ph'}, head, h('span', {class: 'muted small', 'data-pcount': i + 1})), chips(p.kws, kwOpts(p.kws)));
  });
  const loose = (KW.loose.length || !KW.pillars.length) ? h('div', {class: 'pillar'}, h('div', {class: 'ph'}, h('b', {}, 'Not under a pillar'),
      h('span', {class: 'muted small'}, '(they score, but don\'t pick a pillar)'), h('span', {class: 'muted small', 'data-pcount': 0})), chips(KW.loose, kwOpts(KW.loose))) : null;
  const orph = KW.orphans.map(o => h('div', {class: 'pillar'}, h('div', {class: 'ph'}, h('b', {}, `Heading _pillar${o.n}_ (no pillar ${o.n})`),
      h('span', {class: 'muted small'}, 'kept as they are; add a pillar or move them')), chips(o.kws, kwOpts(o.kws))));
  fill($('#c-themes'), 
    h('h2', {}, '🎯 My Research Themes & Keywords'),
    h('p', {class: 'muted small'}, 'Click a weight to change it; ⇄ moves a keyword to another pillar.'),
    howBox('A paper\'s score is the sum of the weights of the keywords in its title, abstract and index terms (word match, case-insensitive unless ALL CAPS; a space or hyphen matches either; plurals match). The pillar with the most hits files it. Weights: 5 = defining term, 3–4 = strong, 1–2 = generic. ',
      h('span', {class: 'tip', title: 're:<label>::<regex> is a raw (Python) regular expression, case-sensitive unless it starts with (?i). The label is what the report shows.'}, 'Regex keywords?')),
    FIX ? h('p', {class: 'muted small'}, `Pillars 1–${FIX} are fixed on this radar (catalog.py; their cached digests name them); their keywords are editable. Add new pillars after them.`) : null,
    h('div', {class: 'row'}, h('button', {class: 'btn primary', disabled: KW.pillars.length >= D.max_pillars, onclick: () => addPillarWizard()}, '＋ Add New Pillar (Guided)'),
      h('span', {class: 'muted small'}, 'name, anchors and seed words → 🪄 planned keywords for the new pillar only → its first 10 candidates')),
    ps, loose, orph, antiquePillars(),
    true ? h('div', {class: 'row'}, h('button', {class: 'btn', disabled: KW.pillars.length >= D.max_pillars, onclick: () => {
      KW.pillars.push({name: '', short: '', marker: null, kws: []}); renderThemes(); changed();
      const ins = document.querySelectorAll('.pillar .name'); if (ins.length) ins[ins.length - 1].focus(); }}, '＋ Add pillar'),
      h('span', {class: 'muted small'}, `Up to ${D.max_pillars}. Reordering renumbers the pillars; papers you pinned to a pillar number keep that number.`)) : null,
    h('div', {class: 'grid2'},
      h('div', {}, h('h3', {}, '🚫 Negative keywords ', h('span', {class: 'tip muted small', title: 'Off-topic terms. A negative keyword zeroes a paper with no positive keyword hit; with a hit (or a title matching the override) it only costs negative_penalty.'}, 'ⓘ')),
        chips(S.negative_keywords, {placeholder: 'e.g. phased array'})),
      h('div', {}, h('h3', {}, '🌍 Beyond-IEEE themes ', h('span', {class: 'tip muted small', title: 'beyond_ieee.key1_themes: application themes of your research (e.g. wearable, implantable, 6G). Empty = the Nature/Science board stays off.'}, 'ⓘ')),
        chips(S.key1_themes, {placeholder: 'e.g. wearable'}))),
    h('h3', {}, 'Negative override (regex on the title) ', h('span', {class: 'tip muted small', title: 'When the title matches, a negative keyword only costs negative_penalty instead of zeroing the paper. Python regex; (?i) = case-insensitive. Empty = off.'}, 'ⓘ')),
    ta({class: 'in wide mono', value: S.negative_override, placeholder: '(?i)harvest|temperature sensor|…', oninput: e => { S.negative_override = e.target.value; changed(); }}, {single: ''}),
    h('div', {class: 'row'}, h('label', {}, 'min_score ', h('input', {class: 'in w', type: 'number', min: 0, max: 1000, step: 1, value: S.min_score,
      oninput: e => { S.min_score = Number(e.target.value); changed(); }})), h('span', {class: 'muted small'}, 'papers below this score never become candidates (default 5).')));
  renderPillarCounts();
  renderPlan();
}

// ---------- 🔎 Topic Deep-Dive & Local Recall (v3.0.6, topic.py) ----------
let TQ = (() => { try { return localStorage.getItem('paper_radar_topic_q') || ''; } catch (e) { return ''; } })(), TZERO = false, TLAST = null;
const topicSlug = q => q.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60);
const pyCmd = () => ((D.commands.daily || [])[0] || 'python briefing.py').split(' briefing.py')[0];
function topicStatic(lines) {
  const isWin = /Win/.test(navigator.platform || navigator.userAgent);
  showConsole('Run this in a terminal', null, [h('p', {class: 'small'}, 'This page was opened as a file: start ',
    h('b', {}, STARTER ? (isWin ? 'Start_Radar_GUI.cmd' : 'Start_Radar_GUI.command') : 'radar_sync_server.py'),
    ' for one-click runs, or run in the radar folder:'), cmdRow(isWin ? `cd /d "${D.here}"` : `cd "${D.here}"`), lines.map(cmdRow)]);
}
function topicRun(a) {
  const q = (TQ || '').replace(/\s+/g, ' ').trim();
  if (q.length < 3) return alert('Type a topic or research question first.');
  const zero = TZERO || !ST.env.has_claude;
  TLAST = {a, q, slug: topicSlug(q)};
  if (!API) return topicStatic([`${pyCmd()} briefing.py ${a === 'topic_recall' ? '--recall' : '--topic'} "${q.replace(/"/g, "'")}"` +
                                (a === 'topic_dive' && zero ? ' --no-claude' : '')]);
  run(a, {query: q, zero});
}
function dropTopic(t) {
  if (!API) return topicStatic([`${pyCmd()} briefing.py --drop-topic ${t.slug}`]);
  if (!confirm(`Delete the topic dive “${t.query}” (${t.date}) permanently? Its papers lose their 🔎 / 🏺 badge (marks, notes and report files stay). 🏺 Retire keeps everything.`)) return;
  run('drop_topic', {slug: t.slug});
}
function retireTopic(t, back) {  // v3.0.8: 🏺 Retire as Antique / ↺ Restore (non-destructive)
  const a = back ? 'unretire_topic' : 'retire_topic';
  if (!API) return topicStatic([`${pyCmd()} briefing.py --${back ? 'unretire' : 'retire'}-topic ${t.slug}`]);
  run(a, {slug: t.slug});
}
function seedWords(q) {
  return q.split(/[,;]|\b(?:and|focusing on|focused on|with|for|in|of|on|using|via|towards?)\b/i)
    .map(s => s.replace(/\s+/g, ' ').trim()).filter(s => s.length > 2).join(', ');
}
let TPT = 0;
function topicPreview() {  // v3.0.7: the concept groups topic.py will search for (AND of ORs), as you type
  clearTimeout(TPT);
  TPT = setTimeout(async () => {
    const el = $('#topicgroups'); if (!el || !API) return;
    const q = (TQ || '').trim(); if (q.length < 3) { el.textContent = ''; return; }
    let r; try { r = await call('GET', '/api/topic_parse?q=' + encodeURIComponent(q)); } catch (e) { return; }
    if (!r.ok) return;
    el.textContent = r.groups.length ? 'Concept groups (a paper needs all but one): ' + r.groups.map(g => '(' + g.join(' | ') + ')').join(' AND ') +
      (r.limit_nw ? ` · power ≤ ${r.limit_nw} nW boosts relevance` : '') : 'No concept words found: add a few specific terms.';
  }, 350);
}
function promoteDive(t) {
  const name = t.query.length > 60 ? t.query.slice(0, 59).replace(/\s+\S*$/, '') + '…' : t.query;
  addPillarWizard({name: name.charAt(0).toUpperCase() + name.slice(1), seeds: seedWords(t.query), anchors: t.classics.slice(0, 6)});
}

// ---------- 🏺 Retire a pillar (v3.0.8, retire.py): a pillar saved in config.json, nothing else unsaved ----------
function savedPillar(i) { return i < S.pillars.length && (KW.pillars[i].name || '').trim() === (S.pillars[i] || '').trim(); }
function retirePillar(i) {
  const p = KW.pillars[i], n = i + 1;
  if (isDirty()) return alert('Save (💾) or reload your other changes first: retiring renumbers the pillars after this one.');
  if (!API) return showConsole('Run this in a terminal', null, [cmdRow(`${pyCmd()} briefing.py --retire-pillar ${n}`)]);
  if (!confirm(`🏺 Retire pillar ${n} “${p.name}” as an antique?\n\n• Its papers keep a 🏺 badge with their marks, ⭐, notes and digests (dashboard → 📅 Weekly Brief → 🏺 Antiques).\n` +
               `• Its unreviewed candidates go back to the corpus; its ${p.kws.length} keywords leave config.json (a copy is kept in _system/retired_pillars_config.json).\n` +
               `• Pillars after it move up one number. A new pillar ${n} later never mixes with these papers.`)) return;
  run('retire_pillar', {pillar: n});
}
function antiquePillars() {
  const A = (ST.topics || {}).antique_pillars || [];
  return A.length ? h('div', {class: 'pillar muted'}, h('div', {class: 'ph'}, h('b', {}, `🏺 Antique pillars (${A.length})`),
      h('span', {class: 'muted small'}, 'retired: their papers keep a 🏺 badge; browse them in the dashboard\'s 📅 Weekly Brief → 🏺 Antiques')),
    h('div', {class: 'chips'}, A.map(a => h('span', {class: 'chip', title: `${a.name} · was P${a.id} · retired ${a.retired_at}`}, `🏺 ${a.key} · ${a.papers} papers`)))) : null;
}

// ---------- ＋ Add New Pillar (Guided), v3.0.7: one new pillar P<N+1>; P1..P<N> stay byte-for-byte as they are ----------
let PLAST = null;  // {n, name} of the pillar the last wizard created (result line in 🪄 Quick Keyword Setup)
function pillarResult() {
  const done = !JOB.running && JOB.action === 'add_pillar';
  return h('div', {class: 'row topicres'}, h('span', {class: 'small'}, done ? (JOB.rc ? `❌ P${PLAST.n} · ${PLAST.name}: the run failed (see the console)` : `✅ P${PLAST.n} · ${PLAST.name} is ready: re-scored and seeded`) : `⏳ P${PLAST.n} · ${PLAST.name}: re-scoring and seeding…`),
    done && !JOB.rc ? h('a', {class: 'btn small primary', href: `DASHBOARD.html#pillar${PLAST.n}`, target: '_blank'}, `📊 Open DASHBOARD → 📅 Weekly Brief → P${PLAST.n}`) : null);
}
function addPillarWizard(pre) {
  pre = pre || {};
  if (KW.pillars.length >= D.max_pillars) return alert(`You already have ${D.max_pillars} pillars: remove one first.`);
  const n = KW.pillars.length + 1;
  const nameIn = ta({class: 'in wide', value: pre.name || '', placeholder: 'Full pillar name, e.g. SAR ADCs for Body-Sensor Networks'}, {single: ' '});
  const shortIn = h('input', {class: 'in short', maxlength: 24, value: pre.short || '', placeholder: 'short label (optional)', title: `Shown as P${n} · <label> on phones and in the Pillar column`});
  const anchIn = ta({class: 'in wide mono', rows: 2, value: (pre.anchors || []).join('\n'), placeholder: '1–6 anchor DOIs (optional): one per line, or comma-separated. Papers that define this pillar'});
  const seedIn = ta({class: 'in wide', value: pre.seeds || '', placeholder: 'a few rough seed words, comma-separated or one per line: SAR ADC, body sensor network, energy-efficient'});
  const out = h('div', {class: 'planbody'});
  const owned = new Map(); KW.pillars.forEach((p, i) => p.kws.forEach(([k]) => owned.set(k.toLowerCase(), i + 1))); KW.loose.forEach(([k]) => owned.set(k.toLowerCase(), 0));
  let plan = null, picks = [], vipPicks = [], ov = null;
  const dois = () => anchIn.value.split(/[\s,;]+/).map(cleanDoi).filter(Boolean);
  const step3 = r => {
    plan = r; picks = []; vipPicks = [];
    const pl = r.pillars[0], rows = pl.keywords.map(x => {
      const own = owned.get(x.k.toLowerCase());
      const box = h('input', {type: 'checkbox'}); box.checked = own === undefined; box.disabled = own !== undefined;
      const w = h('input', {class: 'in w wsel w' + x.w, type: 'number', min: -20, max: 20, step: 1, value: x.w,
                           oninput: e => { e.target.className = 'in w wsel w' + Math.max(1, Math.min(5, Math.round(Number(e.target.value) || 1))); }});
      if (own === undefined) picks.push({x, box, w});
      const rx = x.k.startsWith('re:');
      return h('tr', {class: own !== undefined ? 'muted' : ''}, h('td', {}, box), h('td', {}, w),
        h('td', {}, rx ? h('span', {title: x.k}, h('code', {}, x.k.slice(3).split('::')[0]), h('span', {class: 'muted small'}, ' unit regex' + (x.what ? ' · ' + x.what : '')))
                       : h('b', {}, x.k), x.kind === 'seed' ? h('span', {class: 'tag'}, 'your seed') : null,
          own !== undefined ? h('span', {class: 'tag'}, own ? `owned by P${own}: left as it is` : 'already a keyword: left as it is') : null),
        h('td', {class: 'small muted'}, `${x.why} · ${x.s}/${x.n_s} seed papers · ${fmt(x.b)}/${fmt(x.store)} in store`));
    });
    const no = r.negative_override || {};
    const ovBox = h('input', {type: 'checkbox'}); ovBox.checked = !!no.suggest && no.merged !== no.current;
    const ovIn = ta({class: 'in wide mono', value: no.merged || ''}, {single: ''}); ov = {ovBox, ovIn};
    const vips = (r.vip_authors || []).filter(v => !S.vip_authors.some(x => x.toLowerCase() === v.name.toLowerCase())).map(v => {
      const box = h('input', {type: 'checkbox'}); box.checked = true; vipPicks.push({v, box});
      return h('label', {class: 'chip', title: `${v.seed_papers} of their ${v.store} papers in your store are seed papers`}, box, ` ${v.name} `, h('span', {class: 'muted small'}, v.store ? `(${v.seed_papers}/${v.store})` : `(${v.score})`)); });
    const missing = (r.anchors || []).filter(a => a.source === 'missing');
    fill(out, h('h3', {}, `Step 3 · Review P${n}'s keywords, then create it`),
      h('p', {class: 'small muted'}, `${pl.seed_papers} seed papers (${pl.anchors.length} anchors) · ${fmt(r.store_papers)} papers in your store. Keywords of P1–P${n - 1} are never changed: ones they already own are greyed out.`),
      r.calibration ? h('p', {class: 'small ' + (r.calibration.added > 0.12 * r.store_papers ? 'warn' : 'ok')}, `📏 With these weights the new pillar adds ≈ ${fmt(r.calibration.added || 0)} papers at the threshold (your current keywords pass ${fmt(r.calibration.base || 0)}), covering ${Math.round(r.calibration.seed_cover * 100)} % of its seed papers.`) : null,
      missing.length ? h('p', {class: 'small warn'}, `Not found (neither in your store nor on OpenAlex): ${missing.map(a => a.doi).join(', ')}`) : null,
      pl.examples.length ? h('details', {class: 'small muted'}, h('summary', {}, 'seed papers it learned from'), h('ul', {}, pl.examples.map(t => h('li', {}, t)))) : null,
      rows.length ? h('table', {class: 'mini plantab'}, h('tbody', {}, rows)) : h('p', {class: 'warn small'}, 'No suggestions: add seed words or anchors.'),
      h('h3', {}, '🛡️ negative_override additions'), h('label', {class: 'row'}, ovBox, ovIn),
      h('h3', {}, '👤 Specialist VIP authors'), vips.length ? h('div', {class: 'chips'}, vips) : h('p', {class: 'muted small'}, 'none'));
    $('#wizgo').hidden = false;
  };
  const planIt = async () => {
    const name = nameIn.value.replace(/\s+/g, ' ').trim();
    if (!name) return alert('Step 1: give the new pillar a name.');
    if (KW.pillars.some(p => p.name.trim().toLowerCase() === name.toLowerCase())) return alert('A pillar with that name already exists.');
    const an = dois(), bad = an.filter(d => !/^10\.\d{4,9}\/\S+$/.test(d));
    if (bad.length) return alert('Not a DOI: ' + bad.join(', '));
    if (an.length > 6) return alert('At most 6 anchor DOIs here.');
    const seeds = seedIn.value.split(/[,;\n]+/).map(x => x.trim()).filter(Boolean);
    if (!seeds.length && !an.length) return alert('Step 1: add a few rough seed words or 1–6 anchor DOIs.');
    if (!API) return alert('The guided flow needs the Control Center server: start Start_Radar_GUI (or open this page from the radar server).');
    const b = $('#wizplan'); b.disabled = true; b.textContent = '🪄 Planning… (reading your store)';
    let r;
    try { r = await call('POST', '/api/plan_keywords', {pillars: [{name, seeds}], anchors: an}); } catch (e) { r = {ok: false, error: String(e)}; }
    b.disabled = false; b.textContent = '🪄 Auto-Plan Keywords for This New Pillar';
    if (!r.ok) return alert('Planner: ' + (r.error || 'failed'));
    step3(r);
  };
  const create = () => {
    if (!plan) return;
    if (JOB.running || ACT) return toast('Another radar run is still running');
    const name = nameIn.value.replace(/\s+/g, ' ').trim(), an = dois();
    const kws = picks.filter(p => p.box.checked).map(p => [p.x.k, Number(p.w.value)]).filter(([, w]) => isFinite(w) && w >= -20 && w <= 20 && w !== 0);
    if (!kws.length) return alert('Tick at least one keyword for the new pillar.');
    KW.pillars.push({name, short: shortIn.value.trim(), marker: null, kws});
    for (const d of an) if (!S.anchors.some(x => x.toLowerCase() === d.toLowerCase())) S.anchors.push(d);
    for (const {v, box} of vipPicks) if (box.checked) S.vip_authors.push(v.name);
    if (ov && ov.ovBox.checked) S.negative_override = ov.ovIn.value.trim();
    setSeeds(KW.pillars[n - 1], n - 1, seedIn.value);
    $('#modal').hidden = true;
    PLAST = {n, name};
    renderThemes(); renderPeople(); changed();
    run('add_pillar', {pillar: n, anchors: an});
  };
  modal(`＋ Add New Pillar P${n} (Guided)`, h('div', {},
    h('p', {class: 'small muted'}, `Adds one pillar after P1–P${n - 1}; their names, keywords and cached digests stay exactly as they are.`),
    h('h3', {}, 'Step 1 · Name, anchors and seed words'),
    h('div', {class: 'field'}, h('span', {class: 'flabel'}, `P${n} · Full pillar name`), nameIn),
    h('div', {class: 'field'}, h('span', {class: 'flabel'}, 'Short label ', h('span', {class: 'muted small'}, '(optional, for phones)')), shortIn),
    h('div', {class: 'field'}, h('span', {class: 'flabel'}, '🏛️ Anchor DOIs ', h('span', {class: 'muted small'}, '(one per line or comma-separated)')), anchIn),
    h('div', {class: 'field'}, h('span', {class: 'flabel'}, '🌱 Rough seed words ', h('span', {class: 'muted small'}, '(comma-separated or one per line)')), seedIn),
    h('h3', {}, 'Step 2 · Plan its keywords'),
    h('div', {class: 'row'}, h('button', {class: 'btn primary', id: 'wizplan', onclick: planIt}, '🪄 Auto-Plan Keywords for This New Pillar'),
      h('span', {class: 'muted small'}, 'reads your store (and OpenAlex for anchors it lacks); nothing changes yet')),
    out), [
    h('button', {class: 'btn primary', id: 'wizgo', hidden: true, onclick: create}, '✅ Create Pillar & Seed Papers'),
    h('button', {class: 'btn', onclick: () => { $('#modal').hidden = true; }}, 'Cancel')], true);
  const box = document.querySelector('#modal .mbox'); if (box) box.classList.add('wide');
}

function renderTopic() {
  const el = $('#c-topic'); if (!el) return;
  const T = ST.topics || {dives: [], recalls: []}, noClaude = !ST.env.has_claude;
  const inp = ta({class: 'in wide', id: 'topicq', value: TQ, placeholder: 'e.g. sub-100nW sensor design focusing on low-leakage techniques',
    oninput: e => { TQ = e.target.value; try { localStorage.setItem('paper_radar_topic_q', TQ); } catch (x) {} topicPreview(); }}, {single: ' ', enter: () => topicRun('topic_dive')});
  const zbox = h('input', {type: 'checkbox', onchange: e => { TZERO = e.target.checked; }});
  zbox.checked = TZERO || noClaude; zbox.disabled = noClaude;
  const btn = (a, primary) => h('button', {class: 'btn' + (primary ? ' primary' : ''), 'data-run': a, title: RUN_TIPS[a], onclick: () => topicRun(a)}, D.actions[a]);
  let result = null;
  if (TLAST && !JOB.running) {
    const d = TLAST.a === 'topic_dive' ? T.dives.find(x => x.slug === TLAST.slug) : null;
    const rc = TLAST.a === 'topic_recall' ? T.recalls.find(x => x.name.endsWith('_recall_' + TLAST.slug)) : null;
    const href = d ? d.href : rc ? rc.href : '';
    result = h('div', {class: 'row topicres'}, h('span', {class: 'small'}, `Latest: “${TLAST.q}”`),
      href ? h('a', {class: 'btn small primary', href, target: '_blank'}, TLAST.a === 'topic_dive' ? '📄 Open Topic Report' : '📄 Open Recall Results') : h('span', {class: 'muted small'}, '(no report page yet: see the console)'),
      TLAST.a === 'topic_dive' ? h('a', {class: 'btn small', href: 'DASHBOARD.html#topics', target: '_blank'}, '📊 DASHBOARD → 🔎 Topic Dives') : null);
  }
  const order = T.dives.filter(t => !t.retired).concat(T.dives.filter(t => t.retired));
  const dives = T.dives.length ? h('table', {class: 'mini topictab'}, h('tbody', {}, order.map(t => h('tr', {class: t.retired ? 'muted' : ''},
    h('td', {}, h('b', {}, (t.retired ? '🏺 ' : '🔎 ') + t.query), t.retired ? h('span', {class: 'tag'}, '🏺 Antique') : null, h('div', {class: 'muted small'}, t.date)),
    h('td', {class: 'small nowrap', title: `${t.n_classic} foundational classics + ${t.n_recent} recent SOTA papers`}, `${t.n_classic}🏛️ + ${t.n_recent}🚀`),
    h('td', {class: 'nowrap'}, t.href ? h('a', {class: 'btn small', href: t.href, target: '_blank'}, '📄 Report') : h('span', {class: 'muted small'}, 'no report'),
      h('button', {class: 'btn small', title: 'Open ＋ Add New Pillar, pre-filled with this dive\'s title, words and 🏛️ classic DOIs (as anchors)',
                   disabled: KW && KW.pillars.length >= D.max_pillars, onclick: () => promoteDive(t)}, '📌 Promote Dive to Weekly Pillar'),
      t.retired ? h('button', {class: 'btn small', 'data-run': 'unretire_topic', title: 'Bring this dive back to 🔎 Topic Dives', onclick: () => retireTopic(t, true)}, '↺ Restore')
                : h('button', {class: 'btn small', 'data-run': 'retire_topic', title: '🏺 Retire as Antique: the dive moves to Past / Antique Dives on the dashboard; its papers keep their badge, marks, notes and reports', onclick: () => retireTopic(t)}, '🏺 Retire'),
      h('button', {class: 'btn small danger', 'data-run': 'drop_topic', title: 'Delete permanently (the papers lose this dive\'s badge; marks, notes and report files stay)', onclick: () => dropTopic(t)}, '🗑'))))))
    : h('p', {class: 'muted small'}, 'No topic dives yet.');
  fill(el, h('h2', {}, '🔎 Topic Deep-Dive & Local Recall', h('span', {class: 'muted small'}, 'any research question, classics + state of the art')),
    h('div', {class: 'row'}, inp),
    h('div', {class: 'row'}, btn('topic_recall'), btn('topic_dive', true),
      h('label', {class: 'small', title: noClaude ? 'No Claude CLI found: dives always run in 0-token mode' : 'Skip the claude -p synthesis: ranked tables + lineage map only'},
        zbox, noClaude ? ' 0-Token Fast Mode (no Claude CLI found)' : ' 0-Token Fast Mode (--no-claude)')),
    h('div', {class: 'muted small', id: 'topicgroups'}, API ? 'Type a question to see how it is split into concept groups.' : ''),
    h('p', {class: 'muted small'}, '⚡ Recall: your local store, 0 API calls · 🔎 Deep-Dive: + OpenAlex, 6 classics + 8 recent papers.'),
    howBox('⚡ Recall searches your whole local store (archived papers and the corpus included): 0 API calls, 0 tokens. ' +
      '🔎 Deep-Dive adds OpenAlex' + (ST.env.has_ieee_key ? ', the IEEE topic reserve' : '') + ' (and PubMed for BioMed), picks 6 🏛️ classics + 8 🚀 recent papers, ' +
      'writes Topic_Dives/<date>_<topic>.html with a lineage map and adds the dive to the dashboard\'s 🔎 Topic Dives.'),
    result,
    h('h3', {}, `Existing Topic Dives (${T.dives.length})`), dives,
    T.recalls.length ? h('p', {class: 'small'}, 'Recent recalls: ', T.recalls.map(r => h('a', {href: r.href, target: '_blank', class: 'tag'}, r.name.replace(/^\d{4}-\d\d-\d\d_recall_/, '')))) : null);
  lockRun();
}

// ---------- 🪄 0-token keyword & weight planner (v3.0.5, keyword_planner.py) ----------
let SEEDMAP = (() => { try { return JSON.parse(localStorage.getItem('paper_radar_seeds') || '{}'); } catch (e) { return {}; } })();
const seedKey = (p, i) => (p.name || '').trim() || `#${i}`;
function seedsOf(p, i) { return p.seeds != null ? p.seeds : (SEEDMAP[seedKey(p, i)] || ''); }
function setSeeds(p, i, v) { p.seeds = v; SEEDMAP[seedKey(p, i)] = v; try { localStorage.setItem('paper_radar_seeds', JSON.stringify(SEEDMAP)); } catch (e) {} }
const LEGEND = [[5, 'Core target phrase', '1 hit alone passes min_score', 'ring amplifier · noise-shaping SAR'],
  [4, 'Specific topology / acronym', 'rare in the store, frequent in your field', 'MDAC · NTF · residue amplifier'],
  [3, 'Key mechanism / metric regex', 're:… unit patterns (nW, fJ, MS/s …)', 'comparator · re:fJ-energy'],
  [2, 'Supporting circuit / method term', 'helps in combination', 'gain error · calibration scheme'],
  [1, 'Broad context word', 'never passes min_score alone', 'low power · sensor · CMOS']];
function renderPlan() {
  const el = $('#c-plan'); if (!el || !KW) return;
  const ms = S.min_score == null ? 5 : S.min_score;
  const legend = h('table', {class: 'mini legend'}, h('tbody', {}, LEGEND.map(([w, t, note, ex]) => h('tr', {},
    h('td', {}, h('span', {class: 'wpill w' + w}, '+' + w)), h('td', {}, h('b', {}, t), h('div', {class: 'muted small'}, note)),
    h('td', {class: 'small muted'}, ex)))));
  const pillars = KW.pillars.map((p, i) => h('div', {class: 'row planrow'}, h('span', {class: 'num'}, `P${i + 1}`),
    i >= (D.fixed_pillars || 0) ? ta({class: 'in pname', value: p.name, placeholder: 'Pillar name, e.g. Pipelined & Ring-Amp ADCs',
                          oninput: e => { const s0 = seedsOf(p, i); p.name = e.target.value; setSeeds(p, i, s0); changed(); }, onchange: () => renderThemes()}, {single: ' '})
            : h('b', {class: 'pname'}, p.name.replace(/^Pillar\s*\d+\s*[—–-]\s*/, '')),
    ta({class: 'in seeds', value: seedsOf(p, i), placeholder: 'rough seed words, comma-separated or one per line: ring amplifier, pipelined ADC, low power',
                oninput: e => setSeeds(p, i, e.target.value)})));
  const doiIn = ta({class: 'in mono', placeholder: '10.1109/JSSC.2020.1234567 or a doi.org link: one per line or comma-separated for several'}, {enter: () => addDoi()});
  const addDoi = () => {
    const ds = doiIn.value.split(/[\s,;]+/).map(cleanDoi).filter(Boolean); if (!ds.length) return;
    const bad = ds.filter(d => !/^10\.\d{4,9}\/\S+$/.test(d));
    if (bad.length) return alert('Not a DOI (a DOI starts with 10.): ' + bad.join(', '));
    let n = 0; for (const d of ds) if (!S.anchors.some(x => x.toLowerCase() === d.toLowerCase())) { S.anchors.push(d); n++; }
    doiIn.value = ''; if (!n) return toast(ds.length > 1 ? 'Already anchors' : 'Already an anchor');
    changed(); renderPeople();
  };
  const anchors = S.anchors.length ? h('div', {class: 'chips'}, S.anchors.map((d, i) => h('span', {class: 'chip'}, h('span', {class: 't'}, d),
      h('button', {title: 'Remove anchor', onclick: () => { S.anchors.splice(i, 1); changed(); renderPeople(); }}, '✕'))))
    : h('p', {class: 'muted small'}, 'No anchors yet: 2–5 papers you love make the plan much better.');
  fill(el, h('h2', {}, '🪄 Quick Keyword Setup', h('span', {class: 'muted small'}, '0 tokens, no AI: from your anchors and a few rough words')),
    h('details', {class: 'how legendbox'}, h('summary', {}, `💡 How keyword weights work (min_score = ${ms})`), legend,
      h('p', {class: 'muted small'}, `A paper becomes a candidate when the weights of the keywords in its title, abstract and index terms add up to ${ms}: one +5 is enough, +1 words only help in combination. ALL-CAPS keywords match case; a space or hyphen matches either; plurals match.`)),
    h('h3', {}, '① Name your pillars, add 2–5 anchor DOIs and a few rough seed words'),
    pillars,
    h('div', {class: 'row'}, h('button', {class: 'btn small', disabled: KW.pillars.length >= D.max_pillars, onclick: () => {
      KW.pillars.push({name: '', short: '', marker: null, kws: []}); changed(); renderThemes(); }}, '＋ Add pillar'),
      h('button', {class: 'btn small primary', disabled: KW.pillars.length >= D.max_pillars, onclick: () => addPillarWizard()}, '＋ Add New Pillar (Guided)'),
      h('span', {class: 'muted small'}, 'already set up? the guided flow plans keywords for one new pillar and leaves the others untouched')),
    PLAST ? pillarResult() : null,
    h('div', {class: 'field'}, h('span', {class: 'flabel'}, '🏛️ Anchor DOIs (papers you love) ', h('span', {class: 'muted small'}, 'Enter adds · Shift+Enter for a new line')), doiIn,
      h('div', {class: 'row'}, h('button', {class: 'btn small', onclick: addDoi}, '＋ Add'))), anchors,
    h('h3', {}, '② Let the planner build the weighted keywords'),
    h('div', {class: 'row'}, h('button', {class: 'btn primary', id: 'planbtn', onclick: planKeywords}, '🪄 Auto-Plan Keywords, Weights & VIPs (from Anchors + Seeds)'),
      h('span', {class: 'muted small'}, 'Reads your paper store (and OpenAlex for anchors it lacks). Nothing is changed until you apply.')),
    h('h3', {}, '③ Review the suggestions and apply'),
    h('p', {class: 'muted small'}, 'Untick what you don\'t want, then ✅ Apply Selected to My Config.'),
    howBox('A preview lists weighted keyword chips (+5 down to +1), metric regexes, a negative_override and VIP authors, each with the evidence behind it. Untick what you don\'t want, adjust weights, then ✅ Apply Selected to My Config. Fine-tune anything later in 🎛️ Fine-tune keywords & weights below.'));
}
async function planKeywords() {
  const pillars = KW.pillars.map((p, i) => ({name: (p.name || '').trim(), seeds: seedsOf(p, i).split(/[,;\n]+/).map(s => s.trim()).filter(Boolean)}));
  if (!pillars.length || pillars.some(p => !p.name)) return alert('Step 1: give every pillar a name first.');
  if (!pillars.some(p => p.seeds.length) && !S.anchors.length) return alert('Step 1: add a few rough seed words or 2–5 anchor DOIs first.');
  if (!API) {
    const cmd = `${D.python || 'python'} briefing.py --plan-keywords "${pillars.map(p => p.seeds.join(', ')).join('; ')}"`;
    return modal('🪄 Auto-Plan needs the Control Center server', h('div', {}, h('p', {}, 'This page was opened as a file. Start Start_Radar_GUI (or python briefing.py --gui) for the interactive preview, or run this in the radar folder:'),
      h('pre', {class: 'bib'}, cmd)), [h('button', {class: 'btn', onclick: () => { $('#modal').hidden = true; }}, 'Close')]);
  }
  const b = $('#planbtn'); if (b) { b.disabled = true; b.textContent = '🪄 Planning… (reading your store)'; }
  let r;
  try { r = await call('POST', '/api/plan_keywords', {pillars, anchors: S.anchors}); }
  catch (e) { r = {ok: false, error: String(e)}; }
  finally { if (b) { b.disabled = false; b.textContent = '🪄 Auto-Plan Keywords, Weights & VIPs (from Anchors + Seeds)'; } }
  if (!r.ok) return alert('Planner: ' + (r.error || 'failed'));
  planModal(r);
}
function planModal(r) {
  const picks = [], vipPicks = [];
  const sections = r.pillars.map(pl => h('div', {class: 'planp'},
    h('h3', {}, `P${pl.n} · ${pl.name}`, h('span', {class: 'muted small'}, ` · ${pl.seed_papers} seed papers (${pl.anchors.length} anchors)`)),
    pl.examples.length ? h('details', {class: 'small muted'}, h('summary', {}, 'seed papers it learned from'), h('ul', {}, pl.examples.map(t => h('li', {}, t)))) : null,
    pl.keywords.length ? h('table', {class: 'mini plantab'}, h('tbody', {}, pl.keywords.map(x => {
      const box = h('input', {type: 'checkbox'}); box.checked = !x.exists;
      const w = h('input', {class: 'in w wsel w' + x.w, type: 'number', min: -20, max: 20, step: 1, value: x.w,
                           oninput: e => { e.target.className = 'in w wsel w' + Math.max(1, Math.min(5, Math.round(Number(e.target.value) || 1))); }});
      picks.push({x, box, w, n: pl.n});
      const rx = x.k.startsWith('re:');
      return h('tr', {class: x.exists ? 'muted' : ''}, h('td', {}, box), h('td', {}, w),
        h('td', {}, rx ? h('span', {title: x.k}, h('code', {}, x.k.slice(3).split('::')[0]), h('span', {class: 'muted small'}, ' regex' + (x.what ? ' · ' + x.what : '')))
                       : h('b', {}, x.k), x.kind === 'seed' ? h('span', {class: 'tag'}, 'your seed') : null,
          x.exists ? h('span', {class: 'tag'}, `already yours (${x.current > 0 ? '+' : ''}${x.current})`) : null),
        h('td', {class: 'small muted'}, `${x.why} · ${x.s}/${x.n_s} seed papers · ${fmt(x.b)}/${fmt(x.store)} in store`));
    }))) : h('p', {class: 'muted small'}, 'No suggestions: add seed words or anchors for this pillar.')));
  const no = r.negative_override || {};
  const ovBox = h('input', {type: 'checkbox'}); ovBox.checked = !!no.suggest && no.merged !== no.current;
  const ovIn = ta({class: 'in wide mono', value: no.merged || ''}, {single: ''});
  const vips = (r.vip_authors || []).map(v => { const box = h('input', {type: 'checkbox'}); box.checked = true; vipPicks.push({v, box});
    return h('label', {class: 'chip', title: `${v.seed_papers} of their ${v.store} papers in your store are seed papers`}, box, ` ${v.name} `, h('span', {class: 'muted small'}, v.store ? `(${v.seed_papers}/${v.store})` : `(${v.score})`)); });
  const missing = (r.anchors || []).filter(a => a.source === 'missing');
  const body = h('div', {class: 'planbody'},
    h('p', {class: 'small muted'}, `${fmt(r.store_papers)} papers in your store (${r.fts ? 'FTS5' : 'scan'}) · weights from specificity: share of the seed papers ÷ share of the whole store; terms in more than 5 % of the store stay at +1.`),
    r.calibration ? h('p', {class: 'small ' + (r.calibration.added > 0.12 * r.store_papers ? 'warn' : 'ok')}, `📏 With these weights ≈ ${fmt(r.calibration.pass)} papers (${(r.calibration.share * 100).toFixed(1)} % of your store) reach min_score, covering ${Math.round(r.calibration.seed_cover * 100)} % of the ${fmt(r.calibration.seed_papers)} seed papers` +
      (r.calibration.base ? ` (your current keywords alone pass ${fmt(r.calibration.base)}; the plan adds ${fmt(r.calibration.added)})` : '') +
      (r.calibration.lowered ? ` · ${r.calibration.lowered} weights were lowered so common terms don't flood your radar (${fmt(r.calibration.before)} papers before).` : '.')) : null,
    missing.length ? h('p', {class: 'small warn'}, `Not found (neither in your store nor on OpenAlex): ${missing.map(a => a.doi).join(', ')}`) : null,
    sections,
    h('h3', {}, '🛡️ negative_override (title regex that keeps your core papers from being zeroed by a negative keyword)'),
    h('label', {class: 'row'}, ovBox, ovIn), no.current ? h('p', {class: 'muted small'}, `current: ${no.current} (the suggestion adds the core phrases to it)`) : null,
    h('h3', {}, '👤 Suggested VIP authors (frequent in your anchors ×3 and seed papers, and focused: ≥ 20 % of their papers are seed papers)'),
    vips.length ? h('div', {class: 'chips'}, vips) : h('p', {class: 'muted small'}, 'none'));
  const apply = () => {
    let nk = 0, nu = 0, nv = 0;
    for (const p of picks) {
      if (!p.box.checked) continue;
      const w = Number(p.w.value); if (!isFinite(w) || w < -20 || w > 20 || !KW.pillars[p.n - 1]) continue;
      const hit = [KW.loose, ...KW.pillars.map(q => q.kws), ...KW.orphans.map(o => o.kws)].flat().find(it => it[0].toLowerCase() === p.x.k.toLowerCase());
      if (hit) { if (hit[1] !== w) { hit[1] = w; nu++; } } else { KW.pillars[p.n - 1].kws.push([p.x.k, w]); nk++; }
    }
    for (const {v, box} of vipPicks) if (box.checked && !S.vip_authors.some(x => x.toLowerCase() === v.name.toLowerCase())) { S.vip_authors.push(v.name); nv++; }
    if (ovBox.checked) S.negative_override = ovIn.value.trim();
    $('#modal').hidden = true;
    renderThemes(); renderPeople(); changed();
    const msg = `Applied ${nk} keywords (${nu} weights changed), ${nv} VIP authors${ovBox.checked ? ', negative_override' : ''}`;
    if (API) { save(); toast(msg + ' · saving config.json', 6000); } else toast(msg + ' · 💾 Save to download config.json', 7000);
  };
  modal('🪄 Suggested keywords, weights & VIPs', body, [
    h('button', {class: 'btn primary', onclick: apply}, '✅ Apply Selected to My Config'),
    h('button', {class: 'btn', onclick: () => { picks.forEach(p => { p.box.checked = !p.x.exists; }); }}, 'Select all new'),
    h('button', {class: 'btn', onclick: () => { picks.forEach(p => { p.box.checked = false; }); }}, 'Clear'),
    h('button', {class: 'btn', onclick: () => { $('#modal').hidden = true; }}, 'Cancel')]);
  const mb = $('#modal .mbox'); if (mb) mb.classList.add('wide');
}

// ---------- 3. key authors & anchors ----------
const VIP_TIP = 'Matching: a full name (“Kofi A. A. Makinwa”) matches that first name + surname; an initial form (“T. Jang”) matches only authors listed with that initial, so “Taehoon Jang” doesn’t trigger it; a surname alone (“Makinwa”) matches any author with that surname. Add the spellings you see in IEEE Xplore (accents too).';
function cleanDoi(v) { return v.replace(/^https?:\/\/(dx\.)?doi\.org\//i, '').replace(/^doi:\s*/i, '').trim(); }
function renderPeople() {
  const info = ST.store.anchors || {};
  const rows = S.anchors.map((d, i) => {
    const a = info[d.toLowerCase()];
    return h('tr', {}, h('td', {}, a ? (a.pin === 'anchor' ? '🏛️' : '📌') : '⏳'),
      h('td', {}, h('a', {href: 'https://doi.org/' + d, target: '_blank', rel: 'noopener'}, d)),
      h('td', {}, a ? `${a.title || ''} ` : h('span', {class: 'muted'}, 'not in the store yet: fetched and pinned on the next ▶ Run'), a ? h('span', {class: 'muted small'}, `${a.venue || ''} ${a.year || ''}`) : null),
      h('td', {}, h('button', {class: 'btn small danger', title: 'Remove anchor', onclick: () => { S.anchors.splice(i, 1); renderPeople(); changed(); }}, '✕')));
  });
  const doiIn = ta({class: 'in mono', placeholder: '10.1109/JSSC.2020.1234567 or a doi.org link: one per line or comma-separated for several'}, {enter: () => addDoi()});
  const addDoi = () => {
    const ds = doiIn.value.split(/[\s,;]+/).map(cleanDoi).filter(Boolean); if (!ds.length) return;
    const bad = ds.filter(d => !/^10\.\d{4,9}\/\S+$/.test(d));
    if (bad.length) return alert('Not a DOI (a DOI starts with 10.): ' + bad.join(', '));
    let n = 0; for (const d of ds) if (!S.anchors.some(x => x.toLowerCase() === d.toLowerCase())) { S.anchors.push(d); n++; }
    doiIn.value = ''; if (!n) return toast(ds.length > 1 ? 'Already anchors' : 'Already an anchor');
    renderPeople(); changed();
  };
  fill($('#c-people'), h('h2', {}, '👤 Key Authors & 🏛️ Classic Anchors'),
    h('div', {class: 'grid2'},
      h('div', {}, h('h3', {}, 'VIP authors ', h('span', {class: 'tip muted small', title: VIP_TIP}, 'ⓘ how names match')),
        chips(S.vip_authors, {placeholder: 'e.g. Kofi A. A. Makinwa', size: 26, tip: () => VIP_TIP}),
        h('div', {class: 'row'}, h('label', {}, 'vip_boost +', h('input', {class: 'in w', type: 'number', min: 0, max: 50, step: 1, value: S.vip_boost,
          oninput: e => { S.vip_boost = Number(e.target.value); changed(); }})), h('span', {class: 'muted small'}, 'added to the score of a paper by a VIP author.'))),
      h('div', {}, h('h3', {}, `Anchor DOIs (${S.anchors.length}) `, h('span', {class: 'tip muted small', title: 'Classic papers always kept in the catalog (🏛️ = pinned as anchor, 📌 = pinned by you, ⏳ = fetched on the next run). They get anchor_boost.'}, 'ⓘ')),
        h('div', {class: 'row'}, doiIn, h('button', {class: 'btn small', onclick: addDoi}, '＋ Add anchor')),
        rows.length ? h('div', {class: 'tablewrap'}, h('table', {class: 'mini'}, h('tbody', {}, rows))) : h('p', {class: 'muted small'}, 'No anchors yet.'))));
  renderPlan();
}

// ---------- 4. private shared repo & peers ----------
function toggle(obj, key, label, tip) {
  return h('label', {title: tip || ''}, h('input', {type: 'checkbox', checked: !!obj[key], onchange: e => { obj[key] = e.target.checked; changed(); renderRepoStats(); }}), label);
}
function renderRepoStats() {
  const s = ST.sharing, el = $('#sharestats'); if (!el) return;
  el.textContent = S.data_repo.share_curation
    ? `You share: ✅ ${fmt(s.selected)} Selected · ⭐ ${fmt(s.cited)} Cited · 🏷️ ${S.data_repo.share_tags === false ? 'nothing (tag sharing off)' : (S.data_repo.share_notes ? fmt(s.notes) + ' Tech notes' : 'notes kept private')} (in peers/${ST.data_repo.me || 'you'}.json)`
    : 'You share no curation: only the clean corpus (papers, abstracts, digests) goes to the repo.';
}
function linesList(arr) { return h('textarea', {class: 'in auto-grow', rows: 2, oninput: e => { arr.splice(0, arr.length, ...e.target.value.split('\n').map(x => x.trim()).filter(Boolean)); changed(); }}, arr.join('\n')); }
// v3.0.4: read_repos chips are plain specs; S.data_repo.read_opts[spec] = {merge_corpus, force_merge}
function dataRepoOut() {
  const d = Object.assign({}, S.data_repo), o = d.read_opts || {};
  delete d.read_opts;
  d.read_repos = (d.read_repos || []).map(s => {
    const x = o[s] || {};
    return x.merge_corpus ? Object.assign({repo: s, merge_corpus: true}, x.force_merge ? {force_merge: true} : {}) : s;
  });
  return d;
}
function simNote(x) {
  if (!x.sim) return h('div', {class: 'muted small'}, x.ready ? '' : '⏳ domain check after the next 📥 Read');
  const pct = v => Math.round((v || 0) * 100) + ' %';
  const ov = h('div', {class: 'muted small'}, `${x.sim.theirs_label || 'field not stated'} · venue overlap ${pct(x.sim.venue_overlap)} · DOI overlap ${pct(x.sim.doi_overlap)}`);
  return x.sim.kind === 'similar'
    ? h('div', {}, h('div', {class: 'small ok'}, `🟢 Similar domain detected (${x.sim.theirs_label || x.sim.mine_label}). You can keep this repo separated as a standalone dashboard, or optionally enable “Merge & Sync Corpus” to absorb its papers.`), ov)
    : h('div', {}, h('div', {class: 'small warn'}, `🟠 Different discipline detected (${x.sim.theirs_label || 'another field'} vs. ${x.sim.mine_label}). Locked to Isolated Dashboard Mode by default so its papers never mix into or pollute your SQLite store or your primary GitHub private repo!`), ov);
}
function mergeToggle(x) {
  const o = (S.data_repo.read_opts = S.data_repo.read_opts || {});
  const cur = o[x.spec] || {};
  const box = h('input', {type: 'checkbox', disabled: !x.sim, onchange: e => {
    if (e.target.checked && x.sim && x.sim.kind !== 'similar' && !confirm(`🟠 ${x.label} is a different discipline (${x.sim.theirs_label || 'another field'} vs. ${x.sim.mine_label}). Merging puts its ${fmt(x.papers)} papers into your store, and Full Sync then uploads them into your primary private repo. Merge anyway?`)) {
      e.target.checked = false; return;
    }
    o[x.spec] = e.target.checked ? Object.assign({merge_corpus: true}, x.sim && x.sim.kind !== 'similar' ? {force_merge: true} : {}) : {};
    if (!e.target.checked) delete o[x.spec];
    changed(); toast(e.target.checked ? 'Merge on: 💾 Save, then 📥 Read merges it' : 'Isolated: 💾 Save, then 📥 Read');
  }});
  box.checked = !!cur.merge_corpus;
  const mode = x.mode === 'merged' ? h('span', {class: 'tag'}, '🔀 Merged into my corpus') : h('span', {class: 'tag'}, '🔒 Isolated dashboard');
  return h('div', {}, mode, h('label', {class: 'small', title: x.sim ? '' : 'after the next 📥 Read'}, box, ' Merge & Sync Corpus'));
}
function renderRepo() {
  const dr = ST.data_repo;
  const head = dr.connected
    ? h('div', {class: 'kv'},
        h('b', {}, 'Status'), h('span', {class: 'ok'}, `🟢 Connected to ${repoName(dr.remote_url) || 'your data repo'}`),
        h('b', {}, 'Clone'), h('code', {}, dr.dir),
        h('b', {}, 'Shared corpus'), h('span', {}, `${fmt(dr.papers)} papers in paper_store_clean_share.sqlite`,
          dr.missing_here ? h('span', {class: 'warn'}, ` · ${fmt(dr.missing_here)} not in your store yet`) : (dr.missing_here === 0 ? h('span', {class: 'ok'}, ' · all in your store') : null)),
        h('b', {}, 'Last commit'), dr.commit ? h('span', {}, h('code', {}, dr.commit.hash), ` ${dr.commit.date.replace('T', ' ').slice(0, 16)} · `, h('span', {class: 'muted'}, dr.commit.subject)) : h('span', {class: 'muted'}, '–'),
        h('b', {}, 'You'), h('span', {}, dr.me || '–'))
    : h('div', {id: 'repoopts'},
        h('p', {class: 'warn'}, '⚠️ No primary data repo yet', h('span', {class: 'muted small'}, ` (${dr.reason})`)),
        h('h3', {}, '✨ Option 1: create your own private data repo'),
        h('p', {class: 'small'}, 'With the GitHub CLI (gh) signed in, the repo is created for you.'),
        howBox('Your corpus, your GitHub account: with the GitHub CLI (gh) signed in, the repo is created for you; otherwise create an empty private repo on github.com first. The protocol files, your clean corpus and your overlay are pushed to main.'),
        (() => {
          const go = () => { const v = spec.value.trim(); if (!v) return alert('Type owner/name (or a clone URL) first.'); run('init_data_repo', {spec: v}); };
          const spec = ta({class: 'in mono', placeholder: 'your-name/my-paper-radar-data', 'aria-label': 'owner/name or URL'}, {single: '', enter: go});
          return h('div', {class: 'field'}, h('span', {class: 'flabel'}, 'Repo (owner/name or clone URL)'), spec,
            h('div', {class: 'row'}, h('button', {class: 'btn primary', 'data-run': 'init_data_repo', title: RUN_TIPS.init_data_repo, onclick: go}, D.actions.init_data_repo)));
        })(),
        h('h3', {}, '📥 Option 2: clone a data repo someone shared with you'),
        h('p', {class: 'small'}, 'Git asks you to sign in to GitHub once.'),
        howBox('A collaborator with access can clone it here and load the shared corpus (papers, abstracts, digests) without a terminal. Git asks you to sign in to GitHub once. (Read access only? Add it under “Shared repos to read” instead.)'),
        (() => {
          const url = ta({class: 'in wide mono', value: D.data_url, 'aria-label': 'clone URL'}, {single: ''});
          const dir = ta({class: 'in wide mono', value: S.data_repo_dir || '../ic-paper-radar-data', 'aria-label': 'clone folder'}, {single: ''});
          return h('div', {}, h('div', {class: 'field'}, h('span', {class: 'flabel'}, 'Repo URL'), url),
            h('div', {class: 'field'}, h('span', {class: 'flabel'}, 'Into folder ', h('span', {class: 'muted small'}, '(paths.data_repo_dir)')), dir),
            h('div', {class: 'row'}, h('button', {class: 'btn primary', 'data-run': 'clone', onclick: () => {
              const d = dir.value.trim();
              run('pull_data_repo', {clone_url: url.value.trim(), clone_dir: d === '../ic-paper-radar-data' ? '' : d}); }}, '📥 Clone & Connect Private Data Repo'),
              h('span', {class: 'muted small'}, 'relative folders are next to the radar folder’s scripts')));
        })());
  const peers = dr.peers.length ? h('table', {class: 'mini'}, h('thead', {}, h('tr', {}, h('th', {}, 'Peer'), h('th', {}, 'Rows'), h('th', {}, '✅'), h('th', {}, '⭐'), h('th', {}, 'Updated'), h('th', {}, 'Page'))),
      h('tbody', {}, dr.peers.map(p => h('tr', {}, h('td', {}, h('b', {}, p.id), p.me ? h('span', {class: 'tag'}, 'you') : null,
        p.pillars && p.pillars.length ? h('div', {class: 'muted small'}, p.pillars.map(x => String(x).replace(/^Pillar\s*\d+\s*[—–-]\s*/, '')).join(' · ')) : null),
        h('td', {}, fmt(p.rows)), h('td', {}, fmt(p.selected)), h('td', {}, fmt(p.cited)), h('td', {class: 'small'}, (p.updated || '').replace('T', ' ')),
        h('td', {}, p.href ? h('a', {href: p.href}, `👥 ${p.id}'s Radar ↗`) : h('span', {class: 'muted small'}, p.me ? 'your dashboard' : 'after the next Read'))))))
    : h('p', {class: 'muted small'}, 'No peer overlays yet.');
  const dro = S.data_repo;
  const ax = STARTER ? S.arxiv : null;
  const rr = (dr.read_repos || []);
  const reads = h('div', {}, h('h3', {}, '📚 Shared repos to read (data_repo.read_repos)'),
    h('p', {class: 'small muted'}, 'Repos collaborators shared with you (read access is enough): pulled only, never pushed; isolated by default.'),
    howBox('Private data repos your collaborators shared with you (read access is enough): pulled into <data_repo_dir>_peers/ and never pushed. Each one is an isolated dashboard by default (📚 Switch Radar / Repo in the dashboard header; 0 papers enter your store); merge a similar-domain repo into your corpus with “Merge & Sync Corpus”. owner/name, a clone URL or a local folder.'),
    chips(dro.read_repos, {placeholder: 'colleague/their-paper-radar-data, a clone URL or a folder', long: true,
      check: v => (/^[A-Za-z0-9][\w.-]*\/[\w.-]+$/.test(v) || /^(https:\/\/[A-Za-z0-9.-]+\/|git@[A-Za-z0-9.-]+:)[\w.-]+\/[\w.-]+$/.test(v) || /[\\/]/.test(v)) ? null : 'owner/name, https://…, git@… or a folder'}),
    rr.length ? h('table', {class: 'mini'}, h('tbody', {}, rr.map(x => h('tr', {}, h('td', {class: 'small'}, h('code', {}, x.spec),
      h('div', {class: 'muted small'}, x.error ? h('span', {class: 'warn'}, x.error) : (x.ready ? `${fmt(x.papers)} papers` : '⏳ pulled on the next 📥 Read'))),
      h('td', {}, simNote(x)), h('td', {}, mergeToggle(x)))))) : null);
  fill($('#c-repo'), h('h2', {}, '🤝 Private Shared Repo & Peer Sync'),
    h('div', {class: 'runbar'}, runBtn('pull_data_repo'), runBtn('sync_data_repo', null, false, '🔄 Full Sync with Primary Repo'),
      !dr.connected ? h('button', {class: 'btn', title: 'Create your own private data repo, or clone one shared with you', onclick: () => {
        const e = $('#repoopts'); if (!e) return; e.scrollIntoView({behavior: 'smooth', block: 'center'});
        const i = e.querySelector('.in'); if (i) setTimeout(() => i.focus({preventScroll: true}), 350); }}, '📦 Clone / Init') : null),
    h('p', {class: 'muted small'}, dr.connected ? '📥 Read pulls only (safe any time) · 🔄 Full Sync also pushes to your repo' + (S.schedule && S.schedule.auto_upload_repo !== false ? ` (automatic on Fridays at ${S.schedule.upload_time}: ⏰ Friday Schedule below).` : '.')
      : (dro.read_repos.length ? 'Read works now (shared repos only); Full Sync needs your own repo: 📦 Clone / Init below.' : 'Solo use works without a repo. For a team: 📦 Clone / Init below.')),
    head,
    h('h3', {}, 'My sharing'), h('p', {id: 'sharestats', class: 'small'}),
    h('label', {class: 'field'}, h('span', {class: 'flabel'}, 'peer_id ', h('span', {class: 'muted small'}, '(your name in the shared repo)')),
      h('input', {class: 'in', maxlength: 39, style: 'max-width:360px', value: dro.peer_id, placeholder: dr.me || 'your-name',
        title: 'Your name in the shared repo (peers/<peer_id>.json). Empty = the clone\'s git user.name.', oninput: e => { dro.peer_id = e.target.value.trim(); changed(); }})),
    h('label', {class: 'field'}, h('span', {class: 'flabel'}, 'remote_url ', h('span', {class: 'muted small'}, '(your primary private data repo)')),
      ta({class: 'in mono', value: dro.remote_url, placeholder: 'https://github.com/you/your-paper-radar-data.git',
        title: 'Your primary private data repo. A new computer clones it into paths.data_repo_dir automatically.', oninput: e => { dro.remote_url = e.target.value.trim(); changed(); }}, {single: ''})),
    h('div', {class: 'row'}, toggle(dro, 'share_tags', '🏷️ Share my curated tags & peer radar (✅ / ⭐ / 🔬 / Pillars) to the shared repo',
        'data_repo.share_tags: write peers/<peer_id>.json so collaborators see your radar. Off: syncs still pull peers and share clean papers, and your peers/<peer_id>.json is removed from the repo in the next sync.')),
    h('p', {class: 'muted small'}, '🔒 Your 🏷️ text notes (user_note) stay on this computer unless “share my 🏷️ notes” is ticked, and never leave it while tag sharing is off.'),
    h('div', {class: 'row'}, toggle(dro, 'share_notes', 'share my 🏷️ notes', 'Include your 🏷️ text notes in peers/<peer_id>.json (only while tag sharing is on)'),
      toggle(dro, 'adopt_external', 'adopt shared papers ≥ min_score', 'Merged papers that score ≥ min_score with your keywords join your archive (and can reach your pillars)')),
    STARTER ? h('div', {}, h('h3', {}, 'arXiv feed (optional)'),
      h('div', {class: 'row'}, toggle(ax, 'enabled', 'enabled', 'Run the arXiv queries on the weekly run'), toggle(ax, 'share', 'share arXiv papers', 'Include them in the shared corpus (selected or cited ones always are)')),
      h('div', {class: 'grid2'},
        h('div', {}, h('div', {class: 'small muted'}, 'Queries (one arXiv search_query per line)'), linesList(ax.queries)),
        h('div', {}, h('div', {class: 'small muted'}, 'must_all: one group per line, terms separated by commas (a paper needs a term of every group)'),
          h('textarea', {class: 'in auto-grow', rows: 2, oninput: e => { ax.must_all = e.target.value.split('\n').map(l => l.split(',').map(x => x.trim()).filter(Boolean)).filter(g => g.length); changed(); }},
            (ax.must_all || []).map(g => g.join(', ')).join('\n')))),
      h('div', {class: 'grid2'}, h('div', {}, h('div', {class: 'small muted'}, 'Categories (empty = all)'), chips(ax.categories, {placeholder: 'e.g. cs.AR', check: v => /^[a-z-]+(\.[A-Za-z-]+)?$/.test(v) ? null : 'An arXiv category looks like cs.AR or eess.SP'})),
        h('div', {}, h('div', {class: 'small muted'}, 'exclude_any'), chips(ax.exclude_any, {placeholder: 'e.g. quantum'})))) : null,
    reads,
    h('h3', {}, 'Peers'), peers);
  renderRepoStats(); lockRun();
}

// ---------- v3.0: deployment selector (💻 / 🌐 / 🏢 easter egg) ----------
function modal(title, body, buttons) {
  const m = $('#modal');
  fill(m, h('div', {class: 'mbox', role: 'dialog', 'aria-modal': 'true'}, h('h2', {}, title), body,
    h('div', {class: 'runbar', style: 'justify-content:flex-end;margin-top:14px'}, buttons)));
  m.hidden = false;
  m.onclick = e => { if (e.target === m) m.hidden = true; };
}
function enterprise() {
  modal('🚧 ERROR 402: COFFEE & CONSULTING BUDGET REQUIRED', h('div', {},
    h('p', {}, 'Deploying a multi-tenant public cluster with Kubernetes, corporate SSO, firewall traversal, and legal compliance is NOT a problem I solve for free! 😂 Either switch back to Single PC / Tailscale Virtual LAN ($0 and works in 2 minutes), or bring specialty coffee and contact the author at github.com/jdong-p!'),
    h('p', {class: 'muted'}, '部署带 Kubernetes、企业 SSO、防火墙穿透和合规审查的多租户公共集群？这可不是免费解决的问题！😂 要么切回单机 / Tailscale 虚拟局域网（零成本，两分钟搞定），要么带上精品咖啡，到 github.com/jdong-p 联系作者！'),
    h('p', {class: 'small muted'}, 'Nothing was changed in config.json.')),
    [h('button', {class: 'btn primary', onclick: () => { $('#modal').hidden = true; S.deployment = 'local'; renderDeploy(); changed(); }}, '😅 Back to Single PC'),
     h('a', {class: 'btn', href: 'https://github.com/jdong-p/engineer-paper-radar', target: '_blank', rel: 'noopener'}, '⭐ Star / Contact on GitHub')]);
}
function renderDeploy() {
  const dp = ST.deploy || {}, cur = S.deployment;
  const card = (k, icon, title, sub, onclick) => h('button', {class: 'dcard' + (cur === k ? ' on' : ''), onclick}, h('span', {class: 'di'}, icon),
    h('b', {}, title), h('span', {class: 'muted small'}, sub));
  const mesh = cur === 'mesh_lan' ? h('div', {class: 'dmesh'},
    h('h3', {}, '🌐 LAN / Tailscale hosts (sync_server.hosts)'),
    h('p', {class: 'small muted'}, STARTER
      ? `The --gui server also listens on these private-LAN / Tailscale IPs (port ${dp.gui_port}), each behind the per-launch key; its links are in _system/gui_link.txt. Only addresses like 192.168.x.x, 10.x.x.x or 100.64–127.x.x (Tailscale) are used.`
      : `radar_sync_server.py answers on :${dp.port}; these hosts are the addresses your pages try (127.0.0.1 first). Your phone opens the key link from Open_on_iPhone.txt once.`),
    chips(S.sync_hosts, {placeholder: "this PC's LAN or Tailscale IP", check: v => /^[A-Za-z0-9][A-Za-z0-9.-]{0,99}$/.test(v) ? null : 'A host name or IP address'}),
    h('div', {class: 'row small'}, dp.phone_link ? h('a', {href: dp.phone_link}, '📱 Open_on_iPhone.txt (the key link for your phone)') : null,
      dp.gui_link ? h('span', {class: 'muted'}, `🔑 GUI links: ${dp.gui_link}`) : null),
    h('h3', {}, '🗓️ Task Scheduler (copy into a terminal on the radar computer)'),
    (dp.tasks || []).map(cmdRow)) : null;
  fill($('#c-deploy'), h('h2', {}, '🖥️ Deployment', h('span', {class: 'muted small'}, 'where the radar runs and who can reach it')),
    h('div', {class: 'dgrid'},
      card('local', '💻', 'Single PC / Standalone', 'GUI and sync on 127.0.0.1 only · local SQLite + your private GitHub data repo', () => { S.deployment = 'local'; renderDeploy(); changed(); }),
      card('mesh_lan', '🌐', 'Virtual Mesh LAN Hub', 'Tailscale / ZeroTier + a mini-PC · phone and laptops over the tailnet', () => { S.deployment = 'mesh_lan'; renderDeploy(); changed(); }),
      card('enterprise', '🏢', 'Enterprise / Public Multi-Tenant', 'Kubernetes, SSO, the works…', enterprise)),
    mesh);
}

// ---------- v3.0: domain presets ----------
let PRESET_PENDING = null;  // static page: [[path, value]…] to apply on 💾 Save
async function applyPreset(k) {
  const pr = D.presets.find(x => x.key === k);
  if (!confirm(`Switch this radar to the “${pr.icon} ${pr.label}” preset?\n\nIt sets domain_preset and its evidence cues (badge: ${pr.badge.si_icon} ${pr.badge.si} / ${pr.badge.sim_icon} ${pr.badge.sim}). Your pillars, keywords and marks stay as they are.`)) return;
  const rv = pr.venues.length && confirm(`Also REPLACE your venue list with the preset's ${pr.venues.length} starter venues?\n\n${pr.venues.join(', ')}\n\nOK = replace · Cancel = keep my venues`);
  if (API) {
    const r = await call('POST', '/api/config', {config: {apply_preset: {key: k, replace_venues: !!rv}}});
    if (!r.ok) return alert('Not applied: ' + r.error);
    ST = r.status; S = r.editable; buildKW(); renderAll(); baseline = snapshot(); changed();
    return toast(`Preset applied: ${r.changed.join(', ') || 'nothing changed'}. ▶ Run & Update re-scores with it.`, 6000);
  }
  const c = D.preset_changes[k];
  PRESET_PENDING = rv && c.venues ? c.venues : c.base;
  S.domain_preset = k; renderPreset(); changed();
  toast('Preset staged: 💾 Save config.json writes it.', 5000);
}
function renderPreset() {
  fill($('#c-preset'), h('h2', {}, '🧭 Domain preset', h('span', {class: 'muted small'}, 'IC design is the default; the radar works for other fields too')),
    h('div', {class: 'dgrid'}, D.presets.map(pr => h('div', {class: 'dcard' + (S.domain_preset === pr.key ? ' on' : '')},
      h('span', {class: 'di'}, pr.icon), h('b', {}, pr.label), h('span', {class: 'muted small'}, pr.sources),
      h('span', {class: 'small'}, `${pr.badge.si_icon} ${pr.badge.si} · ${pr.badge.sim_icon} ${pr.badge.sim}`),
      S.domain_preset === pr.key ? h('span', {class: 'tag'}, 'active') : h('button', {class: 'btn small', onclick: () => applyPreset(pr.key)}, 'Apply')))),
    howBox('A venue with "source": "pubmed" (NCBI E-utilities; "query" = a PubMed term, empty = your strongest keywords) or "biorxiv" / "medrxiv" (api.biorxiv.org; optional "categories") is fetched by those adapters; other venues by Crossref (ISSN) and the IEEE API.'));
}

// ---------- v3.0: full-text search (FTS5, bm25) ----------
let SEARCH_T = null;
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
function renderSearch() {
  const out = h('div', {id: 'sresults'});
  const inp = ta({class: 'in wide', placeholder: 'Search the whole store: title ×5, 🏷️ notes ×3, index terms, authors, abstract (e.g. “bootstrapped switch”, Makinwa)…'},
    {single: ' ', enter: () => { clearTimeout(SEARCH_T); doSearch(inp.value, out); }});
  inp.addEventListener('input', () => { clearTimeout(SEARCH_T); SEARCH_T = setTimeout(() => doSearch(inp.value, out), 250); });
  fill($('#c-search'), h('h2', {}, '🔎 Search the store', h('span', {class: 'muted small'}, ST.store.fts ? 'SQLite FTS5 · bm25 · milliseconds' : 'LIKE search (no FTS5 index yet)')),
    API ? [inp, out] : h('p', {class: 'muted small'}, 'Needs the Control Center server (Start_Radar_GUI / briefing.py --gui, or the radar\'s sync server). In a terminal: python briefing.py --recall "<words>".'));
}
async function doSearch(q, out) {
  if (!q.trim()) return fill(out);
  const r = await call('GET', '/api/search?q=' + encodeURIComponent(q));
  if (!r.ok) return fill(out, h('p', {class: 'warn small'}, r.error || 'search failed'));
  const mark = t => esc(t || '').replace(/\[/g, '<mark>').replace(/\]/g, '</mark>');
  fill(out, h('p', {class: 'muted small'}, `${r.results.length} best matches (${r.mode})`),
    h('table', {class: 'mini'}, h('tbody', {}, r.results.map(x => h('tr', {},
      h('td', {class: 'small'}, `${x.venue || ''} ${(x.published || '').slice(0, 4)}`),
      h('td', {}, h('a', {href: 'https://doi.org/' + x.doi, target: '_blank', rel: 'noopener'}, x.title),
        h('div', {class: 'muted small', html: mark(x.snip)}), x.user_note ? h('div', {class: 'small'}, '🏷️ ' + x.user_note) : null),
      h('td', {class: 'small'}, [x.user_mark === 'selected' ? '✅' : '', x.cited ? '⭐' : '', x.user_mark === 'archived' ? '🗄️' : '', x.status].join(' ')))))));
}

// ---------- v3.0.1: cite this software (same as CITATION.cff) ----------
const BIBTEX = `@software{jdong_engineer_paper_radar_2026,
  author    = {jdong-p},
  title     = {{Engineer Paper Radar: Automated Literature Intelligence, Verification Filtering, and Federated Curation}},
  year      = {2026},
  version   = {3.0.2},
  url       = {https://github.com/jdong-p/engineer-paper-radar},
  license   = {MIT},
  note      = {Contributors: Daidingxx}
}`;
function citeModal() {
  modal('📖 Cite Paper Radar', h('div', {}, h('p', {class: 'small'}, 'If the radar helped your research, a citation (or a ⭐) is the best thanks. GitHub also offers “Cite this repository” from CITATION.cff.'),
    h('pre', {class: 'bib'}, BIBTEX)),
    [h('button', {class: 'btn primary', onclick: () => { navigator.clipboard && navigator.clipboard.writeText(BIBTEX).then(() => toast('BibTeX copied'), () => toast('Copy failed: select the text')); }}, '📋 Copy BibTeX'),
     h('button', {class: 'btn', onclick: () => { $('#modal').hidden = true; }}, 'Close')]);
}

function renderAll() { renderStatus(); renderProfile(); renderPreset(); renderRepo(); renderSchedule(); renderPlan(); renderThemes(); renderPeople(); renderTopic(); renderSearch(); renderAdvanced(); renderDeploy(); renderSteps(); lockRun(); }
window.addEventListener('beforeunload', e => { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } });

// ---------- v3.2.0: update banners + ⏰ Friday schedule ----------
let UPD = null, UPD_BUSY = false;
async function checkUpdates(force) {
  if (!API || UPD_BUSY) return; UPD_BUSY = true;
  if (force) toast('🔍 Asking GitHub…', 2500);
  let r; try { r = await call('GET', '/api/check_updates' + (force ? '?force=1' : '')); } catch (e) { r = null; } finally { UPD_BUSY = false; }
  if (!r || !r.ok) { if (force) toast('Could not check for updates' + (r && r.error ? ': ' + r.error : '')); return; }
  UPD = r; renderUpdates();
  if (force) { const n = updCount(); toast(n ? `${n} update${n === 1 ? '' : 's'} available` : '✅ Everything is up to date' + (r.code.state === 'offline' ? ' (GitHub not reachable)' : ''), 4000); }
}
function updCount() { return UPD ? (UPD.code.state === 'behind' || UPD.code.state === 'zip' ? 1 : 0) + UPD.repos.filter(x => x.state === 'behind').length : 0; }
function renderUpdates() {
  const el = $('#updbanner'); if (!el) return;
  if (!UPD) return fill(el);
  const c = UPD.code, behind = UPD.repos.filter(x => x.state === 'behind'), out = [];
  const when = h('span', {class: 'muted small'}, `checked ${String(UPD.checked || '').replace('T', ' ')}`, UPD.cached ? ' (cached)' : '');
  if (c.state === 'behind')
    out.push(h('div', {class: 'updb'}, h('b', {}, `🚀 New Radar Version Available (${c.behind ? c.behind + ' new commit' + (c.behind === 1 ? '' : 's') : 'new commits'} on GitHub)`),
      runBtn('self_update', null, true), when));
  else if (c.state === 'zip')
    out.push(h('div', {class: 'updb'}, h('b', {}, '📦 This copy came from a ZIP and cannot see new versions'),
      runBtn('self_update', null, true, '⬆️ Update Radar Now (turn it into a git clone)'),
      h('span', {class: 'muted small'}, 'config.json, your paper store and output/ are kept as they are')));
  if (behind.length)
    out.push(h('div', {class: 'updb repo'}, h('b', {}, `🔔 Shared Repo Update Available (${behind.map(x => x.label).join(', ')} ${behind.length === 1 ? 'has' : 'have'} new commits)`),
      runBtn('pull_data_repo', null, true, '📥 Pull Updates Now'), when));
  fill(el, out);
  lockRun();
}
function renderSchedule() {
  const el = $('#c-schedule'); if (!el) return;
  const sc = S.schedule = S.schedule || {auto_update_local: true, update_time: '07:00', auto_upload_repo: true, upload_time: '12:00'};
  const tasks = (ST.schedule && ST.schedule.tasks) || [], tn = slot => (tasks.find(t => t.slot === slot) || {});
  const warn = h('p', {class: 'warn small', id: 'schedwarn'});
  const chk = () => { warn.textContent = sc.auto_update_local && sc.auto_upload_repo && sc.upload_time <= sc.update_time
    ? `⚠️ The upload (${sc.upload_time}) should come after the local update (${sc.update_time}), or it carries last week’s marks.` : ''; };
  const slot = (key, tkey, title, desc, task) => {
    const box = h('input', {type: 'checkbox', checked: !!sc[key], onchange: e => { sc[key] = e.target.checked; chk(); changed(); }});
    const tm = h('input', {class: 'in', type: 'time', value: sc[tkey], step: 60, 'aria-label': title + ' time',
      oninput: e => { if (e.target.value) { sc[tkey] = e.target.value; chk(); changed(); } }});
    return h('div', {class: 'slot'}, h('label', {}, box, ' ', h('b', {}, title)), tm,
      h('div', {class: 'small muted'}, desc, task.name ? h('span', {}, ' · task ', h('code', {}, task.name), task.daily ? ' (every day; Fridays as below)' : '') : null));
  };
  fill(el, h('h2', {}, '⏰ Friday Schedule ', h('span', {class: 'daybadge'}, '📅 Every Friday')),
    howBox('Two slots, so the upload carries the marks you make on Friday morning. Slot 1 pulls the latest shared corpus and peer overlays (if a shared repo or read repos are set up), then builds the Friday report locally: nothing is pushed then. Slot 2 runs --sync (imports offline “Save Marks” files) and then --sync-data-repo: the clean corpus' + (S.data_repo.share_tags !== false ? ' and your tags (peers/<peer_id>.json)' : '') + ' go to your private shared repo. “Save & Apply” registers the tasks with the OS (Windows Task Scheduler: runs when available, also on battery; macOS / Linux: crontab). Unticked slots are disabled.'),
    slot('auto_update_local', 'update_time', 'Slot 1 · Local update & pull shared repo', 'pull shared repos, then the Friday briefing (briefing.py --friday); no upload', tn('update')),
    slot('auto_upload_repo', 'upload_time', 'Slot 2 · Upload to private shared repo', 'briefing.py --sync, then --sync-data-repo (clean corpus' + (S.data_repo.share_tags !== false ? ' + Friday marks' : '') + ')', tn('upload')),
    warn,
    h('div', {class: 'runbar'}, runBtn('apply_schedule', null, true),
      h('span', {class: 'muted small'}, (ST.schedule && ST.schedule.os === 'cron') ? 'writes your crontab (lines marked # paper-radar:)' : 'registers / updates Windows scheduled tasks for your user')));
  chk();
}

// ---------- saving ----------
function setPath(o, path, v) { for (const k of path.slice(0, -1)) { if (typeof o[k] !== 'object' || o[k] === null) o[k] = {}; o = o[k]; } o[path[path.length - 1]] = v; }
async function save() {
  const u = collect();
  if (STARTER && u.pillars.some(x => !x)) return alert('Every pillar needs a name.');
  if (API) {
    let r; try { r = await call('POST', '/api/config', {config: u}); } catch (e) { return alert('The radar server did not answer: ' + e); }
    if (!r.ok) return alert('Not saved: ' + r.error);
    ST = r.status; S = r.editable; buildKW(); renderAll(); baseline = snapshot(); changed();
    toast(r.changed.length ? 'Saved config.json: ' + r.changed.join(', ') : 'No changes to save');
    if (r.clone_needed) { toast('📦 Cloning your shared repo next to the radar folder…', 5000); return run('clone_data_repo'); }
    return;
  }
  const raw = clone(D.raw);
  for (const [k, v] of Object.entries(u)) {
    if (k === 'data_repo' || k === 'arxiv' || k === 'schedule') for (const [kk, vv] of Object.entries(v || {})) setPath(raw, [k, kk], vv);
    else setPath(raw, D.paths[k], k === 'positive_keywords' ? Object.fromEntries(v) : v);
  }
  for (const [path, v] of PRESET_PENDING || []) if (path.join('.') !== 'domain_preset') setPath(raw, path, v);
  const text = JSON.stringify(raw, null, 4) + '\n';
  if (window.showSaveFilePicker) {
    try {
      const fh = await showSaveFilePicker({suggestedName: 'config.json', types: [{description: 'JSON', accept: {'application/json': ['.json']}}]});
      const w = await fh.createWritable(); await w.write(text); await w.close();
      baseline = snapshot(); changed();
      return toast(`Saved. It must be the config.json in ${D.here}`, 6000);
    } catch (e) { if (e.name === 'AbortError') return; }
  }
  const a = h('a', {href: URL.createObjectURL(new Blob([text], {type: 'application/json'})), download: 'config.json'});
  document.body.append(a); a.click(); a.remove();
  baseline = snapshot(); changed(); PRESET_PENDING = null;
  toast(`Downloaded config.json: move it into ${D.here} (replace the old one), then run the radar.`, 8000);
}

// ---------- connect: the page's own server (--gui / sync server), else the radar's sync hosts ----------
async function tryBase(base) {
  try {
    const hd = {}; if (base === location.origin) hd['X-Radar-GUI'] = '1';
    if (D.sync && D.sync.token) hd['X-Radar-Token'] = D.sync.token;
    const r = await fetch(base + '/api/config', {headers: hd, credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(3000)});
    if (!r.ok) return null; const j = await r.json(); return j.ok ? j : null;
  } catch (e) { return null; }
}
async function connect() {
  const bases = [];
  if (/^https?:$/.test(location.protocol)) bases.push(location.origin);
  for (const host of (D.sync && D.sync.hosts) || []) bases.push(`http://${host}:${D.sync.port}`);
  for (const b of [...new Set(bases)]) {
    const j = await tryBase(b);
    if (j) {
      API = b; ST = j.status; S = j.editable; buildKW(); renderAll(); AUTO = false; baseline = snapshot(); changed();
      const m = $('#mode'); m.textContent = '🟢 Live'; m.title = 'Connected to ' + b + ': buttons run on the radar computer';
      if (j.job && j.job.running) { JOB = {id: j.job.id, next: 0, running: true}; showConsole(`⏳ ${j.job.label}…`, []); poll(); }
      else if (j.active) streamActive(j.active);
      setTimeout(() => checkUpdates(false), 300);  // after the page is drawn: never holds the load
      return;
    }
  }
  AUTO = false;
  const m = $('#mode'); m.textContent = '⚪ Static'; m.title = 'Opened as a file: edits are saved as a downloaded config.json; the run buttons show the commands';
  if (location.protocol === 'file:' && STARTER) {
    try {
      const r = await fetch(`http://127.0.0.1:${D.gui_port}/ping`, {signal: AbortSignal.timeout(1200)});
      if (r.ok) { m.textContent = '🟡 GUI running'; m.title = 'The Control Center server is running: open it from its window, or this link'; m.onclick = () => { location.href = `http://127.0.0.1:${D.gui_port}/STARTUP.html`; }; }
    } catch (e) {}
  }
}
buildKW(); renderAll(); baseline = snapshot(); changed(); connect();
if (/^#(c-[a-z]+|s[1-4])$/.test(location.hash)) setTimeout(() => openTo(location.hash), 50);
// v3.0.7: the dashboard's ＋ Add Pillar / ＋ New Topic Dive pills land in the guided flows
if (location.hash === '#add-theme') setTimeout(() => { openTo('#c-plan'); addPillarWizard(); }, 300);
if (location.hash === '#topic-dive') setTimeout(() => openTo('#c-topic', '#topicq'), 300);
"""


# ---------- the --gui server (127.0.0.1 only, a key per launch) ----------

PAGE_TYPES = {".html": "text/html; charset=utf-8", ".bib": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}


def page_file(cfg, path, default=PAGE):
    """URL path → a .html/.bib/.md file in the output folder or its Weekly_Briefings / Topic_Dives / Peers folders."""
    root = Path(cfg["paths"]["onedrive_root"]).resolve()
    allowed = {root, Path(cfg["paths"]["weekly_dir"]).resolve(), Path(cfg["paths"]["topic_dir"]).resolve(),
               (root / "Peers").resolve()}
    rel = urllib.parse.unquote(path).lstrip("/") or default
    if "\\" in rel or ":" in rel or "\0" in rel:
        return None
    try:
        f = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if f.parent not in allowed or f.suffix.lower() not in PAGE_TYPES or not f.is_file():
        return None
    return f


class GuiHandler(BaseHTTPRequestHandler):
    server_version = "RadarGUI"

    def _send(self, code, body, ctype, headers=()):
        body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _host_ok(self):
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"{a}:{port}" for a in getattr(self.server, "allowed", ("127.0.0.1", "localhost"))}

    def _cookie_ok(self):
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "radar_gui" in c and secrets.compare_digest(c["radar_gui"].value, self.server.key)

    def _api_ok(self):
        return self._cookie_ok() and self.headers.get("X-Radar-GUI") == "1"

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain; charset=utf-8")
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/ping":  # lets a page opened as a file see that the GUI is running; no data
            return self._send(200, '{"ok": true, "gui": true}', "application/json",
                              [("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Private-Network", "true")])
        q = urllib.parse.parse_qs(u.query)
        if q.get("k"):
            if not secrets.compare_digest(q["k"][0], self.server.key):
                return self._send(401, "Wrong key: use the link printed in the Control Center window.", "text/plain; charset=utf-8")
            self.send_response(303)
            self.send_header("Set-Cookie", f"radar_gui={self.server.key}; Path=/; HttpOnly; SameSite=Strict")
            self.send_header("Location", "/" + (u.path.lstrip("/") or PAGE))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._cookie_ok():
            return self._send(401, f"{NAME} Control Center: open the link printed in its window (it holds this "
                                   "launch's key), or start it again with Start_Radar_GUI / python briefing.py --gui.",
                              "text/plain; charset=utf-8")
        if u.path.startswith("/api/"):
            if not self._api_ok():
                return self._json(403, {"ok": False, "error": "missing X-Radar-GUI header"})
            code, obj = api_get(u.path, q, self.server.jobs)
            return self._json(code, obj)
        cfg = load_cfg()
        if u.path in ("", "/", "/" + PAGE):
            try:
                write_startup(cfg, log=lambda m: None)
            except Exception:
                pass
        f = page_file(cfg, u.path)
        if not f:
            return self._send(404, "Not found.", "text/plain; charset=utf-8")
        self._send(200, f.read_bytes(), PAGE_TYPES[f.suffix.lower()])

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain; charset=utf-8")
        path = urllib.parse.urlsplit(self.path).path
        if not self._api_ok():
            return self._json(403, {"ok": False, "error": "not authorized"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"ok": False, "error": "expected application/json"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                return self._json(413, {"ok": False, "error": "too large"})
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"ok": False, "error": "invalid JSON"})
        code, obj = api_post(path, req, self.server.jobs)
        self._json(code, obj)

    def log_message(self, *args):
        pass


def serve_gui(cfg, port=None, open_browser=True):
    """python briefing.py --gui [PORT]: the Control Center on http://127.0.0.1 until Ctrl+C. In the 🌐 mesh_lan
    deployment it also listens on the private-LAN / Tailscale IPs listed in sync_server.hosts (only such
    addresses, never 0.0.0.0), each behind the same per-launch key. The key links also go to
    _system/gui_link.txt (for a server started at logon without a console). Returns 0."""
    write_startup(cfg, log=lambda m: None)
    srv = None
    for p in ([port] if port else [GUI_PORT, 0]):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), GuiHandler)
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"[ERROR] port {port} on 127.0.0.1 is in use: choose another one (--gui PORT)")
    srv.key, srv.jobs, srv.allowed = secrets.token_urlsafe(24), Jobs(), ("127.0.0.1", "localhost")
    servers = [srv]
    if deployment(cfg) == "mesh_lan":
        for h in (cfg.get("sync_server") or {}).get("hosts") or []:
            if not _lan_ip(h):
                continue
            try:
                extra = ThreadingHTTPServer((h, srv.server_address[1]), GuiHandler)
            except OSError as e:
                print(f"[WARN] --gui: can't listen on {h}: {e}", file=sys.stderr)
                continue
            extra.key, extra.jobs, extra.allowed = srv.key, srv.jobs, (h,)
            threading.Thread(target=extra.serve_forever, daemon=True).start()
            servers.append(extra)
    links = [f"http://{s.server_address[0]}:{s.server_address[1]}/?k={srv.key}" for s in servers]
    url = links[0]
    try:
        import curation
        (curation.system_dir(cfg) / "gui_link.txt").write_text(
            f"{NAME} Control Center (this launch only; don't share these links):\n" + "\n".join(links) + "\n",
            encoding="utf-8")
    except Exception:
        pass
    if sys.stdout is None:  # pythonw at logon: the links are in _system/gui_link.txt
        try:
            srv.serve_forever()
        finally:
            for s in servers:
                s.server_close()
        return 0
    print(f"{NAME} — Setup & Control Center\n"
          + "".join(f"  {u}\n" for u in links)
          + ("Opening it in your browser. " if open_browser else "Open this link in your browser. ")
          + "Keep this window open while you use the page; Ctrl+C (or closing it) stops it.\n"
          + ("The links work until this window is closed. The LAN / Tailscale ones hold this launch's key: "
             "don't share them." if len(links) > 1 else
             "The link works only on this computer, and only until this window is closed."), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        for s in servers:
            s.server_close()
    return 0
