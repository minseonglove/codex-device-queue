---
name: device-queue
description: Use when initializing codex-device-queue with --init, or when a task needs a connected Android or iOS mobile device for installation, execution, testing, debugging, UI or MCP interaction, screenshots, logs or test-data capture.
---

# Use a shared mobile device

The user does not run Python, register requests manually or copy tickets/tokens.

For `device-queue --init` or an explicit request to initialize this plugin, follow the [initialization reference](references/initialization.md) and report its result. This setup-only mode needs no device, owner, ticket or grant. It does not perform a device task; `--front` applies to subsequent device work.

For device work, read the [complete rules](references/rules.md), [internal execution reference](references/cli.md) and [management contract](references/management.md) before first use, then complete the user's task through the shared queue.

## Skill input

Select this skill with `--init` for setup, or provide the device task. For device work, the optional `--front` flag explicitly requests priority:

```text
device-queue --front Verify the login change on the connected tablet.
```

Treat `--front` in the skill input as the user's priority request and pass it to the internal `request` or `run` when registering new work. It does not interrupt the holder or promote an existing request. This is skill input, not a standalone terminal command. Without the flag, apply the normal urgency rules below.

## Workflow

1. Prepare independent builds first. Resolve the actual stable hardware ID using side-effect-free discovery. Always retain its full ID; identical model names do not identify the same hardware.
2. Use the exact owner from startup context, including the distinct child ID for subagents. Reuse this session's active request when resuming; never use another session's grant. A parent must not hold a grant while waiting for a child's device-dependent work to complete.
3. Register a new request and wait locally for an explicit grant. Use managed `run` for a continuous synchronous script; for dependent UI/MCP calls, use one request/wait/exec/release lifecycle. Execute these internal steps yourself. Preserve actual capabilities within the owning task; do not ask the user for them.
4. Choose priority for a new job: add `--front` when the user requests priority in any language or concrete evidence establishes urgency. Briefly explain why and record it in the purpose. Honor explicit normal order. An existing grant is never interrupted; retries do not promote requests.
5. Keep waiting autonomously while the task is active. A timeout from `wait` leaves the request queued; repeat with the same ticket. Do not stop and ask the user to prompt again. Do independent work when possible.
6. After the explicit grant, target only its device. Keep ownership across installation, execution, interaction and evidence collection. End all dependent local and device-side activity before returning it; release before lengthy independent editing/building. Do not release a parent's grant from a subagent.
7. Report the actual device-task result separately from queue state and confirm the observed return. A grant is not a passed test. If ownership cannot be verified, continue independent work without device access.

All versions/projects/worktrees using the same plugin installation share its default state. Separate Codex homes/installations do not share a queue. Never override `--state-dir` during real work. Read-only discovery, queue queries, local builds and simulator-only tasks need no device grant. This plugin does not wake inactive chats.

For management requests, execute the focused sibling skill instead:

| User goal | Skill |
| --- | --- |
| Show holders and waiting jobs | [device-queue-status](../device-queue-status/SKILL.md) |
| Withdraw this session's wait | [device-queue-cancel](../device-queue-cancel/SKILL.md) |
| Return this session's grant | [device-queue-release](../device-queue-release/SKILL.md) |
| Recover verified abandoned work | [device-queue-recover](../device-queue-recover/SKILL.md) |
