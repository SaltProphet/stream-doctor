# NOALBS Recovery Trace

## Source identity

- Repository: `NOALBS/nginx-obs-automatic-low-bitrate-switching`
- Commit: `895a9d554a30b2794f81abd654c882f4a0fa5482`
- Primary source: `src/components/ObsSwitcher.js`
- Configuration source: `config.json`
- Evidence type: public source inspection; no NOALBS runtime was executed.

## Observed control loop

`onAuth()` enables the OBS heartbeat, loads scenes, and schedules
`switchSceneIfNecessary()` every `config.obs.requestMs` milliseconds. The pinned
configuration sets that interval to 2000 ms, `lowBitrateTrigger` to 1000, and
`highRttTrigger` to 2500 ms.

`getBitrate()` polls the configured server-side statistics endpoint. NGINX and
Node-Media-Server provide bitrate; Nimble provides bitrate plus SRT RTT and
connection state; SRT-Live-Server provides bitrate plus RTT. A failed request or
missing stream sets bitrate to `null`.

## Transition table

| Evidence | Guard | Scene action | Persistence shown in source |
|---|---|---|---|
| Bitrate is `null` | Current scene is switchable and is not offline | Switch to `offlineScene` | None; one poll is sufficient |
| Bitrate is `0` | Current scene is switchable and differs from `previousScene` | Switch to `previousScene`; emit live transition | None |
| Bitrate is nonzero and `<= lowBitrateTrigger` | Current scene is switchable and differs from `lowBitrateScene` | Switch to low-bitrate scene; set `previousScene` to low-bitrate scene | None |
| Nimble/SLS RTT `>= highRttTrigger` and bitrate above low threshold | Current scene is switchable and differs from low-bitrate scene | Switch to low-bitrate scene; update `previousScene` | None |
| Nimble/SLS RTT below threshold and bitrate above low threshold | Current scene is switchable and differs from normal scene | Switch to normal scene; update `previousScene` | None |
| Other server and bitrate above low threshold | Current scene is switchable and differs from normal scene | Switch to normal scene; update `previousScene` | None |
| OBS stream starts | OBS event | Set `obsStreaming = true` | Event-driven |
| OBS stream stops | OBS event | Set `obsStreaming = false`, clear bitrate, switch to offline if scene is switchable | Event-driven |
| OBS connection closes | WebSocket event | Clear interval and reconnect after five seconds | Fixed reconnect delay |

## Scene gate and transferability

`canSwitch()` permits changes only when the current scene is the configured low,
normal, or offline scene. Manual scenes outside that set are not changed. The
optional `onlySwitchWhenStreaming` guard defaults to true, so the polling loop
does nothing until OBS reports streaming.

The trigger evidence is primarily server-side incoming-feed telemetry. OBS is
used for connection state, heartbeat, stream lifecycle, and scene control; the
bitrate and RTT decisions are not derived from OBS output counters. Therefore
the state machine is useful as a recovery-policy reference, but it does not
establish how to diagnose or repair an outgoing OBS upload bottleneck.

## Important limitations

- The source shows no consecutive-sample requirement, debounce, hysteresis, or
  recovery dwell time.
- A single transient poll can cause a scene transition.
- For Nimble/SLS low-bitrate handling, the source contains the condition
  `(rtt < highRttTrigger || rtt >= highRttTrigger)`, which is tautological; the
  low-bitrate path therefore does not actually depend on RTT.
- Runtime timing, API response failures, and repeated-drop behavior remain
  unobserved until a disposable server/OBS setup is run.
