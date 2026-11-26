"""Unit tests for bin/ipod_tools.py (no shell script involved)."""
import argparse
import calendar
import glob
import json
import os
import time
import unicodedata

import pytest

from conftest import TOOLS  # noqa: F401  (keeps the import path set up)
import ipod_tools as T

LONDON = "Europe/London"


def wall(y, mo, d, h, mi=0):
    """A Rockbox timestamp: the iPod's wall-clock time written as if it were UTC."""
    return calendar.timegm((y, mo, d, h, mi, 0))


def wall_now(tz=LONDON):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    off = datetime.now(ZoneInfo(tz)).utcoffset().total_seconds()
    return int(time.time() + off)


def nfd(s):
    return unicodedata.normalize("NFD", s)


def nfc(s):
    return unicodedata.normalize("NFC", s)


# ---------------------------------------------------------------- play logs

def test_parse_playback_log_good_and_bad_lines(tmp_path):
    p = tmp_path / "playback.log"
    p.write_bytes("\n".join([
        "# Rockbox playback log",
        "1795708246:200000:200000:/<HDD0>/Music/A/B/01 x.mp3",
        "1795708300:1000:250000:/<MMC0>/Music/A/B/02 Song: Remix.mp3\r",   # CRLF, colon in name
        "",
        "123:abc:4:/Music/bad.mp3",          # not a number
        "123:4",                             # too few fields
        "123:5:0:/Music/zero.mp3",           # length 0
        "123:500:400:/Music/over.mp3",       # played longer than the track
        "\x00\x00\x00\x00",                  # corruption (ignored)
        "1795708400:5:10:/Music/A/B/03 ü.mp3",
    ]).encode() + b"\n\xff\xfe garbage\n")
    out, bad = T.parse_playback_log(str(p))
    assert [e["path"] for e in out] == ["/Music/A/B/01 x.mp3", "/Music/A/B/02 Song: Remix.mp3",
                                        "/Music/A/B/03 ü.mp3"]
    assert bad == 4
    assert out[0]["ts"] == 1795708246 and out[0]["elapsed_ms"] == 200000
    assert out[0]["key"] == "1795708246:200000:200000:/music/a/b/01 x.mp3"


def test_dedupe_resumes_keeps_longest_listen():
    def e(ts, el, path="/Music/A/B/01 x.mp3", ln=300000):
        return {"ts": ts, "elapsed_ms": el, "length_ms": ln, "path": path}
    entries = [e(1000, 60000), e(1100, 290000), e(1200, 300000, "/MUSIC/a/b/01 X.mp3"),  # one play, resumed
               e(2000, 300000),                       # a second play, > 1 track length later
               e(1150, 100000, "/Music/A/B/02 y.mp3")]
    kept, dropped = T.dedupe_resumes(entries)
    assert dropped == 2
    assert [(k["ts"], k["elapsed_ms"]) for k in kept] == [(1150, 100000), (1200, 300000), (2000, 300000)]


@pytest.mark.parametrize("length,elapsed,ok", [
    (30000, 30000, False),       # exactly 30 s is too short ("longer than 30 s")
    (30001, 15001, True),        # just over 30 s, just over half
    (200000, 100000, True),      # exactly half
    (200000, 99999, False),
    (600000, 240000, True),      # 4 minutes of a 10 minute track
    (600000, 239999, False),
])
def test_counts_as_play(length, elapsed, ok):
    assert T.counts_as_play(length, elapsed) is ok


@pytest.mark.parametrize("w,utc", [
    # spring forward, Sun 29 Mar 2026: 01:00 GMT -> 02:00 BST
    (wall(2026, 3, 29, 0, 30), wall(2026, 3, 29, 0, 30)),
    (wall(2026, 3, 29, 2, 30), wall(2026, 3, 29, 1, 30)),
    # 01:30 never happened; zoneinfo reads it with the offset before the change
    (wall(2026, 3, 29, 1, 30), wall(2026, 3, 29, 1, 30)),
    # fall back, Sun 25 Oct 2026: 02:00 BST -> 01:00 GMT
    (wall(2026, 10, 25, 0, 30), wall(2026, 10, 24, 23, 30)),
    # 01:30 happened twice; we take the first (BST) one - up to 1 h out for that hour only
    (wall(2026, 10, 25, 1, 30), wall(2026, 10, 25, 0, 30)),
    (wall(2026, 10, 25, 2, 30), wall(2026, 10, 25, 2, 30)),
    (wall(2026, 7, 1, 12), wall(2026, 7, 1, 11)),
    (wall(2026, 12, 1, 12), wall(2026, 12, 1, 12)),
])
def test_local_to_utc_uk_clock_changes(w, utc):
    assert T.local_to_utc(w, LONDON) == utc


