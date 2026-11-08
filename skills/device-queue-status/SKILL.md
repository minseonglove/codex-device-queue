---
name: device-queue-status
description: Use when the user asks who holds a mobile device, which device requests are waiting, this session's queue position, or the current shared device-queue state.
---

# Show device queues

Read the [shared management contract](../device-queue/references/management.md). Execute internal `manage inspect` using the actual startup owner; this is a queue query, not a request for a device grant.

Render each returned device ID as a separate group. Show the held grant, recorded purpose, waiting order and whether a request belongs to this exact session. Always include the full stable ID; two devices named Pixel 8 remain two groups. Add a friendly name only if actual side-effect-free discovery maps it to that ID. Without discovery, show IDs only; do not invent names, connection status or idle connected devices.

If no groups are returned, say the queue has no active requests, not that no hardware is connected. A held grant does not prove a test is currently running. A first waiter still needs an explicit grant.

Report the observed state in the user's language. Do not return JSON, Python commands, tokens or cache paths as the user-facing result. Do not register, wait, cancel, release or recover anything merely to show status.
