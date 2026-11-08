# Shared physical-device queue

User-facing queue management is provided through the focused status, cancel, release and recovery skills. Follow [their common execution contract](management.md); users do not execute internal Python commands or supply ownership capabilities.

## Automatic application

All Codex sessions sharing this computer must coordinate physical-device use through a common queue, without requiring the user to repeat this instruction. This applies across repositories, branches, worktrees, and chats, including when the agent independently decides that a physical-device test is needed.

Before installing or uninstalling an app, launching or stopping it, running tests, attaching a debugger, interacting with the UI, collecting screenshots, screen dumps or test logs, changing device settings or data, or otherwise using the device for verification, acquire exclusive use through the shared queue. Side-effect-free discovery of connected devices and their identifiers, queue queries, local builds, and simulator-only work do not require a physical-device grant.

## Queue identity and ownership

- Use one queue per stable physical-device identifier obtained from actual device discovery; ordinary requests are FIFO. Do not key queues by display name, connection order, OS, repository, branch, worktree, or session. Use the same exact hardware ID across sessions and tools. The installed tool treats IDs as opaque and case-sensitive and does not infer aliases; if tools expose different IDs for the same hardware, establish which identifier the shared queue already uses before registering rather than creating separate queues.
- Every session sharing a device must use the same Codex home and plugin installation, with shared state outside individual repositories and worktrees. Different physical devices have independent queues and may be used concurrently.
- Disconnecting and reconnecting the same device must preserve its identity and existing queue state. A reconnection does not make the device free or authorize a new queue.
- Each request must identify the owning session, target device, and purpose. Preserve the returned request ID and any ownership token; reuse the pending request when retrying or checking status instead of registering duplicates.
- Only an explicit, atomic grant from the manager authorizes device access. Being first in line, seeing no current owner, or observing an apparently idle screen is insufficient. Ordinary requests cannot skip existing waiters, even when the device appears available. The explicit urgent `--front` option only reorders pending work; it never overrides an existing grant.

## Required workflow

1. Prepare code, builds, and other independent prerequisites before requesting the device where practical, so a grant can be used promptly. Select the intended device using actual discovery; if the target is genuinely ambiguous, resolve it before requesting access.
2. Discover the installed shared queue tool and read its actual interface. Automatically register one request in the target device's queue; do not ask the user to enqueue it manually or remind you to do so.
3. Wait for the grant using the tool's supported wait or status interface. Prefer a wait handled by a local process or manager over frequent model-driven polling. Use bounded waits and continue waiting autonomously while the task is active, or do independent work and check again before device use. Do not end the task merely because the request is queued and expect the user to prompt you again.
4. After an explicit grant, confirm the target device and current ownership, then use only that device through the supported execution path. Carry the manager-issued ownership token when required and keep any lease alive according to the actual tool contract. Do not use a default-device command that could select another connected device.
5. Hold the grant for the whole continuous device-dependent sequence, including install, launch, interaction, and result capture. Avoid releasing between dependent commands that another session could disrupt. Release before returning to lengthy editing, building, or other work that does not require the device.
6. On completion, failure, or cancellation, stop or finish device-dependent child processes, end any continuing device interaction, and release the grant through the manager. Use a guaranteed cleanup path where the execution mechanism supports it. Cancel a pending request if its work is abandoned.

## Interruptions and recovery

- Never use another session's grant, remove its request, edit shared queue state by hand, or bypass the manager with a direct device command. All device-access mechanisms, including shell tools, MCP tools, IDE actions, and UI automation, must honor the same ownership.
- A timeout, expired heartbeat, disconnected device, or silent chat is not proof that the previous device workload stopped. Before a stale grant is reassigned, the manager must verify or enforce that the former owner's device operations can no longer continue. If that cannot be established, leave access blocked and report the condition.
- If this session loses ownership or cannot renew or validate it, stop issuing device operations and use the manager's recovery procedure. Do not release or modify a grant that now belongs to another session.
- Waiting in an active task and waking a finished or inactive chat are separate capabilities. Do not claim that the queue automatically wakes an inactive session unless a supported wakeup integration is actually configured. Do not create scheduled automations merely to implement this rule.

## Missing tooling and honest reporting

- The installed CLI below implements persistent tickets, atomic grants, managed local commands, and explicit recovery. These instructions also govern UI/MCP access that the CLI cannot intercept. Neither the rule nor the CLI automatically wakes inactive chats or universally blocks external device commands.
- If no shared queue tool is installed, its interface is unknown, it is unreachable, or it cannot establish exclusive ownership, do not perform physical-device work. Explain the specific blocker once and continue independent work where possible. Do not invent executable names, flags, request IDs, ownership tokens, or successful queue responses, and do not create a separate queue for this session as a workaround.
- State the scope accurately: queue registration is not a grant, a grant is not a completed test, and a written rule is not enforced device isolation. Report actual device work and results separately from waiting or blocked work.

## Installed CLI: required usage

