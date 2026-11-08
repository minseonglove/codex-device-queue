---
name: device-queue-release
description: Use when the user wants to return this Codex session's exclusive mobile-device grant after device-dependent work, or asks to free the device this session holds.
---

# Return this session's device

Read the [shared management contract](../device-queue/references/management.md). Use internal `manage inspect` with the exact startup owner to resolve this session's held grant. Retain the full stable device ID and returned ticket. If several grants match, ask only for target selection; never return all devices implicitly or use a parent's grant from a child.

Before acting, establish from the actual task and tool results that local commands, UI/MCP interactions, debugger connections and device-side tests for this grant have stopped. A process ending or an old timestamp alone is not evidence that external work ended. If facts are missing, explain what is unknown and keep ownership; do not ask for a token or issue manual CLI instructions.

Execute `manage release` for the resolved owner, device and ticket with actual idle evidence under the internal contract. `--confirm-idle` records verified facts, not permission to bypass guards. If registered work or an inherited lock is alive, report the tool's blocker and retain the grant; do not kill another session's work or force recovery. A queued request is not a grant to release.

Report the returned released/closed state and full device ID in the user's language. Say the next waiter can acquire a grant, not that it already did unless verified by readback. Never claim a test passed merely because the device was returned.
