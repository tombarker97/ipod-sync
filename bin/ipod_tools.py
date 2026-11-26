#!/usr/bin/env python3
"""ipod_tools.py - helpers used by ipod-sync.sh for a Rockbox iPod.

Subcommands
  scrobble-collect   Read Rockbox playback logs from the iPod, turn them into
                     Audioscrobbler 1.1 logs on the Mac, archive the raw logs,
                     then (real mode only) clear the logs on the iPod.
  scrobble-upload    Upload pending scrobble logs with rb-scrobbler.
  playlists-push     Write ~/Music/Playlists/*.m3u8 onto the iPod in Rockbox form.
  playlists-export   One-off: export Music.app playlists to ~/Music/Playlists.
  prune              Delete files from the iPod's /Music that aren't in the library.
  report             Count stray audio files on the iPod outside /Music.

Needs Python 3.9+ and mutagen (pip install mutagen).
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

try:
    import mutagen
except ImportError:
    mutagen = None

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".oga", ".opus",
             ".wav", ".aif", ".aiff", ".wma", ".mpc", ".ape", ".wv"}
MIN_LENGTH_MS = 30_000          # Last.fm: track must be longer than 30 s
FOUR_MIN_MS = 240_000           # ...and played for half its length or 4 min
MAX_AGE_S = 14 * 86400 - 3600   # Last.fm accepts up to 14 days; keep 1 h margin
FUTURE_SLACK_S = 600
CLOCK_FLOOR = 1420070400        # 2015-01-01: anything earlier = clock not set


def log(msg=""):
    print(msg, flush=True)


def record(key, value):
    """Save stats for the sync report (only when ipod-sync.sh sets IPOD_SYNC_RUN)."""
    path = os.environ.get("IPOD_SYNC_RUN")
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data[key] = value
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def nkey(rel):
    """Comparison key for paths: Unicode-normalised, case-insensitive."""
    return unicodedata.normalize("NFC", rel).replace("\\", "/").casefold()


def index_tree(root, audio_only=True):
    """Map nkey(relative path) -> actual relative path for audio under root."""
    idx = {}
    if not os.path.isdir(root):
        return idx
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            if not audio_only or os.path.splitext(f)[1].lower() in AUDIO_EXT:
                rel = os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                idx[nkey(rel)] = rel
    return idx


def core_name(fname, artist_dir=""):
    """'01 KETTAMA - Archangel.mp3' and '01 Archangel.mp3' -> 'archangel'."""
    stem = unicodedata.normalize("NFKC", os.path.splitext(fname)[0]).casefold()
    stem = re.sub(r"^(?:\d+-)?\d+[ ._-]+", "", stem)
    a = unicodedata.normalize("NFKC", artist_dir).casefold()
    if a and stem.startswith(a + " - "):
        stem = stem[len(a) + 3:]
    return re.sub(r"[\W_]+", "", stem)


def track_prefix(fname):
    m = re.match(r"^((?:\d+-)?\d+)[ ._-]", fname)
    return m.group(1).lstrip("0") if m else None


def find_renamed(rel, lib_idx, dir_cache):
    """Same Artist/Album folder, file renamed since the play was logged."""
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    folder = nkey("/".join(parts[:-1]))
    if folder not in dir_cache:
        dir_cache[folder] = [r for k, r in lib_idx.items() if k.rsplit("/", 1)[0] == folder]
    cands = dir_cache[folder]
    artist_dir = parts[-3] if len(parts) >= 3 else ""
    want = core_name(parts[-1], artist_dir)
    hits = [r for r in cands if want and core_name(r.split("/")[-1], artist_dir) == want]
    if len(hits) == 1:
        return hits[0]
    tp = track_prefix(parts[-1])
    hits = [r for r in cands if tp and track_prefix(r.split("/")[-1]) == tp
            and os.path.splitext(r)[1].lower() == os.path.splitext(parts[-1])[1].lower()]
    return hits[0] if len(hits) == 1 else None


def clean(s):
    return re.sub(r"[\t\r\n]+", " ", (s or "")).strip()


def read_tags(path):
    """Return dict(artist, album, title, track, mbid, albumartist) or None."""
    if mutagen is None:
        return None
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return None
    if f is None or not f.tags:
        return None
    t = f.tags

    def g(k):
        try:
            v = t.get(k)
        except Exception:
            v = None
        return clean(v[0]) if v else ""

    tr = g("tracknumber").split("/")[0].strip()
    return {
        "artist": g("artist") or g("albumartist"),
        "album": g("album"),
        "title": g("title"),
        "track": tr if tr.isdigit() else "0",
        "mbid": g("musicbrainz_trackid"),
        "albumartist": g("albumartist"),
    }


def tags_from_path(rel):
    """Fallback when a file has no readable tags: Artist/Album/NN Title.ext"""
    parts = rel.split("/")
    stem = os.path.splitext(parts[-1])[0]
    m = re.match(r"^(?:(\d+)-)?(\d+)[ ._-]+(.*)$", stem)
    title, track = (m.group(3), m.group(2)) if m else (stem, "0")
    return {
        "artist": parts[-3] if len(parts) >= 3 else "",
        "album": parts[-2] if len(parts) >= 2 else "",
        "title": title,
        "track": str(int(track)) if track.isdigit() else "0",
        "mbid": "",
        "albumartist": parts[-3] if len(parts) >= 3 else "",
    }


def local_to_utc(ts, tzname):
    """Rockbox stores the iPod's wall-clock time as if it were UTC.
    Re-interpret it in tzname (DST-aware) to get a true UTC timestamp."""
    if tzname.upper() == "UTC" or ZoneInfo is None:
        return int(ts)
    wall = datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
    return int(wall.replace(tzinfo=ZoneInfo(tzname)).timestamp())


def iso(ts, tzname=None, label=True):
    dt = datetime.fromtimestamp(int(ts), timezone.utc)
    if not label:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if tzname and ZoneInfo is not None and tzname.upper() != "UTC":
        dt = dt.astimezone(ZoneInfo(tzname))
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


AS_HEADER = "#AUDIOSCROBBLER/1.1\n#TZ/UTC\n#CLIENT/Rockbox ipodmini2g (ipod_tools.py)\n"


def as_line(p):
    fields = [p["artist"], p["album"], p["title"], p["track"] or "0",
              str(int(p["length_ms"] // 1000)), "L", str(p["utc"]), p["mbid"]]
    if p.get("albumartist"):
        fields.append(p["albumartist"])
    return "\t".join(clean(x) for x in fields) + "\n"


def write_as(path, plays):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(AS_HEADER)
        for p in plays:
            fh.write(as_line(p))


# --------------------------------------------------------------------------
# scrobble-collect
# --------------------------------------------------------------------------

def parse_playback_log(path):
    """Rockbox playback.log line: end_ts:elapsed_ms:length_ms:/<HDD0>/path
    The timestamp is written when the track FINISHES (device local time)."""
    out, bad = [], 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line or not line[0].isdigit():
                continue
            parts = line.split(":", 3)
            if len(parts) != 4:
                bad += 1
                continue
            try:
                ts, el, ln = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                bad += 1
                continue
            if ln <= 0 or el > ln:          # same validity rule as Rockbox's plugin
                bad += 1
                continue
            p = re.sub(r"^/<[^>]+>", "", parts[3])   # /<HDD0>/Music/.. -> /Music/..
            out.append({"ts": ts, "elapsed_ms": el, "length_ms": ln, "path": p,
                        "key": "%d:%d:%d:%s" % (ts, el, ln, nkey(p))})
    return out, bad


def parse_legacy_scrobbler_log(path):
    """A /.scrobbler.log made earlier by Rockbox's own Last.fm plugin."""
    out, utc_header = [], False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith("#"):
                if line.upper().startswith("#TZ/UTC"):
                    utc_header = True
                continue
            f = line.split("\t")
            if len(f) < 7 or f[5] != "L":
                continue
            try:
                ts, length_s = int(f[6]), int(f[4] or 0)
            except ValueError:
                continue
            out.append({
                "artist": clean(f[0]), "album": clean(f[1]), "title": clean(f[2]),
                "track": f[3] if f[3].isdigit() else "0",
                "length_ms": length_s * 1000, "mbid": clean(f[7]) if len(f) > 7 else "",
                "albumartist": clean(f[8]) if len(f) > 8 else "",
                # the plugin stamps the end of play; approximate the start
                "ts_local_start": ts - length_s, "utc_header": None,
                "path": "(legacy .scrobbler.log)", "key": "legacy:" + line,
            })
    for e in out:
        e["utc_header"] = utc_header
    return out


