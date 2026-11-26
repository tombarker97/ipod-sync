"""Mistakes people make in ~/.config/ipod-sync/config."""
import os
import subprocess

import pytest

from conftest import have_rsync3, tree_hash
from test_tools_unit import wall_now

pytestmark = pytest.mark.skipif(not have_rsync3(), reason="needs Homebrew rsync 3")


def ready(sb):
    sb.make_ipod()
    sb.add_track("A/B/01 a.mp3")
    sb.play_log(["%d:200000:200000:/Music/A/B/01 a.mp3" % (wall_now() - 3600)])


def mac_zone():
    return os.readlink("/etc/localtime").split("zoneinfo/")[-1]


@pytest.mark.parametrize("value", ["Yes", "YES", "true", "1", "y", "on", "maybe"])
def test_dry_run_spellings_never_mean_a_real_run(sb, value):
    ready(sb)
    sb.config(DRY_RUN=value)
    before = tree_hash(sb.ipod)
    r = sb.run()                       # automatic run: obeys the setting
    assert tree_hash(sb.ipod) == before, sb.logtext()
    assert sb.accepted() == []
    if value == "maybe":
        assert "DRY_RUN" in sb.logtext() and "yes or no" in sb.logtext()
    assert r.returncode == 0


@pytest.mark.parametrize("value", ["No", "false", "0", "off"])
def test_no_spellings(sb, value):
    ready(sb)
    sb.config(DO_FILES=value)
    sb.run("--real")
    assert not os.path.exists(os.path.join(sb.ipod, "Music", "A"))


def test_src_without_trailing_slash(sb):
    ready(sb)
    sb.config(SRC=sb.library)          # no trailing /
    r = sb.run("--real")
    assert os.path.exists(os.path.join(sb.ipod, "Music", "A", "B", "01 a.mp3")), r.stdout
    assert not os.path.exists(os.path.join(sb.ipod, "Music", "Music Library"))


@pytest.mark.parametrize("value", ["/", "$HOME", "$HOME/"])
def test_src_whole_disk_or_home_is_refused(sb, value):
    ready(sb)
    sb.raw_config(open(sb.conf).read() + 'SRC="%s"\n' % value)
    before = tree_hash(sb.ipod)
    r = sb.run("--real")
    assert r.returncode == 1
    assert tree_hash(sb.ipod) == before
    assert "SRC" in r.stdout


def test_misspelt_time_zone_falls_back_to_the_macs(sb):
    ready(sb)
    sb.config(IPOD_TZ="Europe/Londn")
    r = sb.run("--real")
    assert "IPOD_TZ" in r.stdout and "Europe/Londn" in r.stdout
    assert "iPod clock (%s)" % mac_zone() in r.stdout
    assert len(sb.accepted()) == 1


def test_empty_time_zone_uses_the_macs_not_utc(sb):
    ready(sb)
    sb.config(IPOD_TZ="")
    r = sb.run("--real")
    assert "iPod clock (%s)" % mac_zone() in r.stdout, r.stdout


def test_missing_time_zone_uses_the_macs(sb):
    ready(sb)
    sb.raw_config("".join(line for line in open(sb.conf) if not line.startswith("IPOD_TZ")))
    r = sb.run("--real")
    assert "iPod clock (%s)" % mac_zone() in r.stdout, r.stdout


@pytest.mark.parametrize("bad,why", [
    ('SRC="/Users/me/Music Library/\n', "unexpected EOF"),            # missing closing quote
    ('SRC="$MUSIC/Library/"\n', "MUSIC: unbound variable"),             # undefined variable
])
def test_broken_settings_file_stops_the_sync(sb, bad, why):
    ready(sb)
    sb.raw_config(open(sb.conf).read() + bad + 'DRY_RUN="yes"\n')
    before = tree_hash(sb.ipod)
    r = sb.run("--real")
    assert r.returncode == 1
    assert tree_hash(sb.ipod) == before
    assert why in r.stdout
    title, msg = sb.notifications()[-1]
    assert "settings" in title.lower()


def test_unknown_setting_name_is_warned_about(sb):
    ready(sb)
    sb.raw_config(open(sb.conf).read() + 'DO_PLAYLIST="no"\n')
    r = sb.run("--real")
    assert "DO_PLAYLIST" in r.stdout and "typo" in r.stdout
    html = open(os.path.join(sb.report_dir, "Last sync.html"), encoding="utf-8").read()
    assert "DO_PLAYLIST" in html


def test_bad_ipod_name(sb):
    ready(sb)
    sb.raw_config(open(sb.conf).read() + 'IPOD_NAME="../TESTPOD"\n')
    r = sb.run("--real")
    assert r.returncode == 1 and "IPOD_NAME" in r.stdout


def test_installer_config_parses_under_set_u():
    """The settings file install.sh writes must be valid on its own."""
    text = open(os.path.join(os.path.dirname(__file__), "..", "install.sh")).read()
    body = text.split('cat > "$CONF" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    body = body.replace("$C_NAME", "IPOD").replace("$C_SRC", "/x/").replace("$C_PL", "/x") \
        .replace("$C_TZ", "UTC").replace("$C_LFM", "").replace("\\$", "$")
    r = subprocess.run(["/bin/bash", "-u", "-c", body], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env={"HOME": "/tmp"})
    assert r.returncode == 0, r.stdout
