"""Basic checks of the shell scripts themselves."""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = ["bin/ipod-sync.sh", "install.sh", "uninstall.sh"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_parses_with_macos_bash_3_2(script):
    r = subprocess.run(["/bin/bash", "-n", os.path.join(REPO, script)], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    assert r.returncode == 0, r.stdout
