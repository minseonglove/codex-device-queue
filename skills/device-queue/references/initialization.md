# Initialize the plugin

Use this mode for `device-queue --init` or an explicit plugin-initialization request. It authorizes configuring this plugin's two current installed hooks. Ordinary device work does not authorize changing hook trust. Initialization neither discovers nor reserves mobile devices.

## Agent workflow

1. Resolve the installed plugin root from this skill's location. Read its actual `hooks/hooks.json`, `hooks/session_start.py` and, on Windows, `hooks/session_start.cmd` before executing or trusting them. The hooks emit queue instructions and session ownership; they must not operate devices or register requests.
2. Locate a local Codex executable supporting plugins and hook settings. Prefer the executable used by the current client; a different, older `codex` on PATH may lack these APIs. Check candidates with `plugin --help`; use an absolute executable path when needed. Do not install another client or change PATH to work around a mismatch.
3. Run the installed internal helper yourself, using an available Python 3.7+ interpreter. It does not require startup owner context, which may not exist before initialization. On Windows use the existing `py -3`, `python3` or `python` interpreter and the actual shell's quoting.

   POSIX example, for internal agent execution only:

   ```sh
   python3 "$PLUGIN_ROOT/scripts/device_queue/cli.py" init --codex "$CODEX_EXE" --check
   ```

   `--check` exercises the installed OS-specific hook commands with synthetic session input and reads their native settings without changing trust. It reports `needs_initialization` or `ready`. If initialization is needed, run the same helper without `--check`. An explicit initialization request already authorizes this scoped change; do not add another confirmation step.
4. The helper uses native `hooks/list`, then the same `config/batchWrite` upsert as Codex's `/hooks` UI. Only the matching installed `codex-device-queue@codex-device-queue` hooks receive `enabled = true` and their exact current trusted hashes. It checks the configuration version and hook hashes, rereads native settings, and leaves unrelated hooks and settings unchanged. A previously disabled or modified own hook is included in explicit initialization. Administrator-controlled hooks are not overridden.
5. Report completion only for a successful helper response with `state: ready` and `hook_probe: passed`. Summarize the two hook states, then tell the user to start a new Codex session to load the rules. This verifies the hook commands and persisted settings; it does not inject startup context into the current chat. Repeating initialization is supported and an already-ready configuration is not rewritten.

Do not display internal Python commands, hashes or tokens as the user's setup instructions. Never use a global hook-trust bypass, trust unrelated hooks, edit trust hashes manually, or claim that initialization performed a device test. If discovery, the hook probe, concurrent configuration checks or native settings writes fail, report the observed error and the applicable manual path below. Do not claim success or silently install, enable or reconfigure a different plugin. After a concurrent-hook change, reread the installed files before retrying.

## Manual initialization

Use either native interface when the helper cannot complete, or when the user prefers manual setup:

| Interface | Steps |
| --- | --- |
| CLI | Open `/hooks`, find `codex-device-queue`, review and trust its `SessionStart` and `SubagentStart` hooks, and enable them if disabled. |
| GUI | Open **Review hooks** or **Settings > Hooks**, find `codex-device-queue`, review and trust those two hooks, and enable them if disabled. |

Start a new session afterward. A changed hook requires review again. Missing plugin hooks require checking that the intended plugin installation is enabled; do not substitute another cache copy.
