# Stream Doctor

Local Windows OBS diagnostics and monitoring application. It uses OBS WebSocket 5.x, keeps incident history in local SQLite, and never stores screenshot pixels, stream keys, or WebSocket passwords.

## Development

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest
python -m stream_doctor.app
```

Enable OBS WebSocket in OBS Studio (Tools → WebSocket Server Settings). Enter the actual endpoint in **Settings**; the application does not assume that the active instance uses port 4455. The client implements the OBS WebSocket 5.x hello/identify protocol and reads telemetry, program-scene screenshots, video settings, and safe output settings without retaining service credentials.

## Monitoring and incidents

The live monitor samples the current OBS program scene in memory. Sustained near-black frames are analyzed locally using brightness and black-pixel ratios. Incidents persist their start time, recovery time, duration, scene, telemetry, and recovery evidence in SQLite. Screenshot bytes are never written or uploaded.

The redesigned interface includes Dashboard, Preflight, Diagnostics, Optimizer, Incidents, Settings, and a floating always-on-top Live Monitor. Reports can be exported as sanitized JSON or HTML from **Incidents → Export report**.

## Optimizer and preflight

Preflight reports measured CPU, RAM, GPU discovery, latency, jitter, packet loss, and upload-test status. The optional Internet upload test uses Cloudflare's documented `https://speed.cloudflare.com/__up` endpoint with bounded POST samples; it is blocked while OBS reports an active stream. Advanced users can provide a controlled endpoint instead.

Recommendations are generated only from available measurements. The optimizer does not invent upload capacity or encoder compatibility. Explicit bitrate changes require confirmation, OBS read-back verification, and support rollback; no setting is changed automatically.

## Scope status

Implemented: local SQLite incidents, sustained blackout detection and recovery, sanitized JSON/HTML reports, measured-input recommendations, safe output-setting apply/read-back/rollback primitives, OBS WebSocket monitoring, program-scene screenshots, hardware discovery, bounded network measurements, PySide6 navigation, floating monitoring, and system-tray lifecycle. Complete OBS encoder enumeration, viewer-visible delivery verification, and a genuine active-output endurance harness remain environment-dependent.

## Safety

The optimizer does not silently change OBS. Upload tests are disabled during active streaming. Settings writes require explicit user confirmation, apply only supported recommendations, reread OBS settings, and expose rollback. The application never starts or stops a user broadcast.

## Windows build

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

The package-aware `launcher.py` entry point is used by PyInstaller, and `stream_doctor/theme.qss` is included in the packaged distribution.
