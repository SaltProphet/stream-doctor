# Stream Doctor release plan

## Verified on the Shadow PC

- The redesigned Dashboard and floating monitor displayed live OBS metrics during a password-protected Chaturbate broadcast.
- Exported incident 47 recorded 8.155 seconds of black OBS video, three consecutive black samples, and recovery while the output was active.
- The operator confirmed OBS and Chaturbate continued streaming after Stream Doctor monitoring stopped.

## Next release candidate

1. Add Clear incident history to Incidents. Stop monitoring before clearing so the current monitoring session can finish writing its events. Confirm deletion, refresh the timeline, and check that a fresh export contains only subsequent records.
2. Build the Windows installer and portable executable from the existing draft PR head. This build includes the telemetry evidence panel, measured-upload idle-only bitrate correction, rollback, and next-broadcast comparison. Run the regression suite and silent install, launch, and uninstall checks.
3. Verify Preflight on the Shadow PC with OBS idle. Record Internet upload, source, and timestamp. If the integrated test fails, enter a recent external test result and record that source.
4. With a disposable Simple Output OBS profile, compare configured bitrate and the recommendation. If a change is recommended, apply it while OBS is idle, check the OBS read-back, roll it back, and confirm the original value. The hosted Windows runner has verified the controlled 6,000 to 3,000 to 6,000 kbps cycle under a 4 Mbps cap; this does not establish behavior on the operator's real ingest path.
5. During a restricted test broadcast, observe the floating monitor during a controlled blackout and confirm that its alert clears after recovery. Export the new incident and check that older records are absent after a reset.
6. Before wider distribution, check Windows keyboard navigation, tray recovery and display scaling at 100%, 125%, 150%, and 200%. Record unsupported optimizer operations and unavailable measurements in the release notes.

The hosted disposable RTMP test has yet to activate its local output. The Shadow PC evidence establishes live monitoring and recovery. The live overlay alert and measured optimizer workflow have their own acceptance checks above.

## Phased diagnosis and recovery roadmap

| Phase | Operator workflow | Acceptance evidence |
| --- | --- | --- |
| 1. Live recovery | Choose a recovery scene in Diagnostics. Preview it, switch OBS program output, and verify the resulting local video. | A known black scene switches to a non-black scene while the broadcast stays active. The incident report records the action and verification. Black targets and changed program scenes are refused. |
| 2. Diagnose the failing stage | Compare OBS reconnects and output congestion with interval rendering and encoding counters, program video, and capture-source evidence. | Controlled source, upload, encoding, and rendering failures each produce a distinct diagnosis or an explicit unknown result. |
| 3. Correct the cause | Offer a tested bitrate change with rollback for constrained upload. For capture faults, verify a known-good fallback or a supported source recovery action. Surface the available correction next to its evidence. | Reproduce the original symptom, apply one correction, verify that the relevant metric recovers, and check the broadcast afterward. |
| 4. Optional automatic recovery | Enable a user-selected fallback for sustained local video loss. Add a cooldown, prevent repeated switching, and retain manual control. | Repeated faults, an unavailable fallback, intentional black scenes, and operator scene changes are all exercised on disposable OBS before use on a live broadcast. |

The Phase 1 scene action can restore visible program output while a source issue is investigated. The live connection path and any downstream playback failure require their own evidence. Optimize and verify OBS settings against a disposable profile before using them on a working broadcast.
