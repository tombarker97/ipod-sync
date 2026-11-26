"""Notification text with quotes, backslashes, emoji and long names."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from conftest import have_rsync3, write_exe

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")

TRICKY = 'Mus"ic \\ AC\\DC \'n\' 🎧 ' + "x" * 200


def osascript_compiles(args):
    """args as passed to osascript: compile the script part (doesn't show anything)."""
    script = []
    rest = list(args)
    while rest and rest[0] == "-e":
        script += ["-e", rest[1]]
        rest = rest[2:]
    r = subprocess.run(["/usr/bin/osacompile", "-o", "/dev/null", *script], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return r.returncode == 0, r.stdout, rest


def test_osascript_fallback_handles_any_text(sb):
    sb.make_ipod()
    missing = os.path.join(sb.root, TRICKY, "Music Library")
    sb.config(SRC=missing + "/")
    r = sb.run("--real", env={"STUB_TERMINAL_NOTIFIER_EXIT": "1"})
    calls = sb.calls("osascript")
    assert calls, r.stdout
    ok, err, argv = osascript_compiles(calls[-1])
    assert ok, "AppleScript doesn't compile: %s\n%s" % (err, calls[-1])
    assert any(missing in a for a in argv), argv      # the text arrives untouched


def test_terminal_notifier_gets_exact_text(sb):
    sb.make_ipod()
    missing = os.path.join(sb.root, TRICKY, "Music Library")
    sb.config(SRC=missing + "/")
    sb.run("--real")
    assert missing in sb.notifications()[-1][1]


def test_report_link_is_a_proper_file_url(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    rd = os.path.join(sb.home, "Music", "iPod Sync #1 100% ü")
    sb.config(REPORT_DIR=rd)
    sb.run("--real")
    last = sb.calls("terminal-notifier")[-1]
    url = last[last.index("-open") + 1]
    assert url == pathlib.Path(rd, "Last sync.html").as_uri()


def test_summary_crash_still_notifies(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    wrapper = os.path.join(sb.root, "python-wrapper")
    write_exe(wrapper, '#!/bin/bash\n[ "$2" = summary ] && { echo Traceback >&2; exit 1; }\nexec "%s" "$@"\n'
              % sys.executable)
    sb.config(PYTHON=wrapper)
    sb.run("--real")
    title, detail = sb.notifications()[-1]
    assert title.strip() and detail.strip()
    assert "report" in detail.lower() or "log" in detail.lower()


def test_bad_history_line_doesnt_break_the_report(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    os.makedirs(sb.report_dir)
    with open(os.path.join(sb.report_dir, "history.jsonl"), "w") as fh:
        fh.write(json.dumps({"something": "else"}) + "\nnot json\n")
    sb.run("--real")
    assert sb.notifications()[-1][0] == "iPod synced - safe to unplug"
