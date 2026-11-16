"""🏺 Retire as Antique (v3.0.8), ↺ Restore and 🗑 Delete (v3.3.1): the life cycle of a pillar or a topic dive.

Every pillar can go through it, P1–P4 included (v3.3.1; before, the Minisforum radar fixed P1–P4).

Cached digests: an en-v3 digest's "Pillar:" line names the pillar *number* that was current when it was written
(briefing.pillars_prompt lists the pillars as they are). Whenever pillar numbers change (retire, delete, a reorder
saved in STARTUP.html), remap_papers() first writes that pillar down for every paper filed by its digest, under the
new numbering (papers.pillar; 0 = no pillar any more), and renumbers explicit papers.pillar values the same way.
So no digest is redone and none points at the wrong pillar; papers without a digest follow their keywords
(catalog.pillar_of votes with the renumbered _pillarN_ blocks).

retire_pillar(store, cfg, n)   briefing.py --retire-pillar N   (STARTUP.html → Step 3 → 🏺 Retire)
  1. Every paper of pillar N (papers.pillar = N, or catalog.pillar_of = N for a core / curated / noted / pinned
     paper) gets papers.retired_pillar = the pillar's antique key (its short label, made unique), and its
     papers.pillar is cleared. An unreviewed, unmarked candidate goes quietly back to the corpus (status
     'archive'); everything curated, pinned, noted or already reported keeps its status. Marks, ⭐, notes, the
     Si call and digests are never touched. catalog.pillar_of returns 0 for a stamped paper.
  2. remap_papers: N → 0, N+1 → N, …
  3. config.json: pillar N leaves "pillars" / "pillar_short"; its _pillarN_ keyword block is removed (saved with
     the pillar's name, label and anchors in _system/retired_pillars_config.json); the blocks above are renumbered.
  4. meta['retired_pillars'][key] = {id, name, short, retired_at, papers}: the 🏺 badge's tooltip and the
     dashboard's 🏺 Antiques ▾ menu.
unretire_pillar(store, cfg, key)   --unretire-pillar KEY   (↺ Restore)
  The pillar comes back at the end (P<last+1>) with its label and saved keyword block; its stamped papers lose the
  🏺 stamp and are filed under the new number. STARTUP.html then runs --rescore --seed-pillars 5 --no-claude.
delete_pillar(store, cfg, n)   --delete-pillar N   (🗑 Delete)
  Like retire, but without an 🏺 antique: the pillar's papers get papers.pillar = 0 (no pillar); unreviewed
  candidates go back to the corpus; ✅ / ⭐ / 🏷️ / 📌 papers keep their status, marks and notes (Selected and
  Cite views). Its keyword block is removed (a copy in _system/deleted_pillars_config.json).
drop_antique(store, cfg, key)   --drop-antique-pillar KEY   (🗑 Drop Antique Tag)
  Forgets a retired pillar: its papers lose the 🏺 stamp (papers.pillar = 0), meta and the 🏺 Antiques menu
  forget it; its saved keywords stay in retired_pillars_config.json.

set_topic_retired(store, slug, flag)  briefing.py --retire-topic / --unretire-topic SLUG
  meta['topics'][slug]['retired'] = True (or removed): the dive moves into 🏺 Past / Antique Dives ▾ on the
  dashboard and its papers keep a 🏺 <short> badge; nothing else changes, so ↺ Restore brings it back as it was.
"""

from __future__ import annotations

import json
import re
from datetime import date

MAX_PILLARS = 9
MARK_RX = re.compile(r"^_pillar(\d+)(.*)$", re.S)
UNREVIEWED = ("status='candidate' AND COALESCE(user_mark,'normal')='normal' AND COALESCE(cited,0)=0 "
              "AND COALESCE(user_note,'')='' AND pin IS NULL AND user_silicon IS NULL")


def antiques(store):
    """meta['retired_pillars']: {key: {id, name, short, retired_at, papers}}."""
    return store.meta("retired_pillars") or {}


def _digest_pillar(p):
    """The pillar number a trusted cached digest names (catalog.digest_pillar), else 0."""
    import catalog
    f = getattr(catalog, "digest_pillar", None)
    if f:
        return f(p)
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


def config_lists(cfg):
    """(pillar names, pillar_short, positive_keywords) as config.json holds them; a radar whose config.json has no
    "pillars" yet gets them seeded from the pillars in use (catalog.PILLARS / PILLAR_SHORT)."""
    import catalog
    import control_center
    raw = control_center.raw_config()
    pk = dict(raw["positive_keywords"]) if isinstance(raw.get("positive_keywords"), dict) else dict(cfg["positive_keywords"])
    ks = sorted(k for k in catalog.PILLARS if k)
    names = [str(x) for x in raw["pillars"]] if isinstance(raw.get("pillars"), list) and raw["pillars"] \
        else [catalog.PILLARS[k].split(" — ", 1)[-1] for k in ks]
    shorts = [str(x or "") for x in raw["pillar_short"]] if isinstance(raw.get("pillar_short"), list) \
        else [catalog.PILLAR_SHORT.get(k, "") for k in ks]
    return names, shorts, pk


