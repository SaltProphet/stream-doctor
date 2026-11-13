# Competitor Recovery Research Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task-by-task.

**Goal:** Trace NOALBS's recovery state machine from public source and benchmark StreamRx's diagnosis workflow using a disposable, locally generated OBS failure log.

**Architecture:** Keep competitor research read-only and separate from product code. Record source commit, observed thresholds, state transitions, evidence, and unknowns in a dated research report; use StreamRx only with a synthetic log that contains no customer data or credentials.

**Tech Stack:** GitHub public source, Python-generated OBS log fixture, agent-browser, Markdown research report.

**Spec:** User request in the conversation: NOALBS public-source trace; StreamRx benchmark with a generated test log; no customer logs.

## Global Constraints

- Do not upload customer logs, stream keys, passwords, or other real credentials.
- Do not infer runtime behavior from source alone; label source-derived behavior separately from observed behavior.
- Pin NOALBS findings to the fetched public commit `895a9d554a30b2794f81abd654c882f4a0fa5482`.
- Do not modify NOALBS or StreamRx; do not bypass authentication or rate limits.

## Review Focus

- Zero bitrate versus low-but-nonzero bitrate: test both because NOALBS treats them as different transitions.
- Recovery hysteresis: test repeated low/normal transitions and record whether there is any persistence or debounce.
- Non-streaming and manual scenes: test that switching is gated by OBS streaming state and allowed scenes.
- High RTT with healthy bitrate: test the separate RTT trigger path.
- StreamRx data handling: verify the synthetic upload contains no secret and record only the report output, not hidden implementation assumptions.

---

### Task 1: Pin and trace NOALBS source

**Files:**
- Create: `docs/research/noalbs-recovery-trace-2026-11-13.md`
- Source evidence: public `src/components/ObsSwitcher.js`, `src/app.js`, `config.json` at the pinned commit

**Interfaces:**
- Produces: a state table for offline, zero bitrate, low bitrate, normal bitrate, high RTT, reconnect, and scene-gating behavior.

- [ ] **Step 1: Capture source evidence**

Record the exact commit and relevant line ranges for `switchSceneIfNecessary`, `getBitrate`, `onAuth`, `reconnect`, `streamStarted`, `streamStopped`, and `canSwitch`.

- [ ] **Step 2: Write the transition table**

Include inputs, configured thresholds, current scene requirements, resulting scene, timer cadence, and whether the source shows persistence/debounce.

- [ ] **Step 3: Mark transferability**

For each transition, label whether it depends on server-side incoming-feed stats, OBS telemetry, or both. Explicitly note that NOALBS monitors an incoming feed and is not direct evidence about an outgoing OBS connection.

- [ ] **Step 4: Review the report**

Verify every behavioral claim links to source evidence and every runtime claim is marked unobserved until tested.

### Task 2: Build a disposable StreamRx benchmark fixture

**Files:**
- Create: `tools/generate_synthetic_obs_log.py`
- Test: `tests/test_synthetic_obs_log.py`
- Create: `docs/research/streamrx-benchmark-2026-11-13.md`

**Interfaces:**
- Produces: `generate_log(scenario: str, output: Path) -> Path`, with scenarios `network_drops` and `encoding_overload`.
- The fixture must contain recognizable OBS version, output settings, counters, and failure lines but no stream URL, key, password, username, or local user path.

- [ ] **Step 1: Write fixture tests**

Assert both scenarios contain the expected failure signatures, are valid UTF-8 text, and contain none of the forbidden secret/path patterns.

- [ ] **Step 2: Implement the generator**

Generate deterministic logs from fixed timestamps and sanitized hardware/settings values. Keep the fixture clearly synthetic in its header.

- [ ] **Step 3: Run the fixture tests**

Run: `python -m pytest tests/test_synthetic_obs_log.py -q`

- [ ] **Step 4: Exercise StreamRx only if needed**

Use the public diagnosis workflow with one generated fixture at a time. Record the health score, detected causes, evidence wording, recommended changes, retest affordance, and whether the site accepts synthetic input. Do not save or share a permanent report link unless explicitly requested.

- [ ] **Step 5: Write the benchmark report**

Separate observed report output from product implications. Compare network-drop and encoding-overload classification, evidence quality, recommended correction, and retest semantics.

### Task 3: Consolidate competitor findings

**Files:**
- Create: `docs/research/competitor-recovery-findings-2026-11-13.md`

- [ ] **Step 1: Compare the two evidence models**

Contrast NOALBS's periodic server-stat trigger and scene automation with StreamRx's uploaded-log diagnosis and retest workflow.

- [ ] **Step 2: Extract only transferable product requirements**

Recommend evidence-first diagnosis, explicit correction, bounded recovery criteria, and before/after measurement; reject customer-log upload as a Stream Doctor requirement.

- [ ] **Step 3: Record unresolved questions**

List any behavior requiring a real disposable OBS run rather than source or public web evidence.
