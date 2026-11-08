---
name: device-queue-cancel
description: Use when the user wants to withdraw or cancel this Codex session's waiting request for a shared mobile device, including when the conversation no longer contains its ticket or token.
---

# Cancel this session's wait

Read the [shared management contract](../device-queue/references/management.md). Execute internal `manage cancel` with the exact startup owner, resolving a device/purpose explicitly named by the user first. The helper finds existing requests and handles tokens internally. Do not ask the user to paste a token or run a command; do not call `request` to retrieve a lost capability.

If the intended ticket is already known, pin it with its device ID and ticket. With one matching waiter, cancel and report the returned cancelled state and full device ID. With multiple candidates, show their full IDs and purposes and ask which one; no mutation occurs before selection. Same-model phones remain distinct. With no request, report that this session has no wait to withdraw.

If the request acquired a grant before cancellation, report that it now holds the device. Do not silently return its grant, interrupt tests or switch to a replacement request. A request belonging to another owner is refused; never retry using that owner's ID. Respect closed-state readback.

Finish with the observed action/result in the user's language, not JSON or internal shell instructions. Only the selected wait may change.
