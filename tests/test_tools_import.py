from __future__ import annotations

import subprocess
import sys


def test_obs_integration_script_uses_checkout_package_when_run_directly():
    result = subprocess.run(
        [sys.executable, "tools/run_obs_integration.py", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr
    assert "--active-output" in result.stdout
