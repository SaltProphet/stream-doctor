# Local OBS and restricted-ingest verification

## Safe boundary

Stream Doctor must not start a stream, change a stream key, or start a paid/private session. The automated workflow is limited to a disposable OBS portable profile and read-only OBS WebSocket requests. A Chaturbate ingest test can only begin after the operator manually configures the dedicated OBS profile and starts the restricted broadcast.

## Dedicated OBS test profile

The harness creates a profile under:

```text
C:\Users\Shadow\AppData\Local\Temp\opencode\stream-doctor-obs-test-*\config\obs-studio
```

It creates `StreamDoctorTestScene` and a local OBS color source. Existing `%APPDATA%\obs-studio` profiles and scene collections are not edited.

Run the disposable read-only integration:

```powershell
python tools\run_obs_integration.py --keep
```

For an optional local-output attempt (never a public broadcast):

```powershell
python tools\run_obs_integration.py --active-output
```

This requires a local RTMP receiver that completes the RTMP handshake. The harness stops and removes its disposable OBS profile afterward unless `--keep` is supplied.

The harness does not broadcast or record. It stages the installed OBS directory into a disposable copy because OBS portable mode requires an isolated executable root. On this workstation the staged OBS 33.0.1 instance exposed port 4456 and passed the authenticated integration workflow.

## Existing OBS read-only inspection

After manually enabling OBS WebSocket for the dedicated test profile, run:

```powershell
python tools\inspect_obs.py --seconds 30 --source StreamDoctorColor
```

The tool reads OBS stats, stream status, output status/list, video settings, and sanitized service metadata. It can request source screenshots for in-memory black/frozen analysis; it does not save or transmit frames.

## Stream Doctor live session workflow

1. Launch `dist\StreamDoctor\StreamDoctor.exe`.
2. Open **Settings** and enter the host, port, and OBS WebSocket password for the exact OBS instance that will broadcast. The endpoint is not assumed to be 4455 or 4456.
3. Use **Test connection** and confirm the displayed OBS and obs-websocket versions.
4. Run **Preflight** checks. Use the bounded Cloudflare upload test only when OBS is not actively streaming.
5. Select **Start monitoring**.
6. Confirm the Dashboard shows stream state, observed bitrate, dropped frames, render lag, current scene measurements, and diagnosis.
7. Start/end any restricted broadcast manually; Stream Doctor does not start or stop it.
8. Use **Incidents → Refresh** and **Export report** after testing.

Connection endpoint and instance label are saved locally; the WebSocket password is deliberately not written to disk. Reports contain measurements and incident metadata only—no credentials, room identifiers, or image data.

## Restricted Chaturbate test procedure

1. Manually select only the dedicated test profile.
2. Configure the restricted/locked Chaturbate destination in OBS yourself; Stream Doctor never reads or changes the stream key.
3. Start the restricted broadcast manually.
4. Run `inspect_obs.py` for the test interval. It records OBS-reported output bytes, output frame totals, skipped frames, congestion, reconnect state, render timing, and timestamps.
5. For controlled tests, manually switch to the black test scene for at least 10 seconds, then restore the normal scene. Stream Doctor requires three consecutive black samples before opening an incident.
6. If testing reconnect, manually interrupt and restore the network or ingest connection; Stream Doctor records only the resulting OBS state and does not initiate recovery.
7. Manually stop the restricted broadcast.
8. Record viewer-confirmed blackout observations separately; OBS telemetry alone cannot prove viewer delivery.

Viewer-confirmed delivery remains separate from OBS verification. A program-scene screenshot proves what OBS rendered locally, not exactly what the platform delivered to viewers.

The current implementation does not yet expose a measured encoder-lag field from OBS WebSocket; it records OBS render timing and output counters and reports encoder lag as unavailable rather than inventing it. Chaturbate ingest bitrate and viewer confirmation require the operator’s manual restricted broadcast.
