def test_packaging_launcher_uses_package_qualified_entrypoint():
    import launcher
    from stream_doctor.app import main

    assert launcher.main is main


def test_windows_build_includes_redesign_stylesheet():
    from pathlib import Path

    build_script = Path("build_windows.ps1").read_text(encoding="utf-8")
    assert "stream_doctor/theme.qss;stream_doctor" in build_script


def test_windows_build_and_installer_use_the_same_pyinstaller_distribution():
    from pathlib import Path

    build_script = Path("build_windows.ps1").read_text(encoding="utf-8")
    installer = Path("installer.iss").read_text(encoding="utf-8")
    assert "dist\\StreamDoctor" in build_script
    assert 'Source: "dist\\StreamDoctor\\*"' in installer
    assert "Inno Setup failed" in build_script