def test_local_to_utc_utc_is_unchanged():
    assert T.local_to_utc(wall(2026, 7, 1, 12), "UTC") == wall(2026, 7, 1, 12)


def test_classify():
    now = wall(2026, 11, 26, 12)
    assert T.classify(wall(2008, 1, 1, 0), now) == "bad-clock"
    assert T.classify(now + T.FUTURE_SLACK_S + 1, now) == "future"
    assert T.classify(now + T.FUTURE_SLACK_S, now) == "ok"
    assert T.classify(now - T.MAX_AGE_S, now) == "ok"
    assert T.classify(now - T.MAX_AGE_S - 1, now) == "too-old"


# ---------------------------------------------------------------- collect

def collect(sb, *extra):
    return sb.tools("scrobble-collect", "--ipod", sb.ipod, "--library", sb.library,
                    "--data", sb.scrobbles, "--tz", LONDON, *extra)


def pending_rows(sb):
    rows = []
    for f in sorted(glob.glob(os.path.join(sb.scrobbles, "pending", "*.log"))):
        rows += [line.rstrip("\n").split("\t") for line in open(f, encoding="utf-8")
                 if not line.startswith("#")]
    return rows


def test_collect_ledger_prevents_duplicates(sb):
    sb.make_ipod()
    sb.add_track("KETTAMA/Archangel/01 Archangel.mp3")
    line = "%d:200000:200000:/<HDD0>/Music/KETTAMA/Archangel/01 Archangel.mp3" % (wall_now() - 3600)
    sb.play_log([line])
    r = collect(sb)
    assert r.returncode == 0, r.stdout
    assert [(x[0], x[2]) for x in pending_rows(sb)] == [("KETTAMA", "Archangel")]
    assert open(os.path.join(sb.ipod, ".rockbox", "playback.log")).read() == ""   # cleared
    # the iPod's log comes back unchanged (e.g. clearing it failed last time)
    sb.play_log([line])
    r = collect(sb)
    assert "1 already processed" in r.stdout
    assert len(pending_rows(sb)) == 1


