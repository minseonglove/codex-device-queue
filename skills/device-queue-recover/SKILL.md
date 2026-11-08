---
name: device-queue-recover
description: Use when the user explicitly asks to inspect or recover a mobile-device request left by a crashed or abandoned session, or a stuck shared device grant needs verified administrative recovery.
---

# Recover abandoned device work

Read the [shared management contract](../device-queue/references/management.md) and [complete recovery rules](../device-queue/references/rules.md). This is an explicit administrative task, not routine cancellation, a priority shortcut or a timer-based cleanup.

1. Inspect the shared queue using the actual startup owner. Resolve the intended request from the user's device/job description, retaining its full stable ID and actual ticket internally. If several targets match, show IDs and recorded purposes and ask which. Do not ask the user to paste an internal ticket/token or register replacement work.
2. Gather actual evidence that the previous controller, tracked processes, UI/MCP/debugger activity and device-side tests cannot continue. Queue age, silence, disconnection or an idle-looking screen is insufficient. Local process exit alone does not establish external idleness. Do not start new device work using the abandoned grant while investigating.
3. If evidence is incomplete, keep the request unchanged and state the specific missing evidence. Ask for that fact only when it cannot be established from available task/tool information. Do not provide manual Python instructions.
4. Once verified, execute the existing guarded recovery internally for that exact ticket, with the actual evidence as the reason. Another session's abandoned request may be targeted only when the user explicitly requested that identified work/device. Never adopt its owner/token, kill its processes or bypass a busy response.
5. Report the returned recovered/closed state and full device ID. A next waiter may now acquire a grant; it has not automatically been granted access unless readback shows it. Do not claim a device test passed or an inactive chat woke up.
