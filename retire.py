"""🏺 Retire as Antique (v3.0.8): take a pillar or a topic dive out of active duty without losing anything.

retire_pillar(store, cfg, n)  briefing.py --retire-pillar N  (STARTUP.html → 🎯 Themes → 🏺 Retire)
  1. Every paper of pillar N (papers.pillar = N, or catalog.pillar_of = N for a core / curated / noted / pinned
     paper) gets papers.retired_pillar = the pillar's antique key (its short label, made unique), and its
     papers.pillar is cleared. An unreviewed, unmarked candidate goes quietly back to the corpus (status
     'archive'); everything curated, pinned, noted or already reported keeps its status. Marks, ⭐, notes, the
     Si call and digests are never touched. catalog.pillar_of returns 0 for a stamped paper, so a later,
     different pillar with the same number never picks it up.
  2. Papers with a chosen pillar or a digest Pillar line above N are pinned to their pillar under the new
     numbering (N+1 → N, …), since the old digests keep their old numbers.
  3. config.json: pillar N leaves "pillars" / "pillar_short"; its _pillarN_ keyword block is removed (saved with
     the pillar's name and label in _system/retired_pillars_config.json) and the blocks above it are renumbered.
  4. meta['retired_pillars'][key] = {id, name, short, retired_at, papers}: the 🏺 badge's tooltip and the
     dashboard's 🏺 Antiques ▾ menu.
On the Minisforum radar pillars 1–4 are fixed (catalog.FIXED_PILLARS) and can't be retired.

set_topic_retired(store, slug, flag)  briefing.py --retire-topic / --unretire-topic SLUG
  meta['topics'][slug]['retired'] = True (or removed): the dive moves into 🏺 Past / Antique Dives ▾ on the
  dashboard and its papers keep a 🏺 <short> badge; nothing else changes, so ↺ Restore brings it back as it was.
"""

from __future__ import annotations

import json
import re
from datetime import date

MARK_RX = re.compile(r"^_pillar(\d+)(.*)$", re.S)
UNREVIEWED = ("status='candidate' AND COALESCE(user_mark,'normal')='normal' AND COALESCE(cited,0)=0 "
              "AND COALESCE(user_note,'')='' AND pin IS NULL AND user_silicon IS NULL")


def antiques(store):
    """meta['retired_pillars']: {key: {id, name, short, retired_at, papers}}."""
    return store.meta("retired_pillars") or {}


def _digest_pillar(p):
    m = re.search(r"\*\*Pillar:\*\*\s*\(?(?:Pillar\s*)?(\d+)", p.get("digest") or "")
    return int(m[1]) if m and p.get("digest_ver") not in ("en-v2", "topic-v1") else 0


def split_keywords(pk, n):
    """positive_keywords → (the dict without pillar n's block, blocks above n renumbered; pillar n's block)."""
    keep, gone, cur = {}, {}, 0
    for k, w in pk.items():
        m = MARK_RX.match(k)
        if m:
            cur = int(m[1])
            if cur == n:
                gone[k] = w
                continue
            keep[f"_pillar{cur - 1}{m[2]}" if cur > n else k] = w
        elif cur == n:
            gone[k] = w
        else:
            keep[k] = w
    return keep, gone