def dedupe_resumes(entries):
    """Rockbox logs a track again when playback is resumed. Like the on-device
    plugin: same file+length with timestamps within one track length = one
    play; keep the entry with the greatest elapsed time."""
    groups = {}
    for e in entries:
        groups.setdefault((nkey(e["path"]), e["length_ms"]), []).append(e)
    kept, dropped = [], 0
    for (_, length_ms), items in groups.items():
        items.sort(key=lambda e: e["ts"])
        cluster = [items[0]]
        for e in items[1:]:
            if e["ts"] - cluster[0]["ts"] <= length_ms / 1000:
                cluster.append(e)
            else:
                kept.append(max(cluster, key=lambda x: x["elapsed_ms"]))
                dropped += len(cluster) - 1
                cluster = [e]
        kept.append(max(cluster, key=lambda x: x["elapsed_ms"]))
        dropped += len(cluster) - 1
    kept.sort(key=lambda e: e["ts"])
    return kept, dropped


def classify(utc, now):
    if utc < CLOCK_FLOOR:
        return "bad-clock"
    if utc > now + FUTURE_SLACK_S:
        return "future"
    if utc < now - MAX_AGE_S:
        return "too-old"
    return "ok"


def load_ledger(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {l.rstrip("\n") for l in fh if l.strip()}


def cmd_scrobble_collect(a):
    now = int(time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    rbdir = os.path.join(a.ipod, ".rockbox")
    rotated = sorted(glob.glob(os.path.join(rbdir, "playback_[0-9][0-9][0-9][0-9].log")))
    current = os.path.join(rbdir, "playback.log")
    sources = rotated + ([current] if os.path.exists(current) else [])
    legacy = os.path.join(a.ipod, ".scrobbler.log")
    has_legacy = os.path.exists(legacy)
    old_copy = os.path.join(rbdir, "playback_old.log")   # plugin's own backup

    if not sources and not has_legacy:
        log("Scrobbles: no playback log on the iPod (is 'Playback Log' switched on?)")
        return 0

    out_dir = os.path.join(a.data, "dry-run", stamp) if a.dry_run else a.data
    for sub in ("raw", "pending", "unsubmittable", "reports"):
        os.makedirs(os.path.join(out_dir, sub) if not a.dry_run else out_dir, exist_ok=True)
    def dest(sub, name):
        return os.path.join(out_dir, name) if a.dry_run else os.path.join(out_dir, sub, name)

    ledger_path = os.path.join(a.data, "ledger.txt")
    ledger = load_ledger(ledger_path)

    # 1. read
    entries, bad = [], 0
    for s in sources:
        e, b = parse_playback_log(s)
        entries += e
        bad += b
    total_read = len(entries)
    entries = [e for e in entries if e["key"] not in ledger]
    already = total_read - len(entries)
    new_keys = [e["key"] for e in entries]
    entries, resumed = dedupe_resumes(entries)

    lib_idx = index_tree(a.library)
    dir_cache = {}
    plays, short = [], 0
    for e in entries:
        if e["length_ms"] <= MIN_LENGTH_MS or not (
                e["elapsed_ms"] * 2 >= e["length_ms"] or e["elapsed_ms"] >= FOUR_MIN_MS):
            short += 1
            continue
        rel = e["path"][len("/Music/"):] if e["path"].startswith("/Music/") else e["path"].lstrip("/")
        meta, source = None, ""
        lib_rel = lib_idx.get(nkey(rel))
        if lib_rel:
            meta, source = read_tags(os.path.join(a.library, lib_rel)), "library"
        if meta is None:
            moved = find_renamed(rel, lib_idx, dir_cache)
            if moved:
                meta, source = read_tags(os.path.join(a.library, moved)), "library (renamed)"
        if meta is None:
            ip = os.path.join(a.ipod, e["path"].lstrip("/"))
            if os.path.exists(ip):
                meta, source = read_tags(ip), "ipod"
        if meta is None or not meta["artist"] or not meta["title"]:
            meta, source = tags_from_path(rel), "filename"
        start_local = e["ts"] - int(round(e["elapsed_ms"] / 1000))
        p = dict(meta)
        p.update(length_ms=e["length_ms"], local=start_local,
                 utc=local_to_utc(start_local, a.tz), path=e["path"], tagsrc=source)
        plays.append(p)

    # legacy /.scrobbler.log from manual on-device exports
    legacy_entries, legacy_dupes = [], 0
    if has_legacy:
        for e in parse_legacy_scrobbler_log(legacy):
            if e["key"] in ledger:
                continue
            utc = e["ts_local_start"] if e["utc_header"] else local_to_utc(e["ts_local_start"], a.tz)
            dup = any(nkey(p["artist"]) == nkey(e["artist"]) and nkey(p["title"]) == nkey(e["title"])
                      and abs(p["utc"] - utc) <= e["length_ms"] / 1000 + 300 for p in plays)
            if dup:
                legacy_dupes += 1
                continue
            p = dict(e)
            p.update(local=e["ts_local_start"], utc=utc, tagsrc="legacy-log")
            legacy_entries.append(p)
    plays += legacy_entries
    plays.sort(key=lambda p: p["utc"])

    ok, held = [], []
    for p in plays:
        p["status"] = classify(p["utc"], now)
        (ok if p["status"] == "ok" else held).append(p)

    # 2. write outputs
    if ok:
        write_as(dest("pending", "scrobbler-%s.log" % stamp), ok)
    if held:
        write_as(dest("unsubmittable", "unsubmittable-%s.log" % stamp), held)
    with open(dest("reports", "collect-%s.tsv" % stamp), "w", encoding="utf-8") as fh:
        fh.write("status\tiPod clock (as logged)\tUTC sent to Last.fm\t%s time\ttag source\tartist\ttitle\tpath\n" % a.tz)
        for p in plays:
            fh.write("\t".join([p["status"], iso(p["local"], label=False), iso(p["utc"]), iso(p["utc"], a.tz),
                                p["tagsrc"], p["artist"], p["title"], p["path"]]) + "\n")

    log("Scrobbles: read %d log entries (+%d bad lines), %d already processed, "
        "%d resume duplicates, %d too short/skipped" % (total_read, bad, already, resumed, short))
    if has_legacy:
        log("Scrobbles: legacy /.scrobbler.log: %d new, %d duplicates of playback.log"
            % (len(legacy_entries), legacy_dupes))
    reasons = {}
    for p in held:
        reasons[p["status"]] = reasons.get(p["status"], 0) + 1
    log("Scrobbles: %d ready to upload, %d held back%s" % (
        len(ok), len(held), (" (" + ", ".join("%s: %d" % kv for kv in sorted(reasons.items())) + ")") if reasons else ""))
    fn = sum(1 for p in plays if p["tagsrc"] == "filename")
    if fn:
        log("Scrobbles: %d plays had no readable tags - names taken from the file path" % fn)
    if ok:
        log("Scrobbles: time check - iPod clock (%s) -> what Last.fm receives:" % a.tz)
        for p in ok[-5:]:
            log("   %s  ->  %s   %s - %s" % (iso(p["local"], label=False), iso(p["utc"]),
                                             p["artist"], p["title"]))

    record("scrobbles", {
        "read": total_read, "ready": len(ok), "held": reasons, "short": short,
        "from_filename": fn, "dry_run": a.dry_run,
        "plays": [{"artist": p["artist"], "title": p["title"], "album": p.get("album", ""),
                   "when": iso(p["utc"], a.tz)} for p in ok][-500:],
    })
    if a.dry_run:
        log("Scrobbles: DRY RUN - previews in %s ; iPod logs left untouched" % out_dir)
        return 0

    # 3. archive raw logs, verify, then clear them on the iPod
    archived_ok = True
    for s in sources + ([legacy] if has_legacy else []) + ([old_copy] if os.path.exists(old_copy) else []):
        name = os.path.basename(s).lstrip(".")
        d = dest("raw", "%s-%s" % (stamp, name))
        shutil.copy2(s, d)
        if os.path.getsize(d) != os.path.getsize(s):
            archived_ok = False
            log("Scrobbles: archive copy of %s does not match - iPod log NOT cleared" % s)
    if not archived_ok:
        return 1
    with open(ledger_path, "a", encoding="utf-8") as fh:
        for k in new_keys:
            fh.write(k + "\n")
        for e in legacy_entries:
            fh.write(e["key"] + "\n")
    for s in rotated:
        os.remove(s)
    if os.path.exists(current):
        open(current, "w").close()          # truncate; Rockbox keeps appending
    if has_legacy:
        os.remove(legacy)
    log("Scrobbles: raw logs archived to %s and cleared on the iPod" % dest("raw", ""))
    return 0


# --------------------------------------------------------------------------
# scrobble-upload
# --------------------------------------------------------------------------

def read_as(path):
    header, rows = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            (header if line.startswith("#") else rows).append(line)
    return header, rows


def cmd_scrobble_upload(a):
    pend = sorted(glob.glob(os.path.join(a.data, "pending", "*.log")))
    if not pend:
        log("Upload: nothing pending from earlier syncs" +
            (" (this sync's plays are in the dry-run preview)" if a.dry_run else ""))
        record("upload", {"uploaded": 0, "failed": False, "dry_run": a.dry_run})
        return 0
    now = int(time.time())
    total, uploaded = 0, 0
    for f in pend:
        header, rows = read_as(f)
        keep, expired = [], []
        for r in rows:
            fields = r.rstrip("\n").split("\t")
            try:
                utc = int(fields[6])
            except (IndexError, ValueError):
                expired.append(r)
                continue
            (keep if now - utc < MAX_AGE_S else expired).append(r)
        if expired and not a.dry_run:
            os.makedirs(os.path.join(a.data, "unsubmittable"), exist_ok=True)
            xp = os.path.join(a.data, "unsubmittable", "expired-" + os.path.basename(f))
            with open(xp, "w", encoding="utf-8") as fh:
                fh.writelines([AS_HEADER] + expired)
            log("Upload: %d plays in %s passed Last.fm's 14-day limit - moved to unsubmittable/"
                % (len(expired), os.path.basename(f)))
        total += len(keep)
        if a.dry_run:
            log("Upload: DRY RUN - would upload %d plays from %s" % (len(keep), os.path.basename(f)))
            continue
        if not keep:
            os.remove(f)
            continue
        with open(f, "w", encoding="utf-8") as fh:
            fh.writelines([AS_HEADER] + keep)
        if not a.rb_scrobbler or not os.access(a.rb_scrobbler, os.X_OK):
            log("Upload: rb-scrobbler not found at %s - %d plays left pending" % (a.rb_scrobbler, len(keep)))
            record("upload", {"uploaded": uploaded, "failed": True, "dry_run": a.dry_run,
                              "message": "rb-scrobbler not found at %s" % a.rb_scrobbler})
            return 2
        tmp = os.path.join(a.data, "pending", ".uploading.tmp")   # not matched by *.log
        shutil.copy2(f, tmp)
        r = subprocess.run([a.rb_scrobbler, "-f", tmp, "-o", "0", "-n", "delete-on-success", "-c=false"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
        out = r.stdout.strip().splitlines()
        log("Upload: " + (out[-2] if len(out) >= 2 else (out[-1] if out else "(no output)")))
        if r.returncode == 0 and not os.path.exists(tmp):
            os.makedirs(os.path.join(a.data, "sent"), exist_ok=True)
            shutil.move(f, os.path.join(a.data, "sent", os.path.basename(f)))
            log("Upload: %s done - %d plays" % (os.path.basename(f), len(keep)))
            uploaded += len(keep)
        else:
            if os.path.exists(tmp):
                os.remove(tmp)
            log("Upload: %s failed (exit %d) - kept in pending/ for next time:\n   %s"
                % (os.path.basename(f), r.returncode, "\n   ".join(out[-5:])))
            log("Upload: %d plays uploaded to Last.fm" % uploaded)
            record("upload", {"uploaded": uploaded, "failed": True, "dry_run": a.dry_run,
                              "message": " / ".join(out[-3:]) or "rb-scrobbler failed"})
            return 2
    if not a.dry_run:
        log("Upload: %d plays uploaded to Last.fm" % uploaded)
    record("upload", {"uploaded": uploaded, "would_upload": total if a.dry_run else 0,
                      "failed": False, "dry_run": a.dry_run})
    return 0


# --------------------------------------------------------------------------
# playlists
# --------------------------------------------------------------------------

MANIFEST = ".mac-playlists.txt"


def cmd_playlists_push(a):
    src = sorted(glob.glob(os.path.join(a.playlists, "*.m3u8")) + glob.glob(os.path.join(a.playlists, "*.m3u")))
    dst = os.path.join(a.ipod, "Playlists")
    ipod_idx = index_tree(os.path.join(a.ipod, "Music"))
    if not ipod_idx:
        log("Playlists: can't see any music on the iPod - playlists left unchanged "
            "(if this runs automatically, check Full Disk Access)")
        return 1
    lib_root = os.path.abspath(a.library).rstrip("/") + "/"
    written, missing_total, stats = [], 0, []
    for m in src:
        name = os.path.splitext(os.path.basename(m))[0] + ".m3u8"
        out, missing = [], []
        with open(m, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(lib_root):
                    rel = line[len(lib_root):]
                elif line.startswith("/Music/"):
                    rel = line[len("/Music/"):]
                else:
                    rel = line.lstrip("/")
                hit = ipod_idx.get(nkey(rel))
                if hit:
                    out.append("/Music/" + hit)        # exact spelling as stored on the iPod
                else:
                    missing.append(rel)
        missing_total += len(missing)
        log("Playlists: %-40s %4d tracks%s" % (name, len(out),
            ("  (%d not on iPod)" % len(missing)) if missing else ""))
        for x in missing[:3]:
            log("            missing: %s" % x)
        stats.append({"name": name[:-5], "tracks": len(out), "missing": len(missing)})
        if not a.dry_run:
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, name), "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(out) + ("\n" if out else ""))
        written.append(name)
    # remove playlists this script created earlier that no longer exist on the Mac
    mpath = os.path.join(dst, MANIFEST)
    previous = set()
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as fh:
            previous = {l.strip() for l in fh if l.strip()}
    stale = sorted(previous - set(written))
    for s in stale:
        log("Playlists: %s removed (no longer in %s)" % (s, a.playlists))
        if not a.dry_run and os.path.exists(os.path.join(dst, s)):
            os.remove(os.path.join(dst, s))
    if not a.dry_run and (written or previous):
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(written) + "\n")
    record("playlists", {"lists": stats, "removed": stale, "dry_run": a.dry_run})
    if not src:
        log("Playlists: none found in %s" % a.playlists)
    elif a.dry_run:
        log("Playlists: DRY RUN - nothing written; 'not on iPod' counts reflect the iPod "
            "before this sync's file copy")
    return 0


OSA_NAMES = '''
on run argv
  tell application "Music"
    if (item 1 of argv) is "yes" then
      set ns to name of every user playlist whose special kind is none
    else
      set ns to name of every user playlist whose special kind is none and smart is false
    end if
  end tell
  set AppleScript's text item delimiters to linefeed
  return ns as text
end run
'''

OSA_TRACKS = '''
on run argv
  set out to {}
  tell application "Music"
    set p to user playlist (item 1 of argv)
    set n to count of file tracks of p
    repeat with i from 1 to n
      set t to file track i of p
      set loc to ""
      try
        set loc to POSIX path of (location of t)
      end try
      set end of out to (artist of t) & tab & (album artist of t) & tab & (album of t) & tab & (name of t) & tab & (track number of t as text) & tab & loc
    end repeat
  end tell
  set AppleScript's text item delimiters to linefeed
  return out as text
end run
'''


def osa(script, *args):
    r = subprocess.run(["osascript", "-", *args], input=script, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.rstrip("\n")


FEAT_RE = re.compile(r"\s+[\(\[]?(feat|ft|featuring|with)\.?\s.*$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = FEAT_RE.sub("", s)
    s = s.split(";")[0]
    return re.sub(r"[^\w]+", "", s)


def strip_tracknum(name):
    stem, ext = os.path.splitext(name)
    return nkey(re.sub(r"^(?:\d+-)?\d+[ ._-]+", "", stem) + ext)


def build_tag_index(library, cache_path):
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}
    idx, fresh = index_tree(library), {}
    for k, rel in idx.items():
        p = os.path.join(library, rel)
        st = os.stat(p)
        sig = "%d:%d" % (st.st_size, int(st.st_mtime))
        c = cache.get(rel)
        if c and c.get("sig") == sig:
            fresh[rel] = c
        else:
            t = read_tags(p) or tags_from_path(rel)
            fresh[rel] = {"sig": sig, "artist": t["artist"], "albumartist": t["albumartist"],
                          "album": t["album"], "title": t["title"]}
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(fresh, fh)
    by_tail, by_aat, by_at = {}, {}, {}
    for rel, t in fresh.items():
        parts = rel.split("/")
        if len(parts) >= 3:
            by_tail.setdefault(nkey("/".join(parts[-3:-1])) + "/" + strip_tracknum(parts[-1]), rel)
        for art in {norm(t["artist"]), norm(t["albumartist"])} - {""}:
            by_aat.setdefault((art, norm(t["album"]), norm(t["title"])), rel)
            by_at.setdefault((art, norm(t["title"])), []).append(rel)
    return idx, by_tail, by_aat, by_at


def cmd_playlists_export(a):
    lib = os.path.abspath(a.library)
    log("Export: indexing library tags (first run takes a minute)...")
    idx, by_tail, by_aat, by_at = build_tag_index(lib, os.path.join(a.data, "library-tags.json"))
    names = [n for n in osa(OSA_NAMES, "yes" if a.include_smart else "no").split("\n") if n.strip()]
    if a.only:
        names = [n for n in names if n in a.only]
    os.makedirs(a.playlists, exist_ok=True)
    log("Export: %d playlists in Music.app" % len(names))
    for name in names:
        try:
            raw = osa(OSA_TRACKS, name)
        except RuntimeError as e:
            log("Export: %s - skipped (%s)" % (name, e))
            continue
        out, missing = ["#EXTM3U"], []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            f = (line.split("\t") + [""] * 6)[:6]
            artist, albumartist, album, title, _trk, loc = f
            rel = None
            if loc and loc.startswith(lib + "/"):
                rel = loc[len(lib) + 1:] if nkey(loc[len(lib) + 1:]) in idx else None
            if not rel and loc:
                parts = loc.rstrip("/").split("/")
                if len(parts) >= 3:
                    rel = by_tail.get(nkey("/".join(parts[-3:-1])) + "/" + strip_tracknum(parts[-1]))
            if not rel:
                for art in (norm(albumartist), norm(artist)):
                    rel = by_aat.get((art, norm(album), norm(title)))
                    if rel:
                        break
            if not rel:
                cands = by_at.get((norm(artist), norm(title)), [])
                rel = cands[0] if len(cands) == 1 else None
            if rel:
                out.append(rel)
            else:
                missing.append("%s - %s" % (artist, title))
                out.append("# missing: %s - %s (%s)" % (artist, title, album))
        safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "Playlist"
        with open(os.path.join(a.playlists, safe + ".m3u8"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out) + "\n")
        log("Export: %-40s %4d matched, %d not found" % (safe, len(out) - 1 - len(missing), len(missing)))
    log("Export: written to %s" % a.playlists)
    return 0


# --------------------------------------------------------------------------
# prune: delete from the iPod's /Music what is no longer in the library
# (done here, not with rsync --delete, because macOS and the iPod's FAT card can
#  spell accented names differently, which makes rsync delete files it should keep)
# --------------------------------------------------------------------------

def cmd_prune(a):
    lib = index_tree(a.library, audio_only=False)
    music = os.path.join(a.ipod, "Music")
    if not lib:
        log("Prune: library looks empty - refusing to delete anything")
        return 1
    doomed, total = [], 0
    for d, dirs, files in os.walk(music):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            if f.startswith("."):
                continue
            total += 1
            rel = os.path.relpath(os.path.join(d, f), music).replace(os.sep, "/")
            if nkey(rel) not in lib:
                doomed.append(rel)
    listing = os.path.join(os.path.expanduser("~"), "Library", "Logs", "ipod-sync-prune.txt")
    os.makedirs(os.path.dirname(listing), exist_ok=True)
    with open(listing, "w", encoding="utf-8") as fh:
        fh.write("\n".join(doomed) + ("\n" if doomed else ""))
    if total and len(doomed) > total * 0.5 and not a.force:
        log("Prune: would delete %d of %d files on the iPod - that looks wrong, nothing deleted "
            "(list: %s)" % (len(doomed), total, listing))
        return 1
    record("prune", {"files": doomed[:500], "count": len(doomed), "dry_run": a.dry_run})
    if a.dry_run:
        log("Prune: %d files would be deleted from the iPod (list: %s)" % (len(doomed), listing))
        return 0
    for rel in doomed:
        os.remove(os.path.join(music, rel))
    removed_dirs = 0
    for d, dirs, files in os.walk(music, topdown=False):
        if d == music:
            continue
        left = os.listdir(d)
        if all(x.startswith(".") for x in left):
            for x in left:
                p = os.path.join(d, x)
                if os.path.isfile(p):
                    os.remove(p)
            try:
                os.rmdir(d)
                removed_dirs += 1
            except OSError:
                pass
    log("Prune: deleted %d files and %d empty folders from the iPod (list: %s)"
        % (len(doomed), removed_dirs, listing))
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

SYSTEM_DIRS = {".rockbox", "music", "playlists", ".trashes", ".spotlight-v100",
               ".fseventsd", ".temporaryitems", "system volume information"}


def cmd_report(a):
    in_music, appledouble, stray = 0, 0, {}
    for d, dirs, files in os.walk(a.ipod):
        rel_d = os.path.relpath(d, a.ipod)
        top = rel_d.split(os.sep)[0].lower() if rel_d != "." else ""
        if rel_d == ".":
            dirs[:] = [x for x in dirs if x.lower() not in SYSTEM_DIRS - {"music"}]
        for f in files:
            if f.startswith("._"):
                appledouble += 1
                continue
            if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
                continue
            if top == "music":
                in_music += 1
            else:
                key = rel_d.split(os.sep)[0] if rel_d != "." else "(iPod root)"
                if key.lower() == "ipod_control":
                    key = "iPod_Control (Apple firmware's own music)"
                stray[key] = stray.get(key, 0) + 1
    record("report", {"in_music": in_music, "stray": stray, "appledouble": appledouble})
    log("Report: %d audio files in /Music" % in_music)
    if stray:
        log("Report: %d audio files OUTSIDE /Music (not managed by the sync):" % sum(stray.values()))
        for k, v in sorted(stray.items(), key=lambda kv: -kv[1]):
            log("   %6d  %s" % (v, k))
    if appledouble:
        log("Report: %d macOS '._' shadow files present (dot_clean removes these)" % appledouble)
    return 0

# --------------------------------------------------------------------------
# summary: one-line notification text + HTML report + history
# --------------------------------------------------------------------------

def parse_rsync_changes(path):
    new, updated = [], []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith(">f"):
                    continue
                flags, _, rel = line.rstrip("\n").partition(" ")
                rel = rel.strip()
                if "\\#" in rel:   # rsync's \#ooo octal escapes for non-ASCII bytes
                    raw = re.sub(rb"\\#([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]),
                                 rel.encode("utf-8", "surrogateescape"))
                    rel = raw.decode("utf-8", "replace")
                if os.path.splitext(rel)[1].lower() not in AUDIO_EXT:
                    continue
                (new if "+++++++" in flags else updated).append(rel)
    return new, updated


def group_albums(paths):
    albums = {}
    for rel in paths:
        parts = rel.split("/")
        key = " — ".join(parts[:2]) if len(parts) >= 3 else "/".join(parts[:-1]) or "(top level)"
        albums[key] = albums.get(key, 0) + 1
    return sorted(albums.items())


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def cmd_summary(a):
    import html as H
    run = {}
    if os.environ.get("IPOD_SYNC_RUN") and os.path.exists(os.environ["IPOD_SYNC_RUN"]):
        try:
            with open(os.environ["IPOD_SYNC_RUN"], encoding="utf-8") as fh:
                run = json.load(fh)
        except Exception:
            run = {}
    dry = a.dry_run
    new, updated = parse_rsync_changes(a.files_list)
    prune = run.get("prune", {})
    removed = prune.get("files", [])
    removed_n = prune.get("count", len(removed))
    sc = run.get("scrobbles", {})
    up = run.get("upload", {})
    pl = run.get("playlists", {})
    rep = run.get("report", {})
    problems = [p for p in (a.problems or "").split("\n") if p.strip()]
    if up.get("failed"):
        problems.append("Last.fm upload failed (%s) - plays kept, will retry next sync"
                        % up.get("message", ""))
    notes = []
    if rep.get("stray"):
        notes.append("%d audio files are outside /Music on the iPod and aren't managed by the sync: %s"
                     % (sum(rep["stray"].values()), ", ".join(sorted(rep["stray"]))))
    scrobbled = up.get("uploaded", 0) if not dry else sc.get("ready", 0)
    held = sum(sc.get("held", {}).values()) if sc else 0
    started = float(a.started or time.time())
    took = max(0, int(time.time() - started))

    # ---- notification text
    parts = []
    if new:
        parts.append(("+" if not dry else "would add ") + plural(len(new), "song"))
    if updated:
        parts.append(plural(len(updated), "updated song"))
    if removed_n:
        parts.append(("−" if not dry else "would remove ") + plural(removed_n, "song"))
    if scrobbled:
        parts.append(plural(scrobbled, "play") + (" scrobbled" if not dry else " to scrobble"))
    detail = " · ".join(parts) or "Already up to date · no new plays"
    if dry:
        status, title = "dry", "iPod dry run finished"
    elif problems and not a.ejected:
        status, title = "error", "iPod sync problem - eject in Finder before unplugging"
    elif problems:
        status, title = "warn", "iPod synced with warnings - safe to unplug"
    else:
        status, title = "ok", "iPod synced - safe to unplug"
    if problems:
        detail = problems[0] + ("  (+%d more)" % (len(problems) - 1) if len(problems) > 1 else "")

    # ---- history
    os.makedirs(a.report_dir, exist_ok=True)
    hist_path = os.path.join(a.report_dir, "history.jsonl")
    entry = {"when": time.strftime("%Y-%m-%d %H:%M"), "mode": "dry run" if dry else "sync",
             "added": len(new), "updated": len(updated), "removed": removed_n,
             "scrobbled": scrobbled, "held": held, "problems": len(problems), "seconds": took}
    with open(hist_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    history = []
    with open(hist_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                history.append(json.loads(line))
            except Exception:
                pass
    history = history[-20:][::-1]

    # ---- HTML report
    e = H.escape
    colour = {"ok": "#2e9d5b", "warn": "#c98a12", "error": "#d0453c", "dry": "#5b6ee1"}[status]
    def tile(n, label):
        return '<div class="tile"><b>%s</b><span>%s</span></div>' % (n, e(label))
    def table(rows, head):
        if not rows:
            return '<p class="muted">Nothing.</p>'
        th = "".join("<th>%s</th>" % e(h) for h in head)
        trs = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % e(str(c)) for c in r) for r in rows)
        return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (th, trs)
    plays = sc.get("plays", [])
    lastfm = ('<a href="https://www.last.fm/user/%s">Open Last.fm</a>' % e(a.lastfm_user)) if a.lastfm_user else ""
    sections = []
    if problems:
        sections.append('<section class="problems"><h2>Needs attention</h2><ul>%s</ul>'
                        '<p class="muted">Full details: ~/Library/Logs/ipod-sync.log</p></section>'
                        % "".join("<li>%s</li>" % e(p) for p in problems))
    if notes:
        sections.append('<section><h2>Notes</h2><ul>%s</ul></section>'
                        % "".join("<li>%s</li>" % e(n) for n in notes))
    sections.append("<section><h2>Scrobbled to Last.fm %s</h2>%s%s</section>" % (
        lastfm,
        table([(p["when"], p["artist"], p["title"], p.get("album", "")) for p in reversed(plays)],
              ["Played", "Artist", "Title", "Album"]),
        ('<p class="muted">%s held back (too old or iPod clock wrong) - kept in ~/Music/Rockbox Scrobbles/unsubmittable</p>'
         % plural(held, "play")) if held else ""))
    sections.append("<section><h2>Added to the iPod</h2>%s</section>" % table(
        [(k, v) for k, v in group_albums(new)], ["Album", "Songs"]))
    if updated:
        sections.append("<section><h2>Updated (new tags or files)</h2>%s</section>" % table(
            [(k, v) for k, v in group_albums(updated)], ["Album", "Songs"]))
    sections.append("<section><h2>Removed from the iPod</h2>%s</section>" % table(
        [(k, v) for k, v in group_albums(removed)], ["Album", "Songs"]))
    lists = pl.get("lists", [])
    sections.append("<section><h2>Playlists</h2>%s</section>" % table(
        [(x["name"], x["tracks"], x["missing"] or "") for x in lists], ["Playlist", "Tracks", "Not on iPod"]))
    sections.append("<section><h2>Recent syncs</h2>%s</section>" % table(
        [(h["when"], h["mode"], h["added"], h["removed"], h["scrobbled"],
          "⚠ %d" % h["problems"] if h["problems"] else "✓") for h in history],
        ["When", "Type", "Added", "Removed", "Scrobbled", "Status"]))
    page = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>iPod sync - %(when)s</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf8;--fg:#1d1d1f;--muted:#6e6e73;--card:#fff;--line:#e5e5ea}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f7;--muted:#98989d;--card:#2c2c2e;--line:#3a3a3c}}
body{font:15px/1.45 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:32px;max-width:900px;margin:auto}
header{border-left:6px solid %(colour)s;padding:4px 0 4px 16px;margin-bottom:24px}
h1{font-size:24px;margin:0}h2{font-size:17px;margin:0 0 10px}h2 a{font-size:13px;font-weight:400;margin-left:8px;color:%(colour)s}
.muted{color:var(--muted);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;margin-bottom:24px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.tile b{display:block;font-size:26px}.tile span{color:var(--muted);font-size:13px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
section.problems{border-color:%(colour)s}
table{width:100%%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:500}
th,td{padding:6px 8px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}
</style></head><body>
<header><h1>%(title)s</h1><div class="muted">%(when)s · took %(took)ss · %(detail)s</div></header>
<div class="tiles">%(tiles)s</div>
%(sections)s
</body></html>""" % {
        "when": entry["when"], "colour": colour, "title": e(title), "took": took, "detail": e(detail),
        "tiles": tile(len(new), "songs added" if not dry else "songs to add")
                 + tile(removed_n, "songs removed" if not dry else "songs to remove")
                 + tile(scrobbled, "plays scrobbled" if not dry else "plays to scrobble")
                 + tile(held, "plays held back")
                 + tile(len(lists), "playlists")
                 + tile(rep.get("in_music", "–"), "songs on the iPod"),
        "sections": "\n".join(sections)}
    report = os.path.join(a.report_dir, "Last sync.html")
    with open(report, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(status)
    print(title)
    print(detail)
    print(report)
    return 0



def main():
    home = os.path.expanduser("~")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["scrobble-collect", "scrobble-upload", "playlists-push",
                                        "playlists-export", "prune", "report", "summary"])
    ap.add_argument("--ipod", default="/Volumes/IPOD")
    ap.add_argument("--library", default=os.path.join(home, "Music", "Music Library"))
    ap.add_argument("--data", default=os.path.join(home, "Music", "Rockbox Scrobbles"))
    ap.add_argument("--playlists", default=os.path.join(home, "Music", "Playlists"))
    ap.add_argument("--tz", default="UTC", help="time zone the iPod clock is set to (e.g. Europe/London)")
    ap.add_argument("--rb-scrobbler", default=os.path.join(home, ".ipod-sync", "rb-scrobbler"))
    ap.add_argument("--include-smart", action="store_true", help="playlists-export: include smart playlists")
    ap.add_argument("--only", nargs="*", help="playlists-export: only these playlist names")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="prune: allow deleting more than half the iPod")
    ap.add_argument("--files-list", help="summary: rsync --itemize-changes output")
    ap.add_argument("--problems", default="", help="summary: newline-separated problems")
    ap.add_argument("--started", help="summary: start time (epoch seconds)")
    ap.add_argument("--ejected", action="store_true", help="summary: the iPod was ejected")
    ap.add_argument("--report-dir", default=os.path.join(home, "Music", "iPod Sync"))
    ap.add_argument("--lastfm-user", default="")
    a = ap.parse_args()
    if mutagen is None and a.command in ("scrobble-collect", "playlists-export"):
        log("WARNING: mutagen not installed - tags will be guessed from file names")
    os.makedirs(a.data, exist_ok=True)
    fn = {"scrobble-collect": cmd_scrobble_collect, "scrobble-upload": cmd_scrobble_upload,
          "playlists-push": cmd_playlists_push, "playlists-export": cmd_playlists_export,
          "prune": cmd_prune, "report": cmd_report, "summary": cmd_summary}[a.command]
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
