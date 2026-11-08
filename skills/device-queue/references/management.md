# Internal skill execution contract

This document is for the agent. Execute the packaged helper yourself using the available shell tool. Do not return a command for the user to run, ask the user to locate the plugin, or ask for a ticket/token. Skills complete the requested action and report the observed result in the user's language.

## Runtime and identity

The startup context supplies the absolute queue CLI command and exact session owner. Resolve missing paths from the loaded skill: every skill directory is under `<plugin-root>/skills/`; the CLI is `<plugin-root>/scripts/device_queue/cli.py`. Invoke it through the startup-supplied Python interpreter, with quoted absolute paths. On Windows use its PowerShell form including `-X utf8`; on POSIX use POSIX quoting. The shell examples below use POSIX syntax and must be adapted to the actual shell. Never assume the Windows interpreter is named `python3` or use Unix shlex quoting on Windows. Do not install dependencies or change shell startup files.

In the internal examples, QUEUE_CLI is that actual path and QUEUE_OWNER is the actual hook-supplied owner. Set these locally in the same tool invocation or substitute the resolved values safely; they are not automatically exported variables. Subagent owners include the parent session ID and agent ID. Never fall back to an inherited parent owner, project name or invented session ID. If runtime identity cannot be established, explain that blocker; do not ask for ownership tokens or operate under another identity.

All real sessions use the default shared state. `--state-dir` is exclusively for isolated automated tests, never a solution for unavailable ownership or a busy device. Queue queries require no physical-device grant.

## Internal operations

```sh
python3 "$QUEUE_CLI" manage inspect --owner "$QUEUE_OWNER"
python3 "$QUEUE_CLI" manage cancel --owner "$QUEUE_OWNER"
python3 "$QUEUE_CLI" manage release --owner "$QUEUE_OWNER" --confirm-idle --reason '<actual evidence that this session stopped all device activity>'
```

Use `--device '<exact stable ID>'` to narrow a known target. When a ticket was already resolved from this task or a candidate response, also pass `--ticket '<actual returned ticket>'` to pin the action to that request. Never substitute a new active request if the selected one has closed.

The management adapter resolves capabilities internally. Do not call `request` to recover a forgotten token: the original request may have closed, and a retry could create new work. Do not read or edit the SQLite database from a skill.

- `outcome: status`: render the returned device groups. Each holder/waiter contains the recorded purpose, exact ID and a `current_session` flag. `waiting_position` is the order among waiters; a queued head without a grant is still waiting.
- `selection_required`: no mutation occurred. Show the candidate device IDs and purposes and ask only which target the user means. Then execute the same action with the selected device and ticket. Never choose the newest request or act on every candidate.
- `no_request`: no suitable session request was found; report there is nothing to cancel/return. Do not register one.
- `cancelled` / `released`: the returned request is the readback from the mutation transaction. Report its exact device and resulting state. Do not claim a successor already obtained a grant without actual readback.
- `already_closed`: report the selected request's actual closed state. Leave any replacement request alone.
- `unauthorized`: the request belongs to another session. Stop; do not copy its owner into a second mutation attempt.
- `already_granted`: cancellation cannot withdraw a grant. Report that this request already holds the device; do not silently release it or terminate work.
- `not_granted`: a waiting request cannot be returned as a grant. Explain that distinction; do not cancel it automatically.
- `device_busy`: tracked work or a lock remains active. Keep ownership intact and report what the tool actually established.
- `confirmation_required`: required idle evidence is missing. Establish the missing facts; never add a confirmation flag merely to get past the error.

Read both JSON and exit status. A tool error is not a successful action. Readback is required for a claimed mutation; if the result is unavailable, inspect the selected ticket before retrying rather than guessing.

## Status and target display

Always show the full stable hardware ID. Add a friendly/model name only when an existing side-effect-free discovery tool actually resolves that exact hardware ID. Keep identical model names in separate groups. If a discovery tool exposes a different ID, verify the mapping from actual device data; matching model names is not sufficient.

Queue records are not a connected-device inventory. Without successful discovery, show queue IDs only and do not invent idle devices, names or connection status. A grant proves ownership, not that a test is running or passed. Describe it as a held grant. Label the exact current owner as this session; distinguish other owners using stable short labels only when useful.

Normal output consists of the device name when known plus full ID, action/result, purpose where useful, and waiting position where relevant. Never show tokens, internal Python commands or installation/cache paths as the normal answer. If blocked, state the concrete blocker and the missing evidence or target selection, not manual CLI instructions.

## Recovery

Recovery is the separate explicit administrative workflow. Resolve its intended ticket from `manage inspect` (or token-free `status --history` for a previously resolved closed ticket). Do not require the user to paste an internal ticket. Recovery may target another abandoned session only when the user clearly requested that specific work/device be recovered.

Check old controllers/registered operations and available evidence about UI/MCP/debugger/device-side work without starting new device operations under an unverified grant. Age, disconnection and an idle-looking screen are insufficient. If external work cannot be established stopped, leave the grant untouched and report the missing evidence.

Only after verification execute the existing guarded operation:

```sh
python3 "$QUEUE_CLI" recover --ticket '<resolved actual ticket>' --confirm-idle --reason '<actual verification evidence>'
```

It still refuses live managed controllers, locks or registered processes. Never force recovery or kill another session's processes. Report the returned recovered/closed state and exact device ID; do not claim tests completed or an inactive chat was awakened.
