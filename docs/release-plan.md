# Stream Doctor release plan

## Verified on the Shadow PC

- The redesigned Dashboard and floating monitor displayed live OBS metrics during a password-protected Chaturbate broadcast.
- Exported incident 47 recorded 8.155 seconds of black OBS video, three consecutive black samples, and recovery while the output was active.
- The operator confirmed OBS and Chaturbate continued streaming after Stream Doctor monitoring stopped.

## Next release candidate

1. Add Clear incident history to Incidents. Stop monitoring before clearing so the current monitoring session can finish writing its events. Confirm deletion, refresh the timeline, and check that a fresh export contains only subsequent records.
2. Build the Windows installer and portable executable. Run the regression suite and silent install, launch, and uninstall checks. Keep the release candidate on the existing draft PR.
3. Verify Preflight on the Shadow PC with OBS idle. Record Internet upload, source, and timestamp. If the integrated test fails, enter a recent external test result and record that source.
4. With a disposable Simple Output OBS profile, compare configured bitrate and the recommendation. If a change is recommended, apply it, check the OBS read-back, roll it back, and confirm the original value. The hosted Windows runner has already verified a controlled 6,000 to 1,234 to 6,000 kbps cycle.
5. During a restricted test broadcast, observe the floating monitor during a controlled blackout and confirm that its alert clears after recovery. Export the new incident and check that older records are absent after a reset.
6. Before wider distribution, check Windows keyboard navigation, tray recovery and display scaling at 100%, 125%, 150%, and 200%. Record unsupported optimizer operations and unavailable measurements in the release notes.

The hosted disposable RTMP test has yet to activate its local output. The Shadow PC evidence establishes live monitoring and recovery. The live overlay alert and measured optimizer workflow have their own acceptance checks above.
