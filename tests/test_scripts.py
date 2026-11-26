"""Basic checks of the shell scripts themselves."""
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["bin/ipod-sync.sh", "install.sh", "uninstall.sh"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_parses_with_macos_bash_3_2(script):
    r = subprocess.run(["/bin/bash", "-n", os.path.join(REPO, script)], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed (brew install shellcheck)")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", *[os.path.join(REPO, s) for s in SCRIPTS]], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    assert r.returncode == 0, r.stdout
