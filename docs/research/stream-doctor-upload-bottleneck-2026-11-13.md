# Stream Doctor Upload-Bottleneck Baseline

## Run identity

- Base: PR #1 head `da09b716ea0d49b320d6b4f3554b2a308aecee02`
- Host: Windows 11, OBS 33.0.1, obs-websocket 5.7.6
- Harness: `tools/run_obs_integration.py`
- Receiver: local FFmpeg RTMP listener on `127.0.0.1:19350`
- Scope: disposable OBS profile only; no external ingest and no customer data

## Baseline result

The read-only/idle harness passes and confirms OBS WebSocket authentication,
scene creation, screenshot analysis, and idle bitrate apply/read-back/rollback.

The active-output baseline is **inconclusive and not ready for bottleneck
testing**. The receiver process remains alive, but OBS reports:

- `outputActive: false`
- `outputBytes: 0`
- `outputTotalFrames: 0`
- `outputSkippedFrames: 0`
- `outputCongestion: 0.0`

`GetOutputList` exposes only `simple_file_output` and `virtualcam_output`; the
file output has `OBS_OUTPUT_SERVICE: false`. No receiver media was observed.

## Root-cause boundary

The failure occurs before network backpressure can be introduced. A slow-reading
receiver cannot explain zero OBS output bytes and zero encoded frames. The
disposable profile is not materializing a streaming output under this OBS
version/configuration, even though `GetStreamServiceSettings` reports the
expected custom RTMP service and `StartStream` returns an empty success response.

The follow-up trace records the same boundary with event and receiver evidence:

- `StartStream` response: `{}`
- `StreamStateChanged` events: `0`
- FFmpeg trace lines: listener startup only; no publisher accept line
- FFmpeg media-progress records: `0`
- OBS outputs before start: `simple_file_output` and `virtualcam_output` only
- `OBS_OUTPUT_SERVICE` on both outputs: `false`
- OBS current encoder setting: unavailable because no service output exists

An independent local probe accepted a generated H.264/AAC RTMP publisher and
reported 40 video frames over four seconds. This isolates the failure to OBS
output initialization, not the receiver or loopback RTMP handshake.

## Auto-Configuration Wizard fix

OBS 33.0.1's fresh portable configuration queued the first-run wizard, which
left outputs disabled while `StartStream` returned successfully. The harness
now preserves the disposable `user.ini` and ensures:

```ini
[General]
FirstRun=true
```

The uncapped baseline then passed from a fixed runtime path:

- Runtime: `%TEMP%\stream-doctor-obs-test\obs-studio`
- OBS state events: `OBS_WEBSOCKET_OUTPUT_STARTING`, `OBS_WEBSOCKET_OUTPUT_STARTED`
- Active output samples: 5
- OBS output bytes: `0 -> 0 -> 0 -> 0 -> 2223`
- FFmpeg media: 467 frames and 28 media-progress records

The harness resets only the disposable configuration/log directory before each
run and keeps the staged executable at the fixed runtime path. WebSocket and
RTMP endpoints remain loopback-bound to `127.0.0.1`.

The harness also had a direct-execution import defect: invoking
`python tools/run_obs_integration.py` selected an installed stale
`stream_doctor` package. The worktree now prepends its repository root before
imports, covered by `tests/test_tools_import.py`.

## Next research step

The publishing baseline is now established, and the controlled cap comparison
below has been completed with rollback verification.

## Controlled cap trials

The trial harness used the same fixed OBS profile, x264 encoder, moving color
source, 15-second window, and localhost RTMP path for all three trials. The
relay cap was 4 Mbps (500,000 bytes/sec), above the corrected stream's expected
3.16 Mbps total (3,000 video + 160 audio) and below the current stream's
approximately 6.3 Mbps total.

| Trial | OBS bitrate | Max congestion delta | New skipped output frames | Reconnect samples | FFmpeg frames | FFmpeg media time |
|---|---:|---:|---:|---:|---:|---:|
| Uncapped current | 6,000 kbps | 0.00 | 0 | 0 | 443 | 14.72 s |
| Capped current | 6,000 kbps | 1.00 | 80 | 0 | 336 | 13.823 s |
| Capped lower | 3,000 kbps | 0.00 | 0 | 0 | 443 | 14.72 s |

The capped-current trial reproduced a measurable delivery failure before the
bitrate change. The lower bitrate was applied only while OBS was idle, read
back as 3,000 kbps, and then restored while idle; final read-back was 6,000
kbps. Delivery improved under the same cap: congestion returned to zero, newly
skipped frames returned to zero, and FFmpeg media progress matched the uncapped
trial. Full sanitized evidence is in
`%TEMP%\stream-doctor-obs-test\trials\trials-report.json`.