- CLI: the bundled `scripts/device_queue/cli.py`, invoked with Python 3.7 or later. Use the absolute command supplied by the startup hook, or resolve it from this skill directory. In examples below, set `QUEUE_CLI` to that absolute Python file path and `QUEUE_OWNER` to the actual hook-supplied owner, and quote both. A subagent must use its distinct hook-supplied owner, never an inherited parent owner or grant.
- Full reference: [CLI reference](cli.md). Read it before first use or recovery.
- Shared state: `.device-queue-state@shared/queue.sqlite3` under the installed plugin’s version-common cache directory. Updates preserve this directory; standard Codex plugin removal deletes it. Different Codex homes or plugin installations do not share this state. Do not override `--state-dir` for real work; that option is only for isolated tool tests.
- Use the owner supplied by the startup hook for `request`/`run`, particularly for a subagent (parent session ID plus agent ID). Otherwise owner defaults to `CODEX_THREAD_ID`. If it is absent, pass `--owner` with an actual stable identifier for this session; do not use a shared project/branch name. A request retry by the same owner/device reuses the active ticket/token.

Prefer one managed invocation for a continuous test script:

```sh
python3 "$QUEUE_CLI" run --owner "$QUEUE_OWNER" --device '<stable-device-id>' --purpose '<test-purpose>' -- /absolute/path/to/device-test-script
```

`run` waits locally until granted and automatically returns the device after synchronous work finishes, including nonzero exits and handled cancellation. Keep the active process and wait for its result using the shell tool's supported process/session interface; do not ask the user to prompt again when the device becomes available. Queue events are JSON on stderr, and command output is preserved. It passes `DEVICE_QUEUE_DEVICE` to the command, but does not rewrite device-selection arguments: explicitly target that ID in the test script. Complete independent builds before entering the queue where practical.

For work spanning separate UI/MCP calls, retain one interactive grant:

```sh
python3 "$QUEUE_CLI" request --owner "$QUEUE_OWNER" --device '<stable-device-id>' --purpose '<test-purpose>'
python3 "$QUEUE_CLI" wait --ticket '<returned-ticket>' --token '<returned-token>' --timeout 30
python3 "$QUEUE_CLI" exec --ticket '<returned-ticket>' --token '<returned-token>' -- /absolute/path/to/device-command '<arguments>'
python3 "$QUEUE_CLI" release --ticket '<returned-ticket>' --token '<returned-token>'
```

- Preserve the actual returned ticket/token in this session. `request` always queues; only a successful `wait` with `state: granted` permits access. Exit 3 from `wait` leaves it queued: repeat `wait` with the same ticket until granted while the task is active. `run --wait-timeout <seconds>` instead cancels its own queued request on timeout.
- Use `exec` for shell commands within an interactive grant. Keep that grant through dependent UI/MCP calls, stop continuing device work, then `release` in cleanup. There is no lease expiry; manual grants require explicit return.
- To abandon a pending request, use `cancel --ticket '<returned-ticket>' --token '<returned-token>'`. Do not cancel a granted ticket as a substitute for stopping its work and releasing it.
- Inspect queues with `status` or `status --device '<stable-device-id>'`; add `--history` for closed requests. Status does not expose ownership tokens. Use `--help` or `<command> --help` for actual options.
- A managed command may not daemonize, escape its process group, or leave device-side work continuing after the script exits. Handle such cleanup inside the script, or retain an interactive grant until verified stopped. A surviving registered group or inherited lock prevents release/recovery.
- After an unhandled crash, first inspect the old workload and device-side activity. Only after verifying they cannot continue may you use `recover --ticket '<abandoned-ticket>' --confirm-idle --reason '<verification-evidence>'`. Never treat `--confirm-idle` as a routine override. Recovery still refuses live managed controllers or registered work, and elapsed time alone is not evidence of idleness.

## Urgent new requests

- Choose queue placement automatically when registering a new job. If the user asks for this task to be prioritized in any language (for example, "this is urgent; prioritize it", "give this task priority", "run this verification as a top priority"), add `--front` to `request` or `run` without asking the user to spell out the flag or approve it again. Interpret priority in the task/device-queue context; an instruction such as "install first, then launch" only specifies step order.
- The agent may also choose `--front` without an explicit priority request when concrete context establishes urgency: for example, verifying a fix for a confirmed active production incident, a known release deadline that is at risk, or a time-critical dependency whose delay would have a demonstrated immediate impact. Base this on actual task evidence, not an invented deadline or an assumption that this session matters more than others.
- Before priority registration, briefly tell the user the concrete reason and include that reason alongside the test purpose in `--purpose`. This is an explanation, not a request for permission. For example: "The release deadline is in 15 minutes and this is the remaining device verification, so I am registering it at the front of the waiting queue."
- Respect an explicit user request to retain normal order or avoid priority. Otherwise, use normal FIFO when evidence is absent or ambiguous. Merely wanting faster results, having a short test, already waiting, or being unable to proceed without the device does not by itself establish urgency. Do not make `--front` a default or repeatedly cancel/requeue to leapfrog other waiters.
- The new ticket is inserted before this device's pending tickets. A granted ticket remains first, even between its commands, and must release normally before urgent work can acquire the device. Do not cancel or interrupt the holder, force recovery, or take another device's grant to accelerate urgent work.
- Multiple front insertions are literal prepends: the newest goes first among waiters; the remaining normal jobs keep their relative FIFO order. Other devices' queues are independent. `status` positions and ordering include the current holder first when one exists.
- A duplicate `request`, including `request --front`, preserves the existing active ticket/token/order instead of creating or promoting another request. Preserve retry identity; do not change owners or cancel/requeue merely to skip ahead.

```sh
python3 "$QUEUE_CLI" run --owner "$QUEUE_OWNER" --device '<stable-device-id>' --purpose '<priority-reason-and-test-purpose>' --front -- /absolute/path/to/device-test-script
```