def _trim(shorts):
    while shorts and not str(shorts[-1]).strip():
        shorts.pop()
    return shorts


def remap_papers(store, mapping, log=print):
    """Before pillar numbers change: mapping {old number: new number, 0 = gone} for every pillar that moves or goes.
    Explicit papers.pillar values are renumbered; then every paper filed by a cached digest's Pillar line gets that
    pillar written down under the new numbering. Returns (renumbered, pinned)."""
    mapping = {int(o): int(n) for o, n in (mapping or {}).items() if int(o) != int(n)}
    if not mapping:
        return 0, 0
    keys = ",".join(str(o) for o in mapping)
    case = " ".join(f"WHEN {o} THEN {n}" for o, n in mapping.items())
    moved = store.db.execute(f"UPDATE papers SET pillar = CASE pillar {case} END WHERE pillar IN ({keys})").rowcount
    store.db.commit()
    pinned = 0
    for p in store.where("pillar IS NULL AND retired_pillar IS NULL AND COALESCE(digest,'') != ''"):
        d = _digest_pillar(p)
        if d in mapping:
            store.update(p["doi"], {"pillar": mapping[d]})
            pinned += 1
    store.db.commit()
    return moved, pinned


def save_remap(store, old_names, new_names, log=print):
    """A saved reorder / rename (STARTUP.html 💾 Save): names matched first, then old pillars that are no longer
    listed take the new positions nobody claimed, in order (a rename in place keeps its number)."""
    new_pos = {}
    for i, n in enumerate(old_names, 1):
        if n in new_names:
            new_pos[i] = new_names.index(n) + 1
    free = [j for j in range(1, len(new_names) + 1) if j not in new_pos.values()]
    for i in range(1, len(old_names) + 1):
        if i not in new_pos:
            new_pos[i] = free.pop(0) if free else 0
    moved, pinned = remap_papers(store, new_pos, log)
    if moved or pinned:
        log(f"Pillars renumbered: {moved} filed papers moved with their pillar, {pinned} digest-filed papers pinned")
    return moved, pinned


def _is_mine(p, n, kwmap):
    import catalog
    if p.get("pillar") == n:
        return True
    if p.get("pillar") is not None:
        return False
    curated = (p.get("user_mark") or "normal") != "normal" or p.get("cited") or p.get("user_note") or p.get("pin")
    return (p.get("status") in ("candidate", "reported", "pinned") or curated) and catalog.pillar_of(p, kwmap) == n


def _check_n(names, n, what):
    if not 1 <= n <= len(names):
        raise ValueError(f"no pillar {n} (pillars: 1–{len(names)})")
    if len(names) <= 1:
        raise ValueError(f"pillar {n} is the only pillar: add another before you {what} it")


def _history(path):
    try:
        h = json.loads(path.read_text(encoding="utf-8"))
        return h if isinstance(h, list) else []
    except (OSError, ValueError):
        return []


