"""Package-aware Windows application entry point for PyInstaller."""

from stream_doctor.app import main


if __name__ == "__main__":
    raise SystemExit(main())
