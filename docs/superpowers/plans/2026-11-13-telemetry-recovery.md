# Stream Doctor Telemetry Recovery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task-by-task.

**Goal:** Diagnose a known upload bottleneck from telemetry collected directly during use, apply a measured correction with explicit confirmation, and prove whether the symptom improved in a disposable OBS session.

**Architecture:** Extend the existing OBS WebSocket monitor and disposable integration harness. Use OBS output-byte deltas, dropped-frame counters, reconnection state, and local network measurements as the evidence record; make correction application transactional and compare pre/post windows rather than accepting a successful settings write as proof of improvement.

**Tech Stack:** Python 3.11+, asyncio, OBS WebSocket 5.x, PySide6, pytest, local disposable OBS/RTMP endpoint.

**Spec:** User request in the conversation: Stream Doctor diagnoses from telemetry it collects during use; reproduce a known upload bottleneck, capture telemetry directly, apply a correction, and measure whether it helped.

## Global Constraints

- Use disposable OBS profiles and a local test endpoint only.
- Never start a customer broadcast or use customer credentials.
- Do not declare improvement from settings read-back alone; require a post-correction measurement window.
- Preserve existing backup and rollback behavior for OBS writes.
- Keep stream keys, passwords, and raw screenshot bytes out of persisted reports.

## Review Focus

- No stream active: diagnosis must remain unknown and must not recommend a bitrate correction.
- Low bitrate caused by network backpressure: distinguish it from encoder/render lag.
- A correction that reads back successfully but does not improve telemetry: report unchanged, not success.
- Reconnection during a measurement window: invalidate or segment the comparison window.
- Repeated runs and cleanup: disposable OBS, local endpoint, sockets, and temp files must be released.

---

### Task 1: Define telemetry diagnosis and comparison result

**Files:**
- Modify: `stream_doctor/monitoring.py`
- Modify: `stream_doctor/measurements.py`
- Test: `tests/test_monitoring.py`
- Test: `tests/test_measurements.py`

**Interfaces:**
- Produces: a diagnosis that can identify upload/network pressure only when active-stream evidence supports it.
- Produces: a comparison result with pre/post sample counts, bitrate, dropped-frame rate, reconnection count, and `improved | unchanged | inconclusive` status.

- [ ] **Step 1: Write failing tests**

Cover a sustained active stream with dropped frames and no encoder/render lag, a healthy stream, an inactive stream, and a post-correction window containing a reconnect.

- [ ] **Step 2: Implement evidence-based classification**

Use deltas/rates from collected samples; retain the existing conservative unknown outcome when the evidence cannot isolate cause.

- [ ] **Step 3: Implement pre/post comparison**

Compare equivalent windows and include explicit reasons when the result is inconclusive.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_monitoring.py tests/test_measurements.py -q`

### Task 2: Add a controlled upload-bottleneck harness

**Files:**
- Modify: `tools/run_obs_integration.py`
- Test: `tests/test_session.py`
- Modify: `docs/local-integration.md`

- [ ] **Step 1: Write the harness contract test**

Assert that a throttled local RTMP sink is disposable, produces a bounded pre-correction and post-correction window, and returns telemetry plus cleanup status without exposing credentials.

- [ ] **Step 2: Add explicit test controls**

Add named options for bottleneck rate, observation duration, correction bitrate, and `--keep`; retain safe defaults that do not start an active output unless requested.

- [ ] **Step 3: Implement bounded backpressure**

Make the local sink read at a caller-selected bounded rate and never contact an external streaming service. Keep all generated artifacts under the approved temp root.

- [ ] **Step 4: Capture direct OBS telemetry**

Collect repeated `GetStreamStatus`, `GetStats`, output-byte deltas, and relevant settings before and after the correction. Persist only sanitized measurements.

- [ ] **Step 5: Apply, verify, measure, and roll back**

Apply the supported bitrate correction through the existing transactional helper, observe the post window, compute the comparison result, then restore the original setting in `finally`.

- [ ] **Step 6: Run the disposable integration test**

Run the harness against the installed OBS executable and require a machine-readable PASS/FAIL report with cleanup evidence.

### Task 3: Expose the evidence-first workflow in the app

**Files:**
- Modify: `stream_doctor/app.py`
- Modify: `app_redesign.py` only if it remains the active UI entry point
- Test: `tests/test_ui_integration.py`

- [ ] **Step 1: Write UI tests**

Assert the UI shows the measured cause, supporting telemetry, proposed correction, explicit confirmation state, and pre/post result; assert no upload-log field is required.

- [ ] **Step 2: Implement the workflow**

Wire the existing monitor records and optimizer into a user-confirmed correction flow. Disable correction when evidence is insufficient or the stream is inactive.

- [ ] **Step 3: Add bounded measurement status**

Show collecting, applying, measuring, improved/unchanged/inconclusive, and rollback states without implying that settings read-back proves viewer-visible recovery.

- [ ] **Step 4: Run UI and regression tests**

Run: `python -m pytest tests/test_ui_integration.py tests/test_monitoring.py tests/test_measurements.py -q`

### Task 4: Document and verify the experiment

**Files:**
- Modify: `docs/local-integration.md`
- Create: `docs/research/stream-doctor-upload-bottleneck-2026-11-13.md`

- [ ] **Step 1: Document the exact disposable setup**

Record OBS version, harness parameters, local sink behavior, correction, observation windows, and cleanup.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest`

- [ ] **Step 3: Write bounded conclusions**

Report what OBS telemetry established, what improved or failed to improve, and what remains unknown because a local sink cannot prove delivery to a real platform.