def retire_pillar(store, cfg, n, log=print):
    import catalog
    import control_center
    import curation
    names, shorts, pk = config_lists(cfg)
    _check_n(names, n, "retire")
    name = names[n - 1]
    short = (shorts[n - 1].strip() if n - 1 < len(shorts) and str(shorts[n - 1]).strip()
             else catalog.pillar_short(n, name).split(" · ", 1)[-1])
    old = antiques(store)
    key = short if short not in old else f"{short} ({date.today().isoformat()})"
    kwmap = catalog._kw_pillars(cfg)

    # 1. stamp the pillar's papers (pillar_of with the current pillars and keywords, before anything changes)
    stamped = back = 0
    for p in store.where("COALESCE(grp,'ieee') != 'beyond' AND retired_pillar IS NULL"):
        if _is_mine(p, n, kwmap):
            upd = {"retired_pillar": key, "pillar": None}
            if store.db.execute(f"SELECT 1 FROM papers WHERE doi=? AND {UNREVIEWED}", (p["doi"],)).fetchone():
                upd["status"] = "archive"  # an unreviewed candidate: quietly back to the corpus
                back += 1
            store.update(p["doi"], upd)
            stamped += 1
    store.db.commit()
    # 2. the pillars above it move down one; digest-filed papers are pinned under the new numbers
    moved, frozen = remap_papers(store, {n: 0, **{k: k - 1 for k in range(n + 1, len(names) + 1)}}, log)

    # 3. config.json: the pillar, its label and its keyword block (backed up), the blocks above renumbered
    keep, gone = split_keywords(pk, n)
    anchors = [r[0] for r in store.db.execute("SELECT doi FROM papers WHERE retired_pillar=?", (key,))
               if r[0] in {str(d).lower() for d in cfg.get("anchors") or []}]
    names = names[:n - 1] + names[n:]
    shorts = _trim(shorts[:n - 1] + shorts[n:])
    bak = curation.system_dir(cfg) / "retired_pillars_config.json"
    hist = _history(bak)
    hist.append({"key": key, "id": n, "name": name, "short": short, "retired_at": date.today().isoformat(),
                 "positive_keywords": gone, "anchors": anchors})
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    changed = control_center.write_config([(["pillars"], names), (["pillar_short"], shorts),
                                           (["positive_keywords"], keep)], cfg)

    # 4. the antique record
    old[key] = {"id": n, "name": name, "short": short, "retired_at": date.today().isoformat(), "papers": stamped}
    store.meta("retired_pillars", old)
    log(f"🏺 Retired pillar {n} '{name}' as '{key}': {stamped} papers stamped ({back} unreviewed candidates back to the "
        f"corpus), {moved + frozen} papers kept on their renumbered pillar, {len(gone)} keywords saved in {bak.name}; "
        f"config.json: {', '.join(changed) or 'unchanged'}")
    return {"key": key, "stamped": stamped, "back": back, "frozen": moved + frozen, "keywords": len(gone)}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:40] or "pillar"


def unretire_pillar(store, cfg, key, log=print):
    import control_center
    import curation
    old = antiques(store)
    if key not in old:
        raise ValueError(f"no retired pillar {key!r} (have: {', '.join(old) or 'none'})")
    a = old[key]
    names, shorts, pk = config_lists(cfg)
    if len(names) >= MAX_PILLARS:
        raise ValueError(f"already {len(names)} pillars (the most): retire or delete one first")
    name = str(a.get("name") or key)
    if name.lower() in (x.lower() for x in names):
        raise ValueError(f"a pillar called {name!r} exists already: rename it first")
    bak = curation.system_dir(cfg) / "retired_pillars_config.json"
    hist = _history(bak)
    entry = next((h for h in reversed(hist) if h.get("key") == key and not h.get("restored_at") and not h.get("dropped_at")), None)
    m = len(names) + 1
    block, skipped = {}, []
    for k, w in ((entry or {}).get("positive_keywords") or {}).items():
        mm = MARK_RX.match(k)
        if mm:
            block[f"_pillar{m}{mm[2]}"] = 0
        elif k in pk:
            skipped.append(k)  # meanwhile filed under another pillar: it stays there
        else:
            block[k] = w
    if not any(MARK_RX.match(k) for k in block):
        block = {f"_pillar{m}_{_slug(name)}": 0, **block}
    marker = next(k for k in block if MARK_RX.match(k))
    block = {marker: 0, **{k: w for k, w in block.items() if k != marker}}
    shorts = shorts + [""] * (m - 1 - len(shorts)) + [str(a.get("short") or "")]
    n = store.db.execute("UPDATE papers SET retired_pillar=NULL, pillar=? WHERE retired_pillar=?", (m, key)).rowcount
    store.db.commit()
    changed = control_center.write_config([(["pillars"], names + [name]), (["pillar_short"], _trim(shorts)),
                                           (["positive_keywords"], {**pk, **block})], cfg)
    del old[key]
    store.meta("retired_pillars", old)
    if entry:
        entry["restored_at"] = date.today().isoformat()
        bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    kws = sum(1 for k in block if not MARK_RX.match(k))
    log(f"↺ Restored pillar '{name}' ({key}) as P{m}: {n} papers back under it, {kws} keywords"
        + (f" ({len(skipped)} now under another pillar stay there: {', '.join(skipped[:5])})" if skipped else "")
        + ("" if entry else "; no saved keywords found (retired_pillars_config.json): add them in Step 3")
        + f"; config.json: {', '.join(changed) or 'unchanged'}")
    return {"pillar": m, "papers": n, "keywords": kws, "skipped": skipped}


