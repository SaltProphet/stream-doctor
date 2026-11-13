from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.run_obs_integration import ensure_first_run_enabled


def test_obs_integration_script_uses_checkout_package_when_run_directly():
    result = subprocess.run(
        [sys.executable, "tools/run_obs_integration.py", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr
    assert "--active-output" in result.stdout


def test_first_run_flag_preserves_other_user_settings(tmp_path: Path):
    user_ini = tmp_path / "user.ini"
    user_ini.write_text("[General]\nTheme=Dark\n[Other]\nValue=kept\n", encoding="utf-8")

    ensure_first_run_enabled(user_ini)

    assert user_ini.read_text(encoding="utf-8") == (
        "[General]\nTheme=Dark\nFirstRun=true\n[Other]\nValue=kept\n"
    )