def test_collect_dry_run_leaves_ipod_and_ledger_alone(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 x.mp3")
    log = sb.play_log(["%d:200000:200000:/Music/A/B/01 x.mp3" % (wall_now() - 3600)])
    r = collect(sb, "--dry-run")
    assert r.returncode == 0, r.stdout
    assert open(log).read() != ""
    assert not os.path.exists(os.path.join(sb.scrobbles, "ledger.txt"))
    assert pending_rows(sb) == []
    assert len(glob.glob(os.path.join(sb.scrobbles, "dry-run", "*", "scrobbler-*.log"))) == 1


def test_collect_holds_back_bad_clock_and_old_plays(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 x.mp3")
    sb.play_log(["%d:200000:200000:/Music/A/B/01 x.mp3" % wall(2008, 5, 1, 12),
                 "%d:200000:200000:/Music/A/B/01 x.mp3" % (wall_now() - 20 * 86400),
                 "%d:200000:200000:/Music/A/B/01 x.mp3" % (wall_now() + 86400)])
    r = collect(sb)
    assert "0 ready to upload, 3 held back (bad-clock: 1, future: 1, too-old: 1)" in r.stdout
    assert pending_rows(sb) == []
    assert len(glob.glob(os.path.join(sb.scrobbles, "unsubmittable", "*.log"))) == 1


def test_collect_reads_rotated_logs(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 x.mp3")
    sb.add_track("A/B/02 y.mp3")
    t = wall_now() - 7200
    sb.play_log(["%d:200000:200000:/Music/A/B/01 x.mp3" % t], name="playback_0001.log")
    sb.play_log(["%d:200000:200000:/Music/A/B/02 y.mp3" % (t + 600)])
    r = collect(sb)
    assert r.returncode == 0, r.stdout
    assert [x[2] for x in pending_rows(sb)] == ["x", "y"]
    assert not os.path.exists(os.path.join(sb.ipod, ".rockbox", "playback_0001.log"))


LEGACY_HEADER = "#AUDIOSCROBBLER/1.1\n#TZ/UNKNOWN\n#CLIENT/Rockbox ipodmini2g $Revision$\n"


def test_legacy_scrobbler_log_import_and_dedupe(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 x.mp3")
    end = wall_now() - 3600
    sb.play_log(["%d:200000:200000:/Music/A/B/01 x.mp3" % end])
    with open(os.path.join(sb.ipod, ".scrobbler.log"), "w") as fh:
        fh.write(LEGACY_HEADER)
        fh.write("A\tB\tx\t1\t200\tL\t%d\t\n" % end)             # same play as playback.log
        fh.write("Other\tAlbum\tNew One\t2\t180\tL\t%d\t\n" % (end - 1000))
        fh.write("Other\tAlbum\tSkipped\t3\t180\tS\t%d\t\n" % (end - 500))
    r = collect(sb)
    assert "legacy /.scrobbler.log: 1 new, 1 duplicates of playback.log" in r.stdout
    rows = pending_rows(sb)
    assert sorted(x[2] for x in rows) == ["New One", "x"]
    new_one = [x for x in rows if x[2] == "New One"][0]
    assert int(new_one[6]) == T.local_to_utc(end - 1000 - 180, LONDON)
    assert not os.path.exists(os.path.join(sb.ipod, ".scrobbler.log"))
    # processing the same legacy log again adds nothing
    with open(os.path.join(sb.ipod, ".scrobbler.log"), "w") as fh:
        fh.write(LEGACY_HEADER + "Other\tAlbum\tNew One\t2\t180\tL\t%d\t\n" % (end - 1000))
    collect(sb)
    assert len(pending_rows(sb)) == 2


def test_legacy_log_with_utc_header_is_not_converted(sb, tmp_path):
    p = tmp_path / "s.log"
    p.write_text("#AUDIOSCROBBLER/1.1\n#TZ/UTC\nA\tB\tT\t1\t100\tL\t5000\t\n")
    (e,) = T.parse_legacy_scrobbler_log(str(p))
    assert e["utc_header"] is True and e["ts_local_start"] == 4900


# ---------------------------------------------------------------- renamed files

def idx(*rels):
    return {T.nkey(r): r for r in rels}


@pytest.mark.parametrize("logged,library,want", [
    ("KETTAMA/Archangel/01 Archangel.mp3", ["KETTAMA/Archangel/01 KETTAMA - Archangel.mp3",
                                            "KETTAMA/Archangel/02 Other.mp3"], 0),
    ("Bill Withers/Just As I Am/03 Ain_t No Sunshine.mp3",
     ["Bill Withers/Just As I Am/03 Ain't No Sunshine.mp3"], 0),
    # title changed completely: matched by track number + extension
    ("A/B/04 Old Title.mp3", ["A/B/04 New Title.mp3", "A/B/04 New Title.flac", "A/B/05 x.mp3"], 0),
    # two candidates with the same number -> don't guess
    ("A/B/04 Old Title.mp3", ["A/B/04 New.mp3", "A/B/04 Newer.mp3"], None),
    # a different album folder never matches
    ("A/C/01 Archangel.mp3", ["A/B/01 Archangel.mp3"], None),
    ("loose.mp3", ["loose.mp3"], None),
])
def test_find_renamed(logged, library, want):
    got = T.find_renamed(logged, idx(*library), {})
    assert got == (library[want] if want is not None else None)


def test_tags_from_path():
    t = T.tags_from_path("Ben Böhmer/Breathing/1-07 Breathing.mp3")
    assert (t["artist"], t["album"], t["title"], t["track"]) == ("Ben Böhmer", "Breathing", "Breathing", "7")


# ---------------------------------------------------------------- prune

def prune_args(sb, **kw):
    a = dict(library=sb.library, ipod=sb.ipod, dry_run=False, force=False,
             log_dir=os.path.join(sb.home, "Library", "Logs"))
    a.update(kw)
    return argparse.Namespace(**a)


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def test_nkey_matches_nfc_nfd_and_case():
    assert T.nkey(nfd("Ben Böhmer/Breathing/01 Böhmer.mp3")) == T.nkey(nfc("BEN BÖHMER/breathing/01 böhmer.MP3"))
    assert T.nkey("A\\B") == T.nkey("a/b")


def test_prune_keeps_accent_and_case_variants(sb):
    sb.make_ipod()
    for rel in ["Ben Böhmer/Breathing/01 Breathing.mp3", "Björk/Post/01 Army of Me.mp3", "Go/x/01 a.mp3"]:
        touch(os.path.join(sb.library, nfd(rel)))
    music = os.path.join(sb.ipod, "Music")
    keep = [nfc("Ben Böhmer/Breathing/01 Breathing.mp3"), nfc("BJÖRK/Post/01 army of me.mp3"), "Go/x/01 a.mp3"]
    for rel in keep:
        touch(os.path.join(music, rel))
    touch(os.path.join(music, "Gone/Album/01 old.mp3"))
    touch(os.path.join(music, "Gone/Album/._01 old.mp3"))
    touch(os.path.join(music, "Go/x/.hidden"))
    assert T.cmd_prune(prune_args(sb)) == 0
    left = sorted(os.path.relpath(os.path.join(d, f), music) for d, _, fs in os.walk(music) for f in fs)
    assert left == sorted(keep + ["Go/x/.hidden"])
    assert not os.path.exists(os.path.join(music, "Gone"))
    listing = open(os.path.join(sb.home, "Library", "Logs", "ipod-sync-prune.txt")).read()
    assert listing == "Gone/Album/01 old.mp3\n"


def test_prune_dry_run_deletes_nothing(sb):
    sb.make_ipod()
    touch(os.path.join(sb.library, "A/B/01 a.mp3"))
    for rel in ["A/B/01 a.mp3", "A/B/02 b.mp3"]:
        touch(os.path.join(sb.ipod, "Music", rel))
    assert T.cmd_prune(prune_args(sb, dry_run=True)) == 0
    assert os.path.exists(os.path.join(sb.ipod, "Music", "A/B/02 b.mp3"))


def test_prune_refuses_to_delete_more_than_half(sb):
    sb.make_ipod()
    touch(os.path.join(sb.library, "A/B/01 a.mp3"))
    for i in range(4):
        touch(os.path.join(sb.ipod, "Music", "A/B/0%d x.mp3" % i))
    assert T.cmd_prune(prune_args(sb)) == 1
    assert len(os.listdir(os.path.join(sb.ipod, "Music", "A", "B"))) == 4
    assert T.cmd_prune(prune_args(sb, force=True)) == 0
    assert os.listdir(os.path.join(sb.ipod, "Music")) == []


@pytest.mark.parametrize("library", ["empty", "missing", "only-dotfiles"])
def test_prune_refuses_when_library_empty_or_missing(sb, library):
    sb.make_ipod()
    lib = os.path.join(sb.root, "External", "Music Library")
    if library == "empty":
        os.makedirs(lib)
    elif library == "only-dotfiles":
        touch(os.path.join(lib, ".DS_Store"))
    touch(os.path.join(sb.ipod, "Music", "A/B/01 a.mp3"))
    assert T.cmd_prune(prune_args(sb, library=lib, force=True)) == 1
    assert os.path.exists(os.path.join(sb.ipod, "Music", "A/B/01 a.mp3"))


# ---------------------------------------------------------------- rsync output

def test_parse_rsync_changes(tmp_path):
    p = tmp_path / "changes.txt"
    p.write_text("\n".join([
        "cd+++++++++ Ben B\\#303\\#266hmer/",
        ">f+++++++++ Ben B\\#303\\#266hmer/Breathing/01 Breathing.mp3",
        ">f+++++++++ Björk/Post/01 Army of Me.mp3",
        ">f..t...... A/B/02 x.flac",
        ">f+++++++++ A/B/cover.jpg",
        ".d..t...... A/",
    ]) + "\n")
    new, updated = T.parse_rsync_changes(str(p))
    assert new == ["Ben Böhmer/Breathing/01 Breathing.mp3", "Björk/Post/01 Army of Me.mp3"]
    assert updated == ["A/B/02 x.flac"]
    assert T.parse_rsync_changes(str(tmp_path / "missing.txt")) == ([], [])


# ---------------------------------------------------------------- playlists

def push(sb, *extra):
    return T.cmd_playlists_push(argparse.Namespace(
        playlists=sb.playlists, ipod=sb.ipod, library=sb.library, dry_run="--dry-run" in extra))


def test_playlists_push_uses_exact_ipod_spelling(sb):
    sb.make_ipod()
    on_ipod = [nfc("Ben Böhmer/Breathing/01 Breathing.mp3"), "kettama/Archangel/01 Archangel.mp3"]
    for rel in on_ipod:
        touch(os.path.join(sb.ipod, "Music", rel))
    with open(os.path.join(sb.playlists, "Mix 🎧.m3u8"), "w", encoding="utf-8") as fh:
        fh.write("﻿#EXTM3U\n# a comment\n")
        fh.write(os.path.join(sb.library, nfd("Ben Böhmer/Breathing/01 Breathing.mp3")) + "\n")  # absolute
        fh.write("/Music/KETTAMA/Archangel/01 Archangel.mp3\n")                                  # Rockbox style
        fh.write("Not/On/iPod.mp3\n")
    assert push(sb) == 0
    out = open(os.path.join(sb.ipod, "Playlists", "Mix 🎧.m3u8"), encoding="utf-8").read()
    assert out == "/Music/%s\n/Music/%s\n" % tuple(on_ipod)


def test_playlists_push_leaves_on_device_playlists_and_removes_deleted_ones(sb):
    sb.make_ipod()
    touch(os.path.join(sb.ipod, "Music", "A/B/01 a.mp3"))
    pl = os.path.join(sb.ipod, "Playlists")
    touch(os.path.join(pl, "Made on iPod.m3u8"), b"/Music/A/B/01 a.mp3\n")
    for name in ("Keep", "Drop"):
        with open(os.path.join(sb.playlists, name + ".m3u8"), "w") as fh:
            fh.write("A/B/01 a.mp3\n")
    push(sb)
    assert sorted(os.listdir(pl)) == [".mac-playlists.txt", "Drop.m3u8", "Keep.m3u8", "Made on iPod.m3u8"]
    os.remove(os.path.join(sb.playlists, "Drop.m3u8"))
    push(sb, "--dry-run")
    assert os.path.exists(os.path.join(pl, "Drop.m3u8"))
    push(sb)
    assert sorted(os.listdir(pl)) == [".mac-playlists.txt", "Keep.m3u8", "Made on iPod.m3u8"]
    assert open(os.path.join(pl, "Made on iPod.m3u8")).read() == "/Music/A/B/01 a.mp3\n"


def test_playlists_push_refuses_when_ipod_music_invisible(sb):
    sb.make_ipod()
    with open(os.path.join(sb.playlists, "Keep.m3u8"), "w") as fh:
        fh.write("A/B/01 a.mp3\n")
    assert push(sb) == 1
    assert not os.path.exists(os.path.join(sb.ipod, "Playlists"))


# ---------------------------------------------------------------- summary

def summary(sb, run, *extra, changes=""):
    runf = os.path.join(sb.root, "run.json")
    with open(runf, "w") as fh:
        json.dump(run, fh)
    files = os.path.join(sb.root, "changes.txt")
    with open(files, "w") as fh:
        fh.write(changes)
    r = sb.tools("summary", "--files-list", files, "--report-dir", sb.report_dir,
                 "--started", str(int(time.time())), *extra, env={"IPOD_SYNC_RUN": runf})
    assert r.returncode == 0, r.stdout
    status, title, detail, report, url = r.stdout.splitlines()[-5:]
    assert url.startswith("file:///")
    return status, title, detail, open(report, encoding="utf-8").read()


@pytest.mark.parametrize("extra,run,want", [
    (["--ejected"], {}, ("ok", "iPod synced - safe to unplug", "Already up to date · no new plays")),
    (["--ejected"], {"upload": {"uploaded": 1}, "prune": {"count": 3}},
     ("ok", "iPod synced - safe to unplug", "+2 songs · −3 songs · 1 play scrobbled")),
    (["--dry-run"], {"scrobbles": {"ready": 4}},
     ("dry", "iPod dry run finished", "would add 2 songs · 4 plays to scrobble")),
    (["--problems", "Disk full\nSecond thing"], {},
     ("error", "iPod sync problem - eject in Finder before unplugging", "Disk full  (+1 more)")),
    (["--ejected"], {"upload": {"failed": True, "message": "offline"}},
     ("warn", "iPod synced with warnings - safe to unplug",
      "Last.fm upload failed (offline) - plays kept, will retry next sync")),
])
def test_summary_notification_text(sb, extra, run, want):
    changes = ""
    if want[2].startswith(("+2", "would add 2")):
        changes = ">f+++++++++ A/B/01 a.mp3\n>f+++++++++ A/B/02 b.mp3\n"
    got = summary(sb, run, *extra, changes=changes)[:3]
    assert got == want


def test_summary_html_is_escaped(sb):
    run = {"scrobbles": {"ready": 1, "plays": [
        {"artist": "Simon & Garfunkel", "title": 'The "Boxer" <live>', "album": "Ain't 🎸", "when": "now"}]},
        "playlists": {"lists": [{"name": "<b>bold</b>", "tracks": 1, "missing": 0}]}}
    _, _, _, html = summary(sb, run, "--ejected", "--problems", "<script>alert(1)</script>",
                            "--lastfm-user", 'x"onmouseover="y')
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Simon &amp; Garfunkel" in html and "The &quot;Boxer&quot; &lt;live&gt;" in html
    assert "Ain&#x27;t 🎸" in html and "&lt;b&gt;bold&lt;/b&gt;" in html
    assert 'x"onmouseover' not in html


def test_summary_history_keeps_last_20_in_report(sb):
    for _ in range(25):
        summary(sb, {}, "--ejected")
    _, _, _, html = summary(sb, {}, "--ejected")
    assert html.count("<td>sync</td>") == 20