def delete_pillar(store, cfg, n, log=print):
    import catalog
    import control_center
    import curation
    names, shorts, pk = config_lists(cfg)
    _check_n(names, n, "delete")
    name = names[n - 1]
    kwmap = catalog._kw_pillars(cfg)
    gone_papers = back = kept = 0
    for p in store.where("COALESCE(grp,'ieee') != 'beyond' AND retired_pillar IS NULL"):
        if _is_mine(p, n, kwmap):
            upd = {"pillar": 0}  # no pillar: a digest's Pillar line or keyword votes never re-file it
            if store.db.execute(f"SELECT 1 FROM papers WHERE doi=? AND {UNREVIEWED}", (p["doi"],)).fetchone():
                upd["status"] = "archive"
                back += 1
            store.update(p["doi"], upd)
            gone_papers += 1
    kept = gone_papers - back
    store.db.commit()
    moved, frozen = remap_papers(store, {n: 0, **{k: k - 1 for k in range(n + 1, len(names) + 1)}}, log)
    keep, gone = split_keywords(pk, n)
    bak = curation.system_dir(cfg) / "deleted_pillars_config.json"
    hist = _history(bak)
    hist.append({"id": n, "name": name, "short": shorts[n - 1] if n - 1 < len(shorts) else "",
                 "deleted_at": date.today().isoformat(), "positive_keywords": gone})
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    changed = control_center.write_config([(["pillars"], names[:n - 1] + names[n:]),
                                           (["pillar_short"], _trim(shorts[:n - 1] + shorts[n:])),
                                           (["positive_keywords"], keep)], cfg)
    log(f"🗑 Deleted pillar {n} '{name}' (no 🏺 antique): {gone_papers} papers left without a pillar ({back} unreviewed "
        f"candidates back to the corpus, {kept} curated / reported papers kept in the store), {moved + frozen} papers "
        f"kept on their renumbered pillar, {len(gone)} keywords removed (a copy in {bak.name}); "
        f"config.json: {', '.join(changed) or 'unchanged'}")
    return {"papers": gone_papers, "back": back, "kept": kept, "frozen": moved + frozen, "keywords": len(gone)}


def drop_antique(store, cfg, key, log=print):
    import curation
    old = antiques(store)
    if key not in old:
        raise ValueError(f"no retired pillar {key!r} (have: {', '.join(old) or 'none'})")
    n = store.db.execute("UPDATE papers SET retired_pillar=NULL, pillar=0 WHERE retired_pillar=?", (key,)).rowcount
    store.db.commit()
    del old[key]
    store.meta("retired_pillars", old)
    bak = curation.system_dir(cfg) / "retired_pillars_config.json"
    hist = _history(bak)
    for h in reversed(hist):
        if h.get("key") == key and not h.get("restored_at") and not h.get("dropped_at"):
            h["dropped_at"] = date.today().isoformat()
            bak.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
            break
    log(f"🗑 Dropped the 🏺 tag '{key}': {n} papers keep their marks and notes, without a pillar")
    return {"papers": n}


def drop_topic(store, cfg, slug, log=print):
    """v3.4.2 🗑 Delete (--drop-topic): the dive is gone for good, unlike 🏺 Retire. It leaves meta['topics'], its
    report files (<date>_<slug>.md / .html and <date>_recall_<slug>.*) are deleted, and its papers that no other dive
    lists and that carry no curation (normal mark, no ⭐, 🏷️, Si call or 📌) go from status 'topic' back to the corpus
    ('archive'). Curated papers keep everything."""
    from pathlib import Path
    topics = store.meta("topics") or {}
    t = topics.pop(slug, None)
    if t is None:
        raise ValueError(f"no topic dive {slug!r} (have: {', '.join(topics) or 'none'})")
    others = {str(d).lower() for x in topics.values() for d in x.get("dois") or []}
    back = kept = 0
    for d in {str(d).lower() for d in t.get("dois") or []} - others:
        if store.db.execute("SELECT 1 FROM papers WHERE doi=? AND status='topic' AND COALESCE(user_mark,'normal')='normal' "
                            "AND COALESCE(cited,0)=0 AND COALESCE(user_note,'')='' AND user_silicon IS NULL AND pin IS NULL",
                            (d,)).fetchone():
            store.update(d, {"status": "archive"}, commit=False)
            back += 1
        else:
            kept += 1
    store.db.commit()
    store.meta("topics", topics)
    td = Path(cfg["paths"]["topic_dir"])
    files = [td / f"{t.get('date')}_{slug}{ext}" for ext in (".md", ".html")]
    files += list(td.glob(f"*_recall_{slug}.md")) + list(td.glob(f"*_recall_{slug}.html")) if td.exists() else []
    gone = []
    for f in files:
        if f.exists():
            f.unlink()
            gone.append(f.name)
    log(f"🗑 Deleted topic dive {slug}: {back} uncurated papers back to the corpus, {kept} kept (curated or in another "
        f"dive), {len(gone)} report file(s) removed{': ' + ', '.join(gone) if gone else ''} ({len(topics)} dives left)")
    return {"back": back, "kept": kept, "files": gone}


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
