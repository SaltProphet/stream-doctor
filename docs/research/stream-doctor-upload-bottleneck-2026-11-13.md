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

The harness also had a direct-execution import defect: invoking
`python tools/run_obs_integration.py` selected an installed stale
`stream_doctor` package. The worktree now prepends its repository root before
imports, covered by `tests/test_tools_import.py`.

## Next required baseline step

Do not add a bandwidth cap yet. First make the disposable profile expose a real
stream output, then require continuous receiver media plus increasing OBS output
bytes before measuring any bottleneck or applying a correction.
