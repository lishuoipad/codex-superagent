# Local development dashboard

A read-only Windows WPF view of the Router, Workers, subagents, project-plan associations and recent public progress.

Requires **PowerShell 7.5+** and **Python 3.10+**. The collector uses the Python standard library. No model calls or network requests; it does not dispatch work or install a service.

## Configure and open

Copy `project.example.json` to a private `project.local.json`. Set your own Codex state database, checkpoint folder, project plan and current team registry. `runtime_status: null` follows `team.runtime.statusPath`. The actual native Codex record format is version-dependent.

~~~powershell
pwsh -NoProfile -STA -File ./Android-Dev-Status.ps1 -ConfigPath ./project.local.json -PythonPath /absolute/path/to/python.exe
~~~

On Windows you can also double-click or invoke the generic CMD wrapper with its configuration:

~~~text
open-status.cmd project.local.json C:\path\to\python.exe
~~~

`pwsh.exe` must be available on PATH for that CMD wrapper. For a local zero-argument shortcut, supply the explicit paths above in your own private launcher.

A read-only data check without opening the window:

~~~text
python -B development_status.py --config project.local.json --once
~~~

## What the view means

- Dynamic mode reads the current Router and Workers from the team/runtime files. New or removed members appear on a subsequent refresh; fixed `threads` lists remain available for other projects.
- In-use Workers include active, waiting, paused and unknown unfinished records. Completed history and the Router do not count against the displayed cap.
- Public progress and control records refresh about every five seconds; subagent discovery is about every thirty seconds. The display is recorded evidence, not instantaneous native execution state.
- A finished chat turn is not task acceptance. Unknown, missing or stale records stay visible as such. Explicit pause controls take precedence.
- Selecting a row shows plan associations, public source text and validation context. Completed history is collapsed by default.

The reader bounds transcript tails and never displays internal reasoning, user prompts or tool outputs. It opens the Codex database read-only and may use an immutable snapshot when normal access is unavailable, which can delay newly registered chats. Errors remain visible instead of being treated as proof that development stopped.

Keep `*.local.json`, SQLite files, real chat IDs, checkpoints, screenshots and private records out of public commits. The example uses placeholders. Closing the window stops its own reader process; it does not stop development.