def retire_pillar(store, cfg, n, log=print):
    import catalog
    import control_center
    import curation
    fixed = control_center.fixed_pillars()
    if n not in catalog.PILLARS or not n:
        raise ValueError(f"no pillar {n} (pillars: {sorted(k for k in catalog.PILLARS if k)})")
    if n <= fixed:
        raise ValueError(f"pillars 1–{fixed} are fixed on this radar and can't be retired")
    name = catalog.PILLARS[n].split(" — ", 1)[-1]
    short = catalog.pillar_short(n).split(" · ", 1)[-1]
    old = antiques(store)
    key = short if short not in old else f"{short} ({date.today().isoformat()})"
    kwmap = catalog._kw_pillars(cfg)

    # 1. stamp the pillar's papers (pillar_of with the current pillars and keywords, before anything changes)
    stamped = back = frozen = 0
    for p in store.where("COALESCE(grp,'ieee') != 'beyond' AND retired_pillar IS NULL"):
        mine = p.get("pillar") == n
        if not mine:
            curated = (p.get("user_mark") or "normal") != "normal" or p.get("cited") or p.get("user_note") or p.get("pin")
            if p.get("status") in ("candidate", "reported", "pinned") or curated:
                mine = catalog.pillar_of(p, kwmap) == n
        if mine:
            upd = {"retired_pillar": key, "pillar": None}
            if store.db.execute(f"SELECT 1 FROM papers WHERE doi=? AND {UNREVIEWED}", (p["doi"],)).fetchone():
                upd["status"] = "archive"  # an unreviewed candidate: quietly back to the corpus
                back += 1
            store.update(p["doi"], upd)
            stamped += 1
        elif not p.get("pillar") and _digest_pillar(p) > n:
            # 2. an old digest names a pillar above n: keep the paper where it is under the new numbering
            store.update(p["doi"], {"pillar": _digest_pillar(p)})
            frozen += 1
    store.db.execute("UPDATE papers SET pillar = pillar - 1 WHERE pillar > ?", (n,))
    store.db.commit()

    # 3. config.json: the pillar, its label and its keyword block (backed up), the blocks above renumbered
    raw = control_center.raw_config()
    pk = raw.get("positive_keywords") if isinstance(raw.get("positive_keywords"), dict) else cfg["positive_keywords"]
    keep, gone = split_keywords(pk, n)
    names = list(raw.get("pillars") if raw.get("pillars") is not None else cfg.get("pillars") or [])
    shorts = list(raw.get("pillar_short") if raw.get("pillar_short") is not None else cfg.get("pillar_short") or [])
    if fixed and len(names) < len(catalog.PILLARS) - 1:  # a radar whose config doesn't list the fixed pillars yet
        names = catalog.pillar_names()
    names = names[:n - 1] + names[n:]
    shorts = shorts[:n - 1] + shorts[n:]
    while shorts and not str(shorts[-1]).strip():
        shorts.pop()
    bak = curation.system_dir(cfg) / "retired_pillars_config.json"
    try:
        hist = json.loads(bak.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    hist.append({"key": key, "id": n, "name": name, "short": short, "retired_at": date.today().isoformat(),
                 "positive_keywords": gone})
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    changed = control_center.write_config([(["pillars"], names), (["pillar_short"], shorts),
                                           (["positive_keywords"], keep)], cfg)

    # 4. the antique record
    old[key] = {"id": n, "name": name, "short": short, "retired_at": date.today().isoformat(), "papers": stamped}
    store.meta("retired_pillars", old)
    log(f"🏺 Retired pillar {n} '{name}' as '{key}': {stamped} papers stamped ({back} unreviewed candidates back to the "
        f"corpus), {frozen} papers kept on their renumbered pillar, {len(gone)} keywords saved in {bak.name}; "
        f"config.json: {', '.join(changed) or 'unchanged'}")
    return {"key": key, "stamped": stamped, "back": back, "frozen": frozen, "keywords": len(gone)}


def set_topic_retired(store, slug, retired=True, log=print):
    topics = store.meta("topics") or {}
    if slug not in topics:
        raise ValueError(f"no topic dive {slug!r} (have: {', '.join(topics) or 'none'})")
    if retired:
        topics[slug]["retired"] = True
        topics[slug]["retired_at"] = date.today().isoformat()
    else:
        topics[slug].pop("retired", None)
        topics[slug].pop("retired_at", None)
    store.meta("topics", topics)
    log(f"{'🏺 Retired' if retired else '↺ Restored'} topic dive {slug}")
