"""Engineer Paper Radar (formerly IC Paper Radar) — the radar behind briefing.py (which checks the Python version first); one progressive report per week, finalized on Friday.

  python briefing.py [--auto-degrade | --require-vpn] [--fetch-pdf] [--finalize] [--dry-run]
                     [--no-claude] [--no-backfill]
  python briefing.py --add "<DOI or title>" [--pillar N] [--cite]   pin a paper into the catalog (pins.py)
  python briefing.py --seed-pillars [N]   best N archive papers per pillar since catch_up.since + Beyond board, now
  python briefing.py --rescore            re-score every paper (after a keyword/pillar change)
  python briefing.py --lineage            conference ↔ journal lineage over every catalog paper, now
  python briefing.py --sync               apply marks, refill the Beyond board (digests only its new papers)
  python briefing.py --select / --unselect / --cite / --uncite / --archive / --unarchive <DOI or text>
  python briefing.py --export-share [DST] [--include-curation]   clean share copy of the store (also every Friday)
  python briefing.py --merge-store OTHER.sqlite [--peer NAME] / --adopt-external   bring in someone else's store
  python briefing.py --sync-data-repo     shared corpus repo: fetch + reset, merge, export, push (also on Fridays)
  python briefing.py --pull-data-repo     shared corpus repo, read only: fetch + reset, peers, merge (no push)
  python briefing.py --gui [PORT]         Setup & Control Center (STARTUP.html) on http://127.0.0.1 (control_center.py)

Every day (scheduled 07:00): collect (0 tokens) → digest only the *new* candidates (cached per DOI,
never re-done) → rewrite this week's file Weekly_Briefings/<YYYY-Www>.md ("in progress").
Friday (or --finalize): key prior work + citation map + one synthesis call → "final"; the week's
papers become `reported` and never appear again.

  (default)       no VPN check: collection uses public APIs only, so no popup and no banner
  --require-vpn   check your campus VPN (config.json → vpn); down → popup + exit 2
  --fetch-pdf     check your campus VPN; down → popup + banner, today's top papers go to the PDF queue
  --auto-degrade  kept for old command lines; same as the default
  --dry-run       work on a copy of the store, write to IC_Paper_Radar\\_archive\\_dryrun\\
  --no-claude     0 tokens: 🧮 0-token draft digests only (= --claude-mode draft), no synthesis
  --claude-mode draft|hybrid|full   v3.4.0 (drafter.py): 🧮 drafts only · 🔍 drafts + a minimal haiku audit of the
                  uncertain ones (default, config claude.mode) · 🤖 a full Claude digest for every new paper
  --refine DOI    ✨ AI Refine: a full Claude digest for this one paper now
  --draft-eval    0 tokens: drafts vs the stored en-v3 digests (accuracy, routing, token estimate); changes nothing
Reports are always in English (ground rule).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from env_guard import ensure_minisforum_env

ensure_minisforum_env()

import arxiv_source
import catalog
import curation
import data_repo
import drafter
import fetcher
import html_renderer
import pins
import presets
import relations
import run_lock
import topic
import store as store_mod
import vpn_checker
from store import Store

HERE = Path(__file__).resolve().parent
PROMPT_VER = "starter-v1"  # bump to re-digest everything with a new prompt
# en-v2 / en-v3 = the pre-loaded digests: kept, not redone (catalog.pillar_of ignores their Pillar line)
DIGEST_VERS_OK = {PROMPT_VER, "en-v2", "en-v3"}


def prompt_ver(cfg, base=None):
    """digest_ver of a new digest (v3.0.1): PROMPT_VER for the default ic_ece preset, so its prompts and every cached
    IC digest stay exactly as they were; '<ver>+<preset>' for bio_med / chem_mat / general_eng, whose prompts
    presets.adapt_prompt rewrites. A digest written under another preset is kept, never redone."""
    base = base or PROMPT_VER
    k = presets.key(cfg)
    return base if k == presets.DEFAULT else f"{base}+{k}"


def digest_done(p, cfg):
    return p.get("digest_ver") in DIGEST_VERS_OK or str(p.get("digest_ver") or "").split("+")[0] == PROMPT_VER

# claude() replaces this with the pillars from config.json, e.g. "(1) SAR ADCs; (2) PLLs"
PILLARS = "@@PILLARS@@"


def pillars_text(cfg):
    return "; ".join(f"({i}) {name}" for i, name in enumerate(cfg.get("pillars") or [], 1)) or "(none configured)"

DIGEST_PROMPT = """You are a senior analog/mixed-signal IC designer writing a reading digest for a PhD
researcher whose research pillars are """ + PILLARS + """. Write in English.
For each paper below use ONLY its title and abstract. Never invent numbers: write "not stated" when a
metric is missing. Measured silicon results matter most to this reader; simulation-only work counts
only if its numbers or topology are exceptional.

Output exactly this for every paper, in the given order, and nothing else (no preamble):

=== <doi> ===
- **Verification:** `✅ Silicon-Verified (<process>, <area>)` if the abstract reports fabricated/measured
  silicon; `⚠️ Simulation-Only (<why it may still be worth reading, or "no exceptional metric">)` if only
  simulations are reported; `❔ Not stated` otherwise. The JSON field "verification_hint" is a keyword
  heuristic; trust the abstract over it.
- **Pillar:** which of the pillars it serves, starting with its number (e.g. "2 — …"), or "other", in a few words
- **Core innovation:** 1–2 sentences
- **Architecture & key metrics:** Process · Power · FoM · ENOB or SNR/SNDR · Area · other headline numbers
  (inaccuracy, resolution FoM, efficiency, etc.)
- **Suggested focus:** e.g. circuit implementation / measurement / calibration / system architecture, with a one-line reason
- **📈 Conference → Journal Lineage & Delta:** ONLY for papers that have a "conference_precursor" field:
  (1) which conference paper it extends ("<venue> <year>: <title>"); (2) what the journal version adds over
  that 4-page conference brief — deeper circuit implementation, calibration, analysis/derivations, extended
  measurements (more chips, corners, temperature range) — judged by comparing the two abstracts. Say
  plainly when the abstracts don't reveal the delta. Omit this bullet entirely for other papers.

Papers (JSON):
{papers}
"""

BEYOND_VER = "beyond-v1"
BEYOND_PROMPT = """You are a senior analog/mixed-signal IC designer scouting non-IEEE journals (Nature /
Science portfolio) for ideas, for a PhD researcher whose pillars are """ + PILLARS + """. Write in English.
Use ONLY each paper's title and abstract; never invent numbers ("not stated" when missing).

Output exactly this for every paper, in the given order, and nothing else (no preamble):

=== <doi> ===
- **What it is:** 1–2 sentences on the transducer / system / bioelectronic concept and the headline result
- **Hardware & numbers:** sensing principle · electronics used (custom IC? discrete? off-the-shelf?) ·
  power / energy · size · key performance numbers
- **💡 System Concept & IC Takeaway:** why the concept is interesting, and concretely how a CMOS
  ultra-low-power / sensor-interface designer could leverage it or build a better IC readout for it
  (e.g. required noise, bandwidth, power budget, interface type)

Papers (JSON):
{papers}
"""

SYNTH_PROMPT = """You are a senior analog/mixed-signal IC designer. Below are this week's selected papers
(title, venue, digest, citation relations, keyword hits). The reader's research pillars are
""" + PILLARS + """. Write the week's synthesis in English, max ~550 words, Markdown, starting directly
with "### By pillar" and nothing before it:

### By pillar
One sub-bullet list per pillar (in the order above, plus "Other" only if needed): the
technical threads, citing papers as #number. Write
"nothing this week" for an empty pillar.
### Head-to-head (silicon-verified state of the art)
A Markdown table of the papers that address comparable problems, with columns Paper · Pillar · Process ·
Power · Key metric (FoM / inaccuracy / efficiency) · Area · Verification. Use only numbers from the
digests. Where a paper has "verification_confirmed_by_reader", that value (from the PDF) replaces the
digest's Verification line. Silicon-verified papers first; mark simulation-only rows with ⚠️ and never present their
numbers as state of the art.
### Reading order
The 3 papers to read first (#number + title) and why.
### Signals
1–3 bullets: VIP-author or anchor-citing work (see "hits"), conference → journal extensions (see
"lineage"), and anything that changes the state of the art for the reader's pillars.
### Beyond IEEE
1–2 bullets on the "beyond_ieee" papers (Nature/Science), only as inspiration for IC work; omit the
heading if that list is empty.

Context (facts, don't contradict them): {context}

Papers (JSON):
{papers}
"""


def claude_available(cfg):
    return shutil.which(cfg["claude"]["command"]) is not None


def claude(prompt, cfg, model=None):
    """One `claude -p` call (no tools, no MCP). model: e.g. 'haiku' for the 🔍 audit (claude.audit_model)."""
    cc = cfg["claude"]
    prompt = presets.adapt_prompt(prompt.replace(PILLARS, pillars_text(cfg)), cfg)  # ic_ece: unchanged
    cmd = [cc["command"], "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"]
    if model or cc.get("model"):
        cmd += ["--model", model or cc["model"]]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=cc["timeout_s"], cwd=HERE, shell=(sys.platform == "win32"))
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude -p failed (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    return r.stdout.strip()


def _precursor(p):
    link = next((l for l in p.get("lineage") or [] if l["type"] == "journal_extension_of"), None)
    return {"venue": link["venue"], "year": link["year"], "title": link["title"],
            "abstract": link.get("abstract") or "(not available)"} if link else None


def _run_digests(todo, prompt, ver, payload_fn, store, cfg, log, what, src="claude"):
    if not todo:
        return 0
    log(f"Calling claude -p to digest {len(todo)} {what} ...")
    text = prompt.format(papers=json.dumps([payload_fn(p) for p in todo], ensure_ascii=False, indent=1))
    out = claude(text, cfg)
    _ledger(store, "full", len(todo), len(text), len(out))
    parts = re.split(r"^=== *(\S+?) *===\s*$", out, flags=re.M)
    got = {parts[i].lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    for p in todo:
        if got.get(p["doi"]):
            store.update(p["doi"], {"digest": got[p["doi"]], "digest_ver": ver, "digest_src": src, "draft_conf": None})
    missing = [p["doi"] for p in todo if not got.get(p["doi"])]
    if missing:
        log(f"  digest missing for {missing} (retried next run)")
    return len(todo) - len(missing)


def _digest_payload(p):
    d = {"doi": p["doi"], "title": p["title"], "venue": p["venue_name"], "verification_hint": _verif(p) or "unclear",
         "abstract": p.get("abstract") or "(no abstract available — title only)"}
    if _precursor(p):
        d["conference_precursor"] = _precursor(p)
    return d


def claude_mode(cfg, a=None):
    """v3.4.0: 'draft' (🧮 0 tokens) | 'hybrid' (🔍 drafts + minimal audit) | 'full' (🤖 en-v3 for every paper).
    --claude-mode beats --no-claude (= draft), which beats config.json claude.mode (default hybrid)."""
    if a is not None and getattr(a, "claude_mode", None):
        return a.claude_mode
    if a is not None and getattr(a, "no_claude", False):
        return "draft"
    return drafter.effective_mode(cfg)


def digest_ok(p, cfg, mode):
    """A digest a run keeps as it is (preset-aware, digest_done); in Full mode a draft or audit is upgraded."""
    return digest_done(p, cfg) or (mode != "full" and p.get("digest_ver") == drafter.AUDIT_VER)


def _ledger(store, kind, papers, chars_in=0, chars_out=0):
    """Per day: drafts final at 0 tokens, audited papers, full digests, and their prompt/answer size (≈ chars / 4
    tokens), for STARTUP.html's 🔑 Claude AI line and --draft-eval. Kept 60 days."""
    day = date.today().isoformat()
    led = store.meta("digest_ledger") or {}
    d = led.setdefault(day, {})
    d[kind] = d.get(kind, 0) + papers
    d[kind + "_tok_in"] = d.get(kind + "_tok_in", 0) + chars_in // 4
    d[kind + "_tok_out"] = d.get(kind + "_tok_out", 0) + chars_out // 4
    store.meta("digest_ledger", {k: led[k] for k in sorted(led)[-60:]})


def highlights(store, cfg, week):
    """R3: this run's top N candidates by score (claude.highlights_per_run, default 3), plus pinned anchors, VIP-author
    papers and ⭐ Cite: their drafts are always audited in Hybrid mode."""
    n = drafter.settings(cfg)["highlights_per_run"]
    cands = sorted(store.where("status='candidate' AND week=? AND grp='ieee'", (week,)), key=lambda p: -(p["score"] or 0))
    out = {p["doi"] for p in cands[:n]}
    out |= {p["doi"] for p in store.where("grp='ieee' AND (pin='anchor' OR COALESCE(cited,0)=1)")}
    out |= {p["doi"] for p in cands if any(str(h).startswith("vip_author:") for h in p.get("hits") or [])}
    return out


def digest_papers(todo, store, cfg, log, mode, hl=frozenset()):
    """🧮 Draft every paper in `todo` (0 tokens); in Hybrid mode send the routed ones (drafter.route) to one
    batched 🔍 audit call, highlights first, then the lowest confidence, at most claude.audits_per_day_max.
    A paper whose audit is missing or capped keeps its draft (digest_src 'draft-queued') and is retried next run."""
    s = drafter.settings(cfg)
    queue, final, new = [], 0, 0
    for p in todo:
        pre = _precursor(p)
        d = drafter.draft(p, cfg, pre)
        r, rule = drafter.route(d, cfg, p["doi"] in hl)
        audit = mode == "hybrid" and r != "final"
        src = "draft-queued" if audit else "draft"
        if (p.get("digest"), p.get("digest_ver"), p.get("digest_src")) != (d["md"], drafter.DRAFT_VER, src):
            new += 1  # a draft that is redone unchanged (each run re-routes the drafts) isn't counted or rewritten
            store.update(p["doi"], {"digest": d["md"], "digest_ver": drafter.DRAFT_VER, "draft_conf": d["conf"],
                                    "digest_src": src})
            final += not audit
        if audit:
            queue.append((p, d, r, rule, pre))
    if final:
        _ledger(store, "draft", final)
    if new or queue:
        log(f"🧮 Drafts (0 tokens): {new} new or changed, {final} final"
            + (f", {len(queue)} for the 🔍 audit" if queue else ""))
    if not queue:
        return len(todo)
    queue.sort(key=lambda x: (x[0]["doi"] not in hl, x[1]["conf"]))
    batch, later = queue[:s["audits_per_day_max"]], queue[s["audits_per_day_max"]:]
    if later:
        log(f"  {len(later)} audit(s) over claude.audits_per_day_max: they keep their draft until the next run")
    items = [drafter.audit_payload(p, d, r, pre) for p, d, r, rule, pre in batch]
    prompt = drafter.audit_prompt(items, cfg)
    rules = ", ".join(f"{k}×{v}" for k, v in sorted(_count(x[3] for x in batch).items()))
    log(f"Calling claude -p --model {s['audit_model']} to audit {len(batch)} drafts ({rules}) ...")
    try:
        out = claude(prompt, cfg, model=s["audit_model"])
    except Exception as e:
        log(f"  audit failed (drafts kept, retried next run): {e}")
        return len(todo)
    got = drafter.parse_audit(out)
    n_pass = n_patch = 0
    dropped_all = []
    for p, d, r, rule, pre in batch:
        ans = got.get(p["doi"].lower())
        if not ans:
            continue
        if ans["verdict"] == "PASS":
            n_pass += 1
            src = "audit-pass"
        else:
            d, dropped = drafter.apply_audit(d, ans, p.get("abstract") or "", cfg, (pre or {}).get("abstract") or "")
            dropped_all += [f"{p['doi']}:{k}" for k in dropped]
            n_patch += 1
            src = "audit-patch"
        store.update(p["doi"], {"digest": d["md"], "digest_ver": drafter.AUDIT_VER, "digest_src": src})
    _ledger(store, "audit", n_pass + n_patch, len(prompt), len(out))
    missing = len(batch) - n_pass - n_patch
    log(f"  🔍 audit: {n_pass} PASS, {n_patch} PATCH" + (f", {missing} missing (retried next run)" if missing else "")
        + f" · ~{len(prompt) // 4:,} input / ~{len(out) // 4:,} output tokens (est.)")
    if dropped_all:
        log(f"  patch fields rejected (number not in the abstract, or unknown key): {', '.join(dropped_all[:8])}")
    return len(todo)


def _count(xs):
    c = {}
    for x in xs:
        c[x] = c.get(x, 0) + 1
    return c


def digest_new(store, cfg, week, today, log, mode="full"):
    """Digest candidates of this week (and pinned papers) that have no current digest. Cached: never repeated
    (a newly found conference precursor resets the cache so the lineage & delta bullet is added).
    v3.4.0: Full = en-v3 for ≤ digests_per_day_max papers (the rest get a 🧮 draft meanwhile); Hybrid = drafts +
    one minimal 🔍 audit call; Draft = drafts only. Every mode then drafts catalog papers that still have no digest."""
    cands = [p for p in store.where("status='candidate' AND week=? AND grp='ieee'", (week,))
             if not digest_ok(p, cfg, mode)]
    # pinned papers (anchors, --add) last: the catalog shows them, the weekly report doesn't
    cands += [p for p in store.where("status='pinned' AND grp='ieee'") if not digest_ok(p, cfg, mode)]
    # wait 2 days for an abstract before a Claude digest (R1); a draft is written meanwhile and redone when it comes
    ready = [p for p in cands if p.get("abstract") or (today - date.fromisoformat(p["first_seen"])).days >= 2]
    ready = sorted(ready, key=lambda p: (p["status"] == "pinned", bool(p.get("catchup")), -p["score"]))  # new first
    n = 0
    if mode == "full":
        todo = ready[:cfg["digests_per_day_max"]]
        n = _run_digests(todo, DIGEST_PROMPT, prompt_ver(cfg), _digest_payload, store, cfg, log, "IEEE papers")
        done = {p["doi"] for p in todo}
        rest = [p for p in cands if p["doi"] not in done and not p.get("digest")]
        digest_papers(rest, store, cfg, log, "draft")  # no blank card while a paper waits for its turn
    else:
        hl = highlights(store, cfg, week) if mode == "hybrid" else frozenset()
        waiting = [p for p in cands if p not in ready]
        digest_papers(waiting, store, cfg, lambda m: None, "draft")  # title-only drafts, routed once the abstract comes
        n = digest_papers(ready, store, cfg, log, mode, hl)
    n += digest_beyond(store, cfg, log, mode)
    draft_backfill(store, cfg, log)
    return n


def draft_backfill(store, cfg, log):
    """🧮 A 0-token draft for every catalog paper (candidate, reported, pinned, ✅ selected, ⭐ cited) that has no
    digest at all, so the dashboard never shows 'No digest yet' (✨ AI Refine upgrades one on request)."""
    rows = [p for p in store.where("grp='ieee' AND COALESCE(digest,'')='' AND (status IN ('candidate','reported','pinned') "
                                   "OR user_mark='selected' OR COALESCE(cited,0)=1)")]
    for p in rows:
        d = drafter.draft(p, cfg, _precursor(p))
        store.update(p["doi"], {"digest": d["md"], "digest_ver": drafter.DRAFT_VER, "draft_conf": d["conf"],
                                "digest_src": "draft"})
    if rows:
        _ledger(store, "draft", len(rows))
        log(f"🧮 Backfill: {len(rows)} catalog paper(s) without a digest got a 0-token draft")
    return len(rows)


def digest_beyond(store, cfg, log, mode="full"):
    """Every paper on the Beyond-IEEE board without a digest (≤ board_size, so one call). Hybrid keeps this full
    Claude digest (few papers, narrative abstracts); Draft mode writes a 0-token draft instead."""
    todo = [p for p in store.where("grp='beyond' AND status IN ('candidate','reported') AND "
                                   "COALESCE(user_mark,'normal')='normal' AND COALESCE(cited,0)=0")
            if str(p.get("digest_ver") or "").split("+")[0] != BEYOND_VER]
    if mode == "draft":
        todo = [p for p in todo if not p.get("digest")]
        for p in todo:
            store.update(p["doi"], {"digest": drafter.draft_beyond(p, cfg), "digest_ver": drafter.DRAFT_VER,
                                    "digest_src": "draft", "draft_conf": None})
        return len(todo)
    return _run_digests(todo, BEYOND_PROMPT, prompt_ver(cfg, BEYOND_VER), _beyond_payload, store, cfg, log, "Beyond-IEEE papers")


def _beyond_payload(p):
    return {"doi": p["doi"], "title": p["title"], "venue": p["venue_name"],
            "abstract": p.get("abstract") or "(no abstract available — title only)"}


def refine(store, cfg, needle, log=print):
    """✨ AI Refine (v3.4.0): today's full Claude digest (en-v3, or beyond-v1 on the Beyond board) for one paper."""
    p = store.get(needle.strip().lower())
    if not p:
        log(f"--refine: no paper with DOI {needle!r} in the store")
        return 1
    if p.get("grp") == "beyond":
        n = _run_digests([p], BEYOND_PROMPT, prompt_ver(cfg, BEYOND_VER), _beyond_payload, store, cfg, log, "Beyond-IEEE paper", "refine")
    else:
        n = _run_digests([p], DIGEST_PROMPT, prompt_ver(cfg), _digest_payload, store, cfg, log, "paper (✨ AI Refine)", "refine")
    log(f"✨ AI Refine: {'done' if n else 'no digest in the answer (try again)'}: {p['title'][:90]}")
    return 0 if n else 1


def draft_eval(store, cfg, log=print):
    """--draft-eval (0 tokens, changes nothing): 🧮 drafts of every IEEE paper with an en-v3 digest and an abstract,
    compared with Claude's digest; the routing of each, a final-threshold sweep and a token estimate."""
    rows = [p for p in store.where("grp='ieee'") if p.get("abstract")
            and str(p.get("digest_ver") or "").split("+")[0] in (PROMPT_VER, "en-v3")]
    if not rows:
        log("--draft-eval: no Claude digests with an abstract in this store (run with Claude first)")
        return 1
    recs = drafter.evaluate(rows, cfg, _precursor)
    n = len(recs)

    def pct(a, b):
        return f"{a}/{b} ({100 * a / max(b, 1):.0f}%)"

    def summary(rs):
        return (pct(sum(r["v_ok"] for r in rs), len(rs)),
                pct(sum(r["num_recall"][0] for r in rs), sum(r["num_recall"][1] for r in rs)),
                sum(r["rouge"] for r in rs) / max(len(rs), 1))
    v, rc, rg = summary(recs)
    log(f"🧮 --draft-eval on {n} papers with an en-v3 digest (0 tokens; Claude's digest = reference)")
    log(f"  Verification verdict = Claude's: {v}")
    log(f"  Process node = Claude's:         {pct(sum(r['proc_ok'][0] for r in recs), sum(r['proc_ok'][1] for r in recs))}")
    log(f"  Metric recall (Claude's numbers that are in the abstract, found by the draft): {rc}")
    log(f"  Metric grounding (draft numbers that are in the title / abstract): "
        f"{pct(sum(r['num_prec'][0] for r in recs), sum(r['num_prec'][1] for r in recs))}")
    log(f"  Pillar = Claude's: {pct(sum(r['pil_ok'] for r in recs), n)} · core sentence ROUGE-1 F vs Claude's: {rg:.2f}")
    conf = drafter.settings(cfg)
    log(f"  Routing (final ≥ {conf['draft_final_conf']}, rewrite < {conf['draft_rewrite_conf']}, top {conf['highlights_per_run']} per week audited):")
    for rule in ("R2", "R3", "R4", "R5", "R6", "R7", "R8"):
        rs = [r for r in recs if r["route"][1] == rule]
        if rs:
            vv, rr, _ = summary(rs)
            log(f"    {rule} {rs[0]['route'][0]:7s} {len(rs):4d}  verdict ok {vv:14s} metric recall {rr}")
    log("  Final-threshold sweep (papers that would be final at 0 tokens):")
    for t in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9):
        c2 = {**cfg, "claude": {**(cfg.get("claude") or {}), "draft_final_conf": t}}
        fin = [r for r in drafter.evaluate(rows, c2, _precursor) if r["route"][0] == "final"]
        vv, rr, gg = summary(fin)
        log(f"    T={t:.2f}  final {pct(len(fin), n):14s} verdict ok {vv:14s} metric recall {rr:16s} ROUGE {gg:.2f}")
    # token estimate: Full = the en-v3 payload in and the stored digest out; Hybrid = audit payloads in, and out a
    # PASS line (~12 tokens) or a PATCH (~12 + 20 per field it would fix) or, for R5 / R8, a delta / core (~90)
    by_doi = {p["doi"]: p for p in rows}
    batch = max(1, min(cfg.get("digests_per_day_max", 12), 4))  # the radar's runs digest 1–4 papers at a time
    full_in = len(DIGEST_PROMPT) / 4 / batch * n
    full_out = 0
    hy_in, hy_out, audited = 0.0, 0.0, 0
    for r in recs:
        p = by_doi[r["doi"]]
        full_in += len(json.dumps(_digest_payload(p), ensure_ascii=False)) / 4
        full_out += len(p["digest"]) / 4
        if r["route"][0] == "final":
            continue
        audited += 1
        d = drafter.draft(p, cfg, _precursor(p))
        hy_in += len(drafter.audit_payload(p, d, r["route"][0], _precursor(p))) / 4
        nfix = (not r["v_ok"]) + (r["num_recall"][0] < r["num_recall"][1])
        hy_out += 12 + 20 * nfix + (90 if r["route"][1] in ("R5", "R8") else 0)
    hy_in += len(drafter.AUDIT_PROMPT) / 4 / batch * audited
    w = 5  # an output token weighs ~5 input tokens (speed and usage limits)
    log(f"  Token estimate for these {n} papers (≈ chars / 4; batches of {batch}; output weighted ×{w}):")
    log(f"    Full en-v3:   ~{full_in:,.0f} in + ~{full_out:,.0f} out  → weighted ~{full_in + w * full_out:,.0f}")
    log(f"    Hybrid audit: ~{hy_in:,.0f} in + ~{hy_out:,.0f} out  ({audited} audited, {n - audited} final at 0 tokens)"
        f"  → weighted ~{hy_in + w * hy_out:,.0f}")
    log(f"    Saving: input {100 * (1 - hy_in / full_in):.0f}% · output {100 * (1 - hy_out / full_out):.0f}% · "
        f"weighted {100 * (1 - (hy_in + w * hy_out) / (full_in + w * full_out)):.0f}%")
    return 0


def pillar_line(added):
    return ", ".join(f"Pillar {k} +{v}" for k, v in sorted(added.items())) or "nothing new"


def synthesis(store, cfg, week, papers, beyond, notes, force, log):
    cached = store.meta(f"synthesis:{week}")
    key = [prompt_ver(cfg), BEYOND_VER, [p["doi"] for p in papers + beyond]]
    if cached and cached["key"] == key and not force:
        return cached["text"]
    payload = {"ieee": [{"no": i, "title": p["title"], "venue": p["venue"], "digest": p.get("digest") or "",
                         "hits": p.get("hits") or [], "relations": notes.get(p["doi"], []),
                         "lineage": [_lineage_line(l) for l in p.get("lineage") or []],
                         # the reader's own call from the PDF beats the digest's Verification line
                         **({"verification_confirmed_by_reader": p["user_silicon"]} if p.get("user_silicon") else {})}
                        for i, p in enumerate(papers, 1)],
               "beyond_ieee": [{"title": p["title"], "venue": p["venue_name"], "digest": p.get("digest") or ""} for p in beyond]}
    log("Calling claude -p for the weekly synthesis ...")
    with_refs = sum(1 for p in papers if p.get("refs"))
    context = (f"{len(cfg.get('anchors', []))} anchor papers and {len(cfg.get('vip_authors', []))} VIP-author name "
               f"forms are configured. 'cites an anchor' can only appear when a paper's reference list is indexed; "
               f"this week {with_refs} of {len(papers)} papers have an indexed reference list (Early Access papers "
               f"often have none yet), so a missing anchor hit is not evidence of no citation.")
    out = claude(SYNTH_PROMPT.format(papers=json.dumps(payload, ensure_ascii=False, indent=1), context=context), cfg)
    out = out[out.find("### "):] if "### " in out else out
    store.meta(f"synthesis:{week}", {"key": key, "text": out})
    return out


def fetch_pdfs(items, cfg, log=print):
    """Best effort via the DOI's IEEE arnumber. With the split tunnel this usually gets HTML; failures stay queued."""
    out_dir = Path(cfg["paths"]["pdf_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for it in items:
        try:
            req = urllib.request.Request(f"https://doi.org/{it['doi']}", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                m = re.search(r"/document/(\d+)", r.geturl())
            if not m:
                raise RuntimeError("no IEEE arnumber")
            req = urllib.request.Request(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m[1]}",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if not data.startswith(b"%PDF"):
                raise RuntimeError("no institutional access (got HTML, not a PDF)")
            (out_dir / f"{it['doi'].replace('/', '_')}.pdf").write_bytes(data)
            log(f"  PDF ok: {it['doi']}")
        except Exception as e:
            log(f"  PDF failed: {it['doi']}: {e}")
            failed.append({**it, "last_error": str(e)})
    return failed


def _verif(p):
    if p.get("user_silicon"):  # the reader's call after reading the PDF (v2.10)
        return p["user_silicon"]
    hits = p.get("hits") or []
    return next((h for h in ("silicon_verified", "simulation_only") if h in hits), "")


BADGE = {"silicon_verified": "✅", "simulation_only": "⚠️ sim-only", "": "❔"}


def _authors(p):
    a = p.get("authors") or []
    return ", ".join(a[:4]) + (" et al." if len(a) > 4 else "") or "n/a"


def _lineage_line(l):
    if l["type"] == "journal_extension_of":
        return f"📈 Extended from {l['venue']} {l['year'] or ''}: *{l['title']}* ({l['doi']}) — signal {l['signal']}"
    return f"📈 Has a journal extension in {l['venue']} {l['year'] or ''}: *{l['title']}* ({l['doi']}) — signal {l['signal']}"


def _paper_block(head, p, today, notes):
    L = [f"### {head}. {p['title']}" + (" 🆕" if p["first_seen"] == today.isoformat() else ""),
         f"*{p['venue_name']}* · {p['published']} · {_authors(p)} · DOI [{p['doi']}](https://doi.org/{p['doi']})"
         + ("" if p.get("abstract") else " · ⚠️ no abstract yet"), ""]
    L += [p.get("digest") or "_Digest pending (next run)._", curation.line(p), ""]
    lin = p.get("lineage") or []
    if lin:
        L += ["**" + " · ".join(_lineage_line(l) for l in lin) + "**", ""]
    rel = list(notes.get(p["doi"], []))
    rel += [f"possible other version: *{r['title']}* ({r['venue']}, {r['published']}, {r['doi']})"
            for r in p.get("related") or [] if r["doi"] not in {l["doi"] for l in lin}]
    if p.get("keyrefs"):
        rel.append("key prior work: " + "; ".join(
            f"{k['first_author']} — *{k['title']}* ({k['venue'] or 'n/a'}, {k['year']}, cited {k['cited_by']}×)" for k in p["keyrefs"]))
    if rel:
        L += ["**Relations:** " + " · ".join(rel), ""]
    return L


def render(week, friday, today, vpn, papers, also, catch, beyond, notes, edges, synth, final, store, cfg, run):
    L = [f"# Engineer Paper Radar — Week {week} (ends Fri {friday:%d %b %Y})", ""]
    L.append(f"> ✅ **Final** — {today:%a %d %b %Y}" if final else
             f"> 🟡 **In progress** — updated {datetime.now():%a %d %b %Y %H:%M}; final report on Friday {friday:%d %b}.")
    if vpn["connected"] is False:  # None = not checked (default daily run)
        L += ["", "> ⚠️ **[VPN Disconnected - Abstract-Only Mode]** Reconnect your campus VPN on this computer. "
              "Today's top papers were queued in `pending_pdf_queue.json`."]
    c = store.counts()
    L += ["", f"**Network:** {vpn['message']}  ",
          f"**This week:** {len(papers) + len(also)} matched papers + {len(catch)} catch-up · today +{run['new']} new records, "
          f"+{len(run['new_candidates'])} matches · IEEE API {run['ieee_calls']}/{cfg['ieee_daily_budget']} calls today  ",
          f"**Corpus:** {sum(c.values())} papers ({c.get('archive', 0)} backfilled, {c.get('reported', 0)} reported, "
          f"{c.get('pending', 0)} waiting for an abstract)", ""]
    if run["errors"]:
        L += ["**Fetch errors:** " + "; ".join(run["errors"]), ""]
    if synth:
        L += ["## Week synthesis", "", synth, ""]
    if not papers and not catch:
        return "\n".join(L + ["_No matching papers yet this week._", ""])

    def table(rows, numbered, added_col):
        out = [f"| {numbered} | Score | {presets.badge(cfg)['column']} | Venue | Title | Cited by | {added_col} | Hits |",
               "|---|---|---|---|---|---|---|---|"]
        for i, p in enumerate(rows, 1):
            new = " 🆕" if p["first_seen"] == today.isoformat() else ""
            cites = max(p.get("cited_by") or 0, p.get("ieee_cites") or 0)
            hits = ", ".join(f"**{h}**" if h.startswith(("vip_author", "cites an anchor")) else h
                             for h in p["hits"] if h not in ("silicon_verified", "simulation_only"))
            when = p["first_seen"][5:] if added_col == "Added" else p["published"]
            out.append(f"| {numbered.replace('#', '') + str(i)} | {p['score']} | {BADGE[_verif(p)] if presets.key(cfg) == 'ic_ece' else presets.icon(_verif(p), cfg)} | {p['venue']} "
                       f"| [{p['title']}](https://doi.org/{p['doi']}){new} | {cites} | {when} | {hits} |")
        return out

    if papers:
        L += ["## Ranked papers", ""] + table(papers, "#", "Added")
        graph = relations.mermaid(papers, edges)
        if graph:
            L += ["", "## Citation map", "", "Solid arrow = cites; dotted line = shared references.", "", graph]
        L += ["", "## Papers", ""]
        for i, p in enumerate(papers, 1):
            L += _paper_block(i, p, today, notes)
    else:
        L += ["_No new matching papers yet this week._", ""]
    if also:
        L += ["## Also matched (not in the top list)", "", "| Score | Venue | Title |", "|---|---|---|"]
        L += [f"| {p['score']} | {p['venue']} | [{p['title']}](https://doi.org/{p['doi']}) |" for p in also]
    if catch:
        L += ["", "## ⏪ Catch-up: recent papers the radar filed before it saw them as new", "",
              f"_Published since {cfg['catch_up']['since']} but backfilled into the corpus, so no earlier report "
              f"showed them. The best {cfg['catch_up']['per_pillar']} of each pillar per week (flagship venues "
              f"rank higher); `--seed-pillars N` pulls more._", ""]
        L += table(catch, "C", "Published") + [""]
        for i, p in enumerate(catch, 1):
            L += _paper_block(f"C{i}", p, today, notes)
    L += render_beyond(beyond, today)
    return "\n".join(L + [""])


def render_beyond(beyond, today):
    if not beyond:
        return []
    L = ["", "## 🌍 Beyond IEEE: Cross-Disciplinary & Bioelectronics Highlights (Nature / Science)", "",
         "_Inspiration, not circuits papers: passed the dual-key gate (application theme + hardware/IC term)._", ""]
    for i, p in enumerate(beyond, 1):
        gate = ", ".join(h for h in p["hits"] if h.startswith(("theme:", "hw:")))
        L += [f"### 🌍 {i}. {p['title']}" + (" 🆕" if p["first_seen"] == today.isoformat() else ""),
              f"*{p['venue_name']}* · {p['published']} · {_authors(p)} · DOI [{p['doi']}](https://doi.org/{p['doi']})"
              + ("" if p.get("abstract") else " · ⚠️ no abstract (title-only gate)") + f" · gate: {gate}", "",
              p.get("digest") or "_Digest pending (next run)._", curation.line(p), ""]
    return L


def rerender_week(store, cfg, today, log=print):
    """v3.4.2: after a pillar 🏺 retire / 🗑 delete / ↺ restore, re-render this week's in-progress report (0 tokens,
    cached digests, the last collection's numbers from meta['last_run']) so 📄 Open Weekly Report shows the new
    pillars now, not after tomorrow's run. A week already marked ✅ Final is never touched."""
    week, friday = fetcher.week_of(today)
    out = Path(cfg["paths"]["weekly_dir"]) / f"{week}.md"
    if not out.exists() or "✅ **Final**" in out.read_text(encoding="utf-8"):
        return False
    ieee_q = ("status='candidate' AND week=? AND grp='ieee'", (week,))
    allc = sorted(store.where(*ieee_q), key=lambda p: (-p["score"], p["first_seen"]))
    fresh = [p for p in allc if not p.get("catchup")]
    catch = [p for p in allc if p.get("catchup")]
    papers, also = fresh[:cfg["weekly_top"]], fresh[cfg["weekly_top"]:]
    all_beyond = sorted(store.where("status='candidate' AND week=? AND grp='beyond'", (week,)), key=lambda p: -p["score"])
    beyond = [p for p in all_beyond if (p.get("user_mark") or "normal") != "archived"]
    edges, notes = relations.week_relations(papers, store)
    notes.update(relations.week_relations(catch, store)[1])
    run = store.meta("last_run") or {}
    run = {"new": run.get("new", 0), "new_candidates": run.get("new_candidates") or [],
           "ieee_calls": run.get("ieee_calls", store.calls_today("ieee")), "errors": run.get("errors") or []}
    vpn = {"connected": None, "message": "public APIs only (re-rendered after a pillar change, 0 tokens)"}
    out.write_text(render(week, friday, today, vpn, papers, also, catch, beyond, notes, edges, "", False, store, cfg, run),
                   encoding="utf-8")
    log(f"Weekly report {week} re-rendered (still in progress, 0 tokens): the pillar change shows there now")
    return True


def update_index(weekly_dir, topic_dir=None, topics=None):
    rows = ["# Engineer Paper Radar — Index", "", "**[DASHBOARD.html](DASHBOARD.html)** — open in a browser to filter, search and click ⭐/🗄️ · "
            "[MASTER_TABLE.md](MASTER_TABLE.md) — ⭐ cite, ✅ selected, 📋 candidate and 🗄️ archived papers · "
            "[cite.bib](cite.bib) — ⭐ Cite only ·[selected_pool.bib](selected_pool.bib) — ✅ + ⭐ · "
            "[all_reported.bib](all_reported.bib) — everything not archived",
            "", "## Weekly reports", ""]
    for f in sorted(weekly_dir.glob("????-W??.md"), reverse=True):
        t = f.read_text(encoding="utf-8")
        n = len(re.findall(r"^### C?\d+\. ", t, flags=re.M))  # C1, C2… = catch-up papers
        state = "final" if "✅ **Final**" in t else "in progress"
        rows.append(f"- [{f.stem}]({weekly_dir.name}/{f.name}) — {n} papers, {state}")
    if topic_dir and topic_dir.exists():
        rows += ["", "## Topic deep-dives", ""]
        live = None if topics is None else {f"{t.get('date')}_{s}" for s, t in topics.items()}
        for f in sorted(topic_dir.glob("*.md"), reverse=True):
            if live is not None and f.stem not in live and "_recall_" not in f.stem:
                continue  # v3.4.2: a deleted dive's leftover report is not linked
            first = re.sub(r"^# Topic Deep-Dive: ", "", f.read_text(encoding="utf-8").splitlines()[0])
            rows.append(f"- [{f.stem[:10]} — {first}]({topic_dir.name}/{f.name})")
    (weekly_dir.parent / "INDEX.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def export_share(store_path, cfg, dst=None, include_curation=False, log=print):
    """The clean share database (store.export_clean_share_db) at dst, else the optional paths.share_db (~ allowed),
    else output/paper_store_clean_share.sqlite. A failure is logged and never fails the run; the previous share
    database stays."""
    dst = Path(dst or cfg["paths"].get("share_db") or Path(cfg["paths"]["onedrive_root"]) / "paper_store_clean_share.sqlite").expanduser()
    try:
        s = store_mod.export_clean_share_db(store_path, dst, include_curation, secrets=(curation.sync_token(),),
                                            skip_arxiv=arxiv_source.share_skip(cfg))
        log(f"Share database: {dst} ({s['papers']} papers, {s['abstracts']} abstracts, {s['digests']} digests, "
            f"{s['bytes'] / 1e6:.1f} MB{', with curation' if include_curation else ''})")
    except Exception as e:
        log(f"Share database not written (previous one kept): {e}")


def adopt_external(store, cfg):
    """--adopt-external: merged ('external') IEEE papers that score ≥ min_score here become this radar's archive
    papers, so the pillar replenish can pull them. Returns how many."""
    n = 0
    for p in store.where("status IN ('external','external_ro') AND grp='ieee'"):  # external_ro: from read_repos
        s, hits = fetcher.score(p, cfg)
        if s >= cfg["min_score"]:
            store.update(p["doi"], {"status": "archive", "score": s, "hits": hits})
            n += 1
    return n


def sync_data_repo(store, store_path, cfg, push=True, log=print):
    """--sync-data-repo, and after every Friday finalize when the clone exists (data_repo.py): fetch + reset
    the shared corpus repo, merge its file into this store (+ adopt), export the clean union back, commit and
    push if it changed; peers' overlays (peers/<id>.json) come in and this radar's goes out (v2.19), then the
    HTML is rewritten so Peers/DASHBOARD_<peer>.html is current. A failure is logged and never fails the run.
    Returns 0 / 1."""
    try:
        data_repo.sync(store, store_path, cfg, adopt=adopt_external, secrets=(curation.sync_token(),), push=push, log=log)
    except Exception as e:
        log(f"Shared corpus not synced: {e}")
        return 1
    if push:  # not from a --dry-run copy
        html_renderer.write_all(store, cfg, log)
    return 0


def init_data_repo(spec, store, store_path, cfg, log=print):
    """--init-data-repo (STARTUP.html ✨ Create / Init My Private Repo): data_repo.init_repo, then
    data_repo.remote_url in config.json (only the changed key, control_center.write_config). Returns 0 / 1."""
    try:
        r = data_repo.init_repo(spec, store, store_path, cfg, secrets=(curation.sync_token(),), log=log)
    except Exception as e:
        log(f"Data repo not initialized: {e}")
        return 1
    import control_center
    changed = control_center.write_config([(["data_repo", "remote_url"], r["url"])], cfg)
    log(f"Data repo ready: {r['dir']} ({'created on GitHub, ' if r['created'] else ''}"
        f"{'initialized and pushed' if r['initialized'] else 'connected'})"
        + ("; config.json: data_repo.remote_url set" if changed else ""))
    html_renderer.write_all(store, cfg, log)
    return 0


def pull_data_repo(store, cfg, write_html=True, log=print):
    """--pull-data-repo (STARTUP.html's 📥 Read from Private Repo): data_repo.pull, read only (nothing exported or
    pushed), then the HTML, so Peers/DASHBOARD_<peer>.html and the dashboard are current. Returns 0 / 1."""
    try:
        data_repo.pull_only(store, cfg, adopt=adopt_external, log=log)
    except Exception as e:
        log(f"Shared corpus not read: {e}")
        return 1
    if write_html:
        html_renderer.write_all(store, cfg, log)
    return 0


def backup_store(store, cfg, log=print):
    """Disaster recovery: lock-safe online copy of the store (sqlite3 backup API) to
    IC_Paper_Radar\\_system\\paper_store_backup.sqlite. Written to a .tmp file, checked, then swapped in, so
    OneDrive never syncs a half-written copy. A failure is logged and never fails the run."""
    dest = curation.system_dir(cfg) / "paper_store_backup.sqlite"
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        dst = sqlite3.connect(tmp)
        try:
            store.db.backup(dst)
            ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dst.close()
        if ok != "ok":
            raise RuntimeError(f"quick_check: {ok}")
        os.replace(tmp, dest)
        log(f"Store backup: {dest}")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log(f"Store backup failed (previous backup kept): {e}")


T0 = time.time()  # v3.3.2: "Done in N s." at the end of a Control Center action


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--require-vpn", action="store_true")
    g.add_argument("--auto-degrade", action="store_true")
    ap.add_argument("--fetch-pdf", action="store_true")
    ap.add_argument("--finalize", action="store_true", help="finalize this week now (default: on Fridays)")
    ap.add_argument("--friday", action="store_true", help="Friday slot 1 (v3.2.0): pull the shared repos, then finalize "
                    "this week (no upload); skipped as a finalize if this week was already finalized today")
    ap.add_argument("--friday-upload", action="store_true", help="Friday slot 2 (v3.2.0): --sync, then --sync-data-repo")
    ap.add_argument("--no-auto-sync", action="store_true", help="no pull of the shared repos before the Friday finalize")
    ap.add_argument("--clone-data-repo", action="store_true", help="clone data_repo.remote_url if there is no clone yet")
    ap.add_argument("--self-update", action="store_true", help="update the radar code from GitHub (git pull --ff-only)")
    ap.add_argument("--apply-schedule", action="store_true", help="register the Friday slots (config.json schedule) "
                    "as OS scheduled tasks (Windows Task Scheduler / crontab)")
    ap.add_argument("--check-updates", action="store_true", help="print whether GitHub has a new radar version and "
                    "whether the shared repos have new commits")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-claude", action="store_true", help="0 tokens: 🧮 draft digests only (= --claude-mode draft)")
    ap.add_argument("--claude-mode", choices=drafter.MODES, help="v3.4.0: draft (0 tokens) · hybrid (drafts + a minimal "
                    "haiku audit of the uncertain ones) · full (every new paper digested by Claude); default config claude.mode")
    ap.add_argument("--refine", metavar="DOI", help="✨ AI Refine: a full Claude digest for this one paper now")
    ap.add_argument("--draft-eval", action="store_true", help="0 tokens, read only: drafts vs the stored en-v3 digests "
                    "(accuracy, routing, threshold sweep, token estimate)")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--topic", help='on-demand deep-dive, e.g. --topic "sub-100nW sensor design and low-leakage techniques"')
    ap.add_argument("--max-ieee", type=int, help="IEEE calls this --topic run may use (from topic.ieee_topic_reserve)")
    ap.add_argument("--min-year", type=int, metavar="YYYY", help="with --topic / --recall: ignore papers published "
                    "before YYYY (this run only; 0 = all years; default: topic.min_year)")
    ap.add_argument("--split-year", type=int, metavar="YYYY", help="with --topic: papers before YYYY are classics, "
                    "from YYYY on recent (this run only; default: topic.recent_from_year)")
    ap.add_argument("--topic-title", help="v3.4.2: a short English title for --topic / --recall (dropdown, report, file name)")
    ap.add_argument("--topic-spec", help='v3.4.2: the planned dive as JSON: {"core": [[term, synonym], …], "boosters": […], '
                    '"exclude": […], "power_nw": n, "seeds": [doi, …], "prompt": "scope paragraph"}')
    ap.add_argument("--recall", help="search the whole store (incl. archived + corpus) locally: 0 API calls, 0 tokens")
    ap.add_argument("--drop-topic", metavar="SLUG", help="delete a topic dive permanently from 🔎 Topic Dives (its papers and "
                    "report files stay; --retire-topic keeps its 🔎 badges)")
    ap.add_argument("--retire-topic", metavar="SLUG", help="🏺 retire a topic dive: it moves to Past / Antique Dives, its papers "
                    "keep their badge (v3.0.8)")
    ap.add_argument("--unretire-topic", metavar="SLUG", help="↺ restore a retired topic dive")
    ap.add_argument("--retire-pillar", type=int, metavar="N", help="🏺 retire pillar N: its papers keep a 🏺 badge, marks, notes "
                    "and digests; its keywords leave config.json (saved in _system/retired_pillars_config.json) (v3.0.8)")
    ap.add_argument("--unretire-pillar", metavar="KEY", help="↺ restore a 🏺 retired pillar (its antique key): it comes "
                    "back as the last pillar with its label, keywords and papers (v3.3.1); add --rescore --seed-pillars 5")
    ap.add_argument("--delete-pillar", type=int, metavar="N", help="🗑 delete pillar N without an 🏺 antique: its unreviewed "
                    "candidates go back to the corpus, ✅ / ⭐ / 🏷️ / 📌 papers stay without a pillar (v3.3.1)")
    ap.add_argument("--drop-antique-pillar", metavar="KEY", help="🗑 drop a retired pillar's 🏺 tag: its papers keep "
                    "their marks and notes, without a pillar (v3.3.1)")
    ap.add_argument("--sync", action="store_true", help="apply ✅/⭐/🗄️ checkbox edits, refill the Beyond-IEEE board, "
                                                         "rewrite the catalog (a claude -p call only for new board papers)")
    ap.add_argument("--rescore", action="store_true", help="re-score every paper after a keyword/pillar change (0 calls)")
    ap.add_argument("--lineage", action="store_true", help="conference ↔ journal lineage over every catalog paper now "
                                                            "(OpenAlex, 0 IEEE calls, 0 tokens)")
    ap.add_argument("--add", metavar="DOI_OR_TITLE", help="pin a paper into the active catalog (fetched if missing), "
                                                          "bypassing min_score and date windows; with --pillar / --cite")
    ap.add_argument("--pin-anchors", nargs="+", metavar="DOI",
                    help="with --pillar N: file these anchors (config.json anchors) under pillar N (v3.0.7)")
    ap.add_argument("--pillar", type=int, metavar="N", help="pillar number for --add (from config.json \"pillars\")")
    ap.add_argument("--export-share", nargs="?", const="", metavar="DST",
                    help="write the clean share database now (default output/paper_store_clean_share.sqlite or "
                         "paths.share_db; also every Friday finalize)")
    ap.add_argument("--include-curation", action="store_true",
                    help="with --export-share: keep marks, notes, pillars and scores (for a collaborator)")
    ap.add_argument("--merge-store", metavar="OTHER.sqlite",
                    help="merge another store: fill missing objective data, add new DOIs as 'external' (never "
                         "pulled into the pillars); this store's curation is never touched")
    ap.add_argument("--peer", metavar="NAME", help="with --merge-store: keep the other store's marks/notes in peer_curation")
    ap.add_argument("--adopt-external", action="store_true",
                    help="score the 'external' (merged) IEEE papers and move those ≥ min_score into this radar's archive")
    ap.add_argument("--sync-data-repo", action="store_true",
                    help="one-command sync with the shared corpus repo (paths.data_repo_dir, default ../ic-paper-radar-data): "
                         "fetch + reset, merge into this store, export the clean union back, commit + push if changed "
                         "(also on Fridays)")
    ap.add_argument("--pull-data-repo", action="store_true",
                    help="read only: fetch + reset the shared corpus repo, import the peers' overlays, merge its papers "
                         "into this store (+ adopt), rewrite the HTML; nothing is exported or pushed")
    ap.add_argument("--init-data-repo", metavar="OWNER/NAME_OR_URL",
                    help="create (gh) or clone your own private data repo into paths.data_repo_dir, write the protocol "
                         "files + your clean corpus + overlay, push main, set data_repo.remote_url")
    ap.add_argument("--gui", nargs="?", type=int, const=0, metavar="PORT",
                    help="Setup & Control Center: STARTUP.html served on http://127.0.0.1:18766 (or PORT) with working "
                         "buttons, until Ctrl+C (control_center.py)")
    ap.add_argument("--no-browser", action="store_true", help="with --gui: don't open the browser")
    ap.add_argument("--plan-keywords", nargs="?", const="", metavar="SEEDS",
                    help="0-token keyword & weight planner (keyword_planner.py): SEEDS = rough words of pillar 1; "
                         "pillar 2; … With your anchors → suggested weighted keywords, re: metric regexes, "
                         "negative_override and VIP authors (printed; STARTUP.html 🪄 applies them)")
    ap.add_argument("--seed-pillars", type=int, nargs="?", const=-1, metavar="N",
                    help="pull the best N archive papers per pillar (since catch_up.since) into this week now "
                         "(default catch_up.per_pillar), pin the anchors, and fill the Beyond-IEEE board")
    # --cite takes a DOI/text; with --add it may stand alone ("--add <doi> --cite")
    ap.add_argument("--cite", metavar="DOI_OR_TEXT", nargs="?", const=True,
                    help="mark papers matching a DOI or title substring (cite); bare with --add")
    for flag in ("select", "unselect", "archive", "unarchive", "uncite"):
        ap.add_argument(f"--{flag}", metavar="DOI_OR_TEXT", help=f"mark papers matching a DOI or title substring ({flag})")
    a = ap.parse_args()
    if a.cite is True and not a.add:
        ap.error("--cite needs a DOI or title substring (or use it with --add)")
    if a.pillar and not (a.add or a.pin_anchors):
        ap.error("--pillar only works with --add or --pin-anchors")
    add_cite = a.cite is True
    if add_cite:
        a.cite = None

    cfg = fetcher.load_config()
    if (cfg.get("claude") or {}).get("enabled") is False and not a.no_claude:
        a.no_claude = True  # legacy v3.3.3 switch; since v3.4.0 STARTUP.html writes it as the mirror of claude.mode = draft
        print("Claude AI: 🧮 0-Token Draft (config.json claude.enabled = false): running as --no-claude", flush=True)
    tspec = None  # v3.4.2: --topic-spec (STARTUP.html's planned dive)
    if a.topic_spec:
        try:
            tspec = topic.clean_spec(json.loads(a.topic_spec))
        except (ValueError, TypeError) as e:
            ap.error(f"--topic-spec: {e}")
    if a.plan_keywords is not None:  # v3.0.5, read only: prints the plan (STARTUP.html 🪄 shows it in a preview)
        import keyword_planner
        return keyword_planner.cli(cfg, a.plan_keywords)
    if a.gui is not None:  # the Setup & Control Center: a local server until Ctrl+C (control_center.py)
        import control_center
        return control_center.serve_gui(cfg, a.gui or None, open_browser=not a.no_browser)
    if a.self_update or a.apply_schedule or a.check_updates:  # v3.2.0, no store needed
        import control_center
        if a.check_updates:
            r = control_center.check_updates(cfg, force=True)
            print(json.dumps(r, indent=1, ensure_ascii=False))
            return 0
        return (control_center.self_update(cfg) if a.self_update else control_center.apply_schedule(cfg))
    if a.friday_upload:  # v3.2.0 Friday slot 2, like run_data_repo_sync.cmd: two runs, each taking the run lock
        dr = cfg.get("data_repo") or {}
        if not ((data_repo.repo_dir(cfg) / ".git").exists() or (dr.get("remote_url") or "").strip()):
            print("Friday upload: no primary shared repo (Step 2 → remote_url): nothing to upload")
            return 0
        rc = 0
        for extra in (["--sync"], ["--sync-data-repo"]):
            r = subprocess.call([sys.executable, str(Path(__file__).resolve().parent / "briefing.py"), *extra])
            rc = r or rc
        return rc
    if a.clone_data_repo:  # v3.2.0: STARTUP.html saves a remote_url without a clone → clone it next to the radar
        try:
            d = data_repo.ensure_clone(cfg)
        except Exception as e:
            print(f"Shared repo not cloned: {e}")
            return 1
        print(f"Shared repo clone: {d}" if (d / ".git").exists() else "No data_repo.remote_url: nothing to clone")
        return 0
    # one radar run at a time (run_lock.py): the scheduled tasks, terminal runs and the Control Center's jobs
    # wait for each other (up to 30 min) instead of working on the store / data-repo clone together
    pillar_ops = a.unretire_pillar or a.delete_pillar or a.drop_antique_pillar  # v3.3.1
    recall_only = a.recall and not (a.topic or a.drop_topic or a.retire_topic or a.unretire_topic or a.retire_pillar or pillar_ops or a.sync or a.rescore or a.lineage or a.add or a.seed_pillars is not None
                                    or any(getattr(a, f) for f in curation.ACTIONS))
    if not recall_only:
        busy = run_lock.acquire(sys.argv[1:])
        if busy:
            print(f"[BUSY] still running after {run_lock.WAIT_S // 60} min: {run_lock.describe(busy)}. "
                  "Not started; run it again later.", file=sys.stderr)
            return run_lock.EXIT_BUSY
        run_lock.tee_output()  # .run.log: the Control Center shows this run too
    if a.pillar and a.pillar not in catalog.PILLARS:
        ap.error(f"--pillar must be one of {sorted(k for k in catalog.PILLARS if k)} (config.json \"pillars\")")
    if not a.no_claude and not claude_available(cfg):
        print(f"[INFO] '{cfg['claude']['command']}' CLI not found on PATH: running in --no-claude mode (scoring, "
              "dashboard, master table, --topic and --seed-pillars still work; digests are 🧮 0-token drafts and the synthesis is skipped).")
        a.no_claude = True
    today = date.today()
    week, friday = fetcher.week_of(today)
    final = a.finalize or a.friday or today.strftime("%A") == cfg["report_day"]

    store_path = HERE / cfg["paths"]["store_file"]
    out_dir = Path(cfg["paths"]["weekly_dir"])
    if a.dry_run:
        dry = HERE / "_dryrun.sqlite"
        if store_path.exists():
            shutil.copy(store_path, dry)
        archive = Path(cfg["paths"].get("archive_dir") or Path(cfg["paths"]["onedrive_root"]) / "_archive")
        store_path, out_dir = dry, archive / "_dryrun" / out_dir.name
    if fetcher.store_paper_count(store_path) == 0:  # checked before Store() creates an empty file
        print("\n" + fetcher.STORE_TIP + "\n")
    store = Store(store_path)
    mode = claude_mode(cfg, a)  # v3.4.0: draft | hybrid | full (drafter.py)
    if a.draft_eval:
        return draft_eval(store, cfg)
    if final and not a.finalize and not a.dry_run and store.meta("last_finalize") == today.isoformat():
        final = False  # v3.2.0: the daily task and the Friday slot both run on Friday: finalize only once
        print("This week was already finalized today: a progressive run (use --finalize to finalize again)")
    import scorer
    scorer.prepare(store, cfg)  # v3.0: coupling core + taste model from this store's marks
    topic_dir = Path(cfg["paths"]["topic_dir"])

    if a.adopt_external and (a.rescore or a.seed_pillars is not None):
        # v3.3.2: ▶ Run & Update in one process (one scorer.prepare): adopt, then go on to --rescore / --seed-pillars
        print(f"Adopted {adopt_external(store, cfg)} external papers into the archive (≥ min_score)", flush=True)
        a.adopt_external = False
    # sharing (v2.16): stand-alone actions, no collection / report
    if (a.merge_store or a.adopt_external or a.export_share is not None or a.sync_data_repo or a.pull_data_repo
            or a.init_data_repo):
        if a.merge_store:
            store_mod.merge_store(store, a.merge_store, a.peer)
            if a.peer and not a.dry_run:  # the peer's page (Peers/DASHBOARD_<peer>.html) and the header link
                html_renderer.write_all(store, cfg)
        if a.adopt_external:
            print(f"Adopted {adopt_external(store, cfg)} external papers into the archive (≥ min_score)")
        if a.export_share is not None:
            export_share(store_path, cfg, a.export_share or None, a.include_curation)
        if a.init_data_repo:
            return init_data_repo(a.init_data_repo, store, store_path, cfg)
        if a.pull_data_repo:
            return pull_data_repo(store, cfg, write_html=not a.dry_run)
        if a.sync_data_repo:  # with --dry-run: on the dry-run copy, committed locally but never pushed
            return sync_data_repo(store, store_path, cfg, push=not a.dry_run)
        return 0

    def refresh_catalog():
        catalog.write_all(store, cfg)
        update_index(Path(cfg["paths"]["weekly_dir"]), topic_dir, store.meta("topics") or {})
        curation.sync_markdown_marks(store, cfg, read_edits=False)  # bring every report's boxes in line
        html_renderer.write_all(store, cfg)  # after the .md boxes are final

    if a.refine:  # ✨ AI Refine (DASHBOARD.html button → /api/run refine, or a terminal)
        rc = refine(store, cfg, a.refine)
        if not a.dry_run:
            html_renderer.write_all(store, cfg)
        print(f"Done in {time.time() - T0:.1f} s.", flush=True)
        return rc

    if final and not a.dry_run and not a.no_auto_sync and store.meta("friday_pull") != today.isoformat():
        sc = cfg.get("schedule") or {}
        dr = cfg.get("data_repo") or {}
        if sc.get("auto_update_local", True) is not False and (
                (data_repo.repo_dir(cfg) / ".git").exists() or (dr.get("remote_url") or "").strip() or dr.get("read_repos")):
            print("Friday slot 1: pulling the shared repos first (nothing is pushed now)")
            pull_data_repo(store, cfg, write_html=False)  # a failure is logged; the local briefing goes on
            store.meta("friday_pull", today.isoformat())
    if not a.dry_run:
        n = curation.sync_markdown_marks(store, cfg)
        print(f"Curation sync: {n} checkbox change(s) applied")
    marks = [(getattr(a, f), f) for f in curation.ACTIONS if getattr(a, f)]
    for needle, action in marks:
        curation.set_mark(store, needle, action)
    if not fetcher.pillars_ready(cfg) and not (a.topic or a.recall or a.drop_topic or a.retire_topic or a.unretire_topic):
        # Starter Edition: nothing to score against yet (--topic / --recall don't need pillars);
        # still (re)write the welcome dashboard
        print("\n" + fetcher.SETUP_MSG + "\n")
        if not a.dry_run:
            refresh_catalog()
            print(f"Dashboard: {Path(cfg['paths']['onedrive_root']) / 'DASHBOARD.html'}")
        return 0
    if a.unretire_pillar:  # v3.3.1 ↺ Restore: before --rescore / --seed-pillars, which then use its keywords
        import retire
        try:
            retire.unretire_pillar(store, cfg, a.unretire_pillar)
        except ValueError as e:
            print(f"[retire] {e}", file=sys.stderr)
            return 1
        cfg = fetcher.load_config()  # the pillar and keywords just written back
    if a.rescore:
        fetcher.rescore_all(cfg, store, log=lambda m: print(m, flush=True))
    if a.lineage:
        print(f"Lineage: {relations.catalog_lineage(cfg, store, today)} new conference ↔ journal links")
    if a.retire_topic or a.unretire_topic or a.retire_pillar or a.delete_pillar or a.drop_antique_pillar:  # retire.py
        import retire
        try:
            if a.retire_pillar:
                retire.retire_pillar(store, cfg, a.retire_pillar)
                cfg = fetcher.load_config()  # the pillars and keywords just written
            if a.delete_pillar:  # v3.3.1 🗑 Delete (no 🏺 antique)
                retire.delete_pillar(store, cfg, a.delete_pillar)
                cfg = fetcher.load_config()
            if a.drop_antique_pillar:  # v3.3.1 🗑 Drop Antique Tag
                retire.drop_antique(store, cfg, a.drop_antique_pillar)
            if a.retire_topic or a.unretire_topic:
                retire.set_topic_retired(store, a.retire_topic or a.unretire_topic, bool(a.retire_topic))
        except ValueError as e:
            print(f"[retire] {e}", file=sys.stderr)
            return 1
    if a.drop_topic:  # v3.0.6 🗑 Delete; v3.4.2: report files removed, uncurated papers back to the corpus
        import retire
        try:
            retire.drop_topic(store, cfg, a.drop_topic)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    if a.min_year is not None or a.split_year is not None:  # v3.2.3: year bounds for this run only
        tcfg = cfg.setdefault("topic", {})
        if a.min_year is not None:
            tcfg["min_year"] = a.min_year if a.min_year > 1900 else 0
        if a.split_year is not None:
            if not 1950 <= a.split_year <= 2100:
                print("--split-year must be a year between 1950 and 2100", file=sys.stderr)
                return 1
            tcfg["recent_from_year"] = a.split_year
        print(f"Topic years: {tcfg.get('min_year') or 'all'} onward, recent from {tcfg.get('recent_from_year')}")
    if a.recall:
        topic.recall(a.recall, cfg, store, spec=tspec, title=a.topic_title)
    if a.topic:
        topic.run(a.topic, cfg, store, a.max_ieee, claude, print, use_claude=mode != "draft", spec=tspec, title=a.topic_title)
    if a.add or a.seed_pillars is not None or a.pin_anchors:
        print(f"Anchors: {pins.sync_anchors(store, cfg)} pinned as 🏛️ Classic / Anchor")
        if a.pin_anchors and a.pillar:  # v3.0.7: STARTUP.html → ＋ Add New Pillar files its anchors under it
            filed = [d.lower() for d in a.pin_anchors if store.get(d.lower())]
            for d in filed:
                store.update(d, {"pillar": a.pillar})
            print(f"Pillar {a.pillar}: {len(filed)} anchor(s) filed under it")
        added = a.add and pins.add(a.add, cfg, store, a.pillar, add_cite)
        if a.add and not added:
            return 1
        if a.seed_pillars is not None:
            print("Seeding pillars…", flush=True)
            # v3.3.2: right after --rescore the stored scores are fresh: rank by them instead of scoring again
            n = catalog.pillar_replenish(store, cfg, week, None if a.seed_pillars < 0 else a.seed_pillars,
                                         fresh=not a.rescore)
            print("Seed: " + pillar_line(n), flush=True)
            print(f"Beyond pool: {fetcher.pool_search(cfg, store, today)} gate-passing papers searched")
        try:
            p = added and store.get(added)
            # this paper right away if it has an abstract (else the daily queue waits for one, ≤ 2 days);
            # v3.4.0: Hybrid drafts it and audits it (a paper the user pinned is a highlight), Draft only drafts it
            if p and not digest_ok(p, cfg, mode):
                if mode == "full" and p.get("abstract"):
                    _run_digests([p], DIGEST_PROMPT, prompt_ver(cfg), _digest_payload, store, cfg, print, "pinned paper")
                else:
                    digest_papers([p], store, cfg, print, mode if p.get("abstract") else "draft", {p["doi"]})
        except Exception as e:
            print(f"Digest failed (retried next run): {e}", file=sys.stderr)
    if a.sync or marks or a.rescore or a.lineage or a.recall or a.topic or a.drop_topic or a.retire_topic \
            or a.unretire_topic or a.retire_pillar or pillar_ops or a.add or a.seed_pillars is not None:
        if a.dry_run:
            return 0
        board = a.sync or marks or a.rescore or a.lineage or a.add or a.seed_pillars is not None  # not --recall/--topic
        refilled = {}
        if board:  # (v3.3.2: no second scoring pass after --rescore) a mark may have dropped a pillar below the water mark, or freed a board slot
            print("Replenishing pillars…", flush=True)
            refilled = catalog.pillar_replenish(store, cfg, week, fresh=not a.rescore)
            print("Pillar replenish: " + pillar_line(refilled), flush=True)
            print(f"Beyond board: +{fetcher.beyond_replenish(cfg, store, week)} from the pool")
        if board:
            try:  # the board's new papers (and the replenished / seeded IEEE papers, ≤ digests_per_day_max)
                if refilled or a.seed_pillars is not None:
                    digest_new(store, cfg, week, today, print, mode)
                else:
                    digest_beyond(store, cfg, print, mode)
            except Exception as e:
                print(f"Digest failed (retried next run): {e}", file=sys.stderr)
        if a.retire_pillar or a.delete_pillar or a.unretire_pillar:  # v3.4.2: this week's report too
            rerender_week(store, cfg, today)
        print("Rebuilding DASHBOARD.html & STARTUP.html…", flush=True)
        refresh_catalog()
        print(f"Done in {time.time() - T0:.1f} s.", flush=True)
        return 0

    # The daily pipeline only uses public APIs (IEEE Metadata API, Crossref, OpenAlex), and IEEE
    # Xplore may bypass a split-tunnel VPN anyway, so the VPN matters only for --require-vpn / --fetch-pdf.
    if a.require_vpn or a.fetch_pdf:
        vpn = vpn_checker.check_vpn_status()
        print(vpn["message"])
        if not vpn["connected"]:
            if cfg["vpn"].get("notify", True) and not a.dry_run:
                vpn_checker.notify_disconnected()
            if a.require_vpn:
                print("--require-vpn set: stopping.", file=sys.stderr)
                return 2
    else:
        vpn = {"connected": None, "message": "public APIs only (VPN not checked; use --require-vpn / --fetch-pdf)"}

    print("Collecting (IEEE API / Crossref / OpenAlex, 0 tokens) ...")
    run = fetcher.collect(cfg, store, today, backfill=not a.no_backfill)
    store.meta("last_run", {k: run.get(k) for k in ("new", "new_candidates", "ieee_calls", "errors")})  # v3.4.2
    print(f"{run['new']} new records, {len(run['new_candidates'])} new matches, IEEE calls today {run['ieee_calls']}")
    print("Catch-up: " + pillar_line(catalog.pillar_replenish(store, cfg, week)))
    print(f"Anchors: {pins.sync_anchors(store, cfg)} pinned as 🏛️ Classic / Anchor")
    print(f"Beyond board: +{fetcher.beyond_replenish(cfg, store, week)} from the pool")

    ieee_q =("status='candidate' AND week=? AND grp='ieee'", (week,))
    relations.fill_keyrefs(store.where(*ieee_q), cfg, store)  # references first: lineage signal A uses them
    n = relations.update_lineage(store.where(*ieee_q), cfg, store, today)
    n += relations.catalog_lineage(cfg, store, today)  # anchors, pinned, selected, cited, reported (every 30 days)
    print(f"Lineage: {n} new conference ↔ journal links")

    try:  # v3.4.0: every mode; Draft (--no-claude) writes 0-token drafts instead of leaving blanks
        print(f"Digests: {dict(draft='🧮 0-Token Draft Only', hybrid='🔍 Hybrid Audit', full='🤖 Full Claude')[mode]}")
        digest_new(store, cfg, week, today, print, mode)
    except Exception as e:
        print(f"Digest failed (retried next run): {e}", file=sys.stderr)

    allc = sorted(store.where(*ieee_q), key=lambda p: (-p["score"], p["first_seen"]))
    fresh = [p for p in allc if not p.get("catchup")]
    catch = [p for p in allc if p.get("catchup")]
    papers, also = fresh[:cfg["weekly_top"]], fresh[cfg["weekly_top"]:]
    all_beyond = sorted(store.where("status='candidate' AND week=? AND grp='beyond'", (week,)), key=lambda p: -p["score"])
    beyond = [p for p in all_beyond if (p.get("user_mark") or "normal") != "archived"]  # this week's board arrivals
    edges, notes = relations.week_relations(papers, store)
    notes.update(relations.week_relations(catch, store)[1])

    synth = ""
    if final and (papers or catch) and mode != "draft":
        try:
            synth = synthesis(store, cfg, week, papers + catch, beyond, notes, a.finalize, print)
        except Exception as e:
            synth = f"> ❌ Synthesis failed: {e}"

    queue_path = HERE / cfg["paths"]["pdf_queue_file"]
    queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    todays = [p for p in papers if p["doi"] in run["new_candidates"]]
    if vpn["connected"] and a.fetch_pdf:
        queue = fetch_pdfs(queue + [{"doi": p["doi"], "title": p["title"]} for p in todays], cfg)
    elif vpn["connected"] is False:
        known = {q["doi"] for q in queue}
        queue += [{"doi": p["doi"], "title": p["title"], "queued": today.isoformat()} for p in todays if p["doi"] not in known]

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{week}.md"
    out.write_text(render(week, friday, today, vpn, papers, also, catch, beyond, notes, edges, synth, final, store, cfg, run),
                   encoding="utf-8")
    print(f"Wrote {out}")

    if final and not a.dry_run:
        for p in allc + all_beyond:
            store.update(p["doi"], {"status": "reported"})
        store.meta("last_finalize", today.isoformat())
    if not a.dry_run:
        refresh_catalog()
    else:
        update_index(out_dir)
    if not a.dry_run:
        queue_path.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
        backup_store(store, cfg, print)
        if final:  # the share database grows with the corpus, once a week
            export_share(store_path, cfg)
            # v3.2.0: no upload here. The Friday slot 2 (schedule.upload_time, briefing.py --friday-upload) uploads
            # after the morning's curation; data_repo.friday_sync is no longer read.
    print(store.counts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
