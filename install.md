# agentplay

> Replay coding-agent sessions (Claude Code, Codex, Cursor, Copilot, OpenCode, Kiro) as a map and a timeline. Local only: it reads transcripts already on disk and serves a viewer on 127.0.0.1.

I want you to set up agentplay on this machine and open it with my sessions mapped. Work through the steps yourself and stop to ask me only where a step says to, or if something fails.

OBJECTIVE: the agentplay viewer is running locally and shows the coding-agent sessions on this machine.

DONE WHEN: `/api/status` on the port the viewer printed (for example `http://127.0.0.1:7317/api/status`) returns JSON with `"available": true` and a `mapCount` above 0, and you have given me the URL.

## TODO

- [ ] Pick a way to run agentplay
- [ ] Confirm the version
- [ ] Find the agents with sessions on this machine
- [ ] Build the session maps
- [ ] Start the viewer
- [ ] Verify it and hand over

## Ground rules

- Ask me before installing anything system-wide (uv, pipx, a Python).
- Do not change any agent's settings or files. agentplay only reads them.
- Do not upload anything. agentplay makes no network calls with session data. The viewer may offer an optional email signup; leave that to me.

## Pick a way to run agentplay

Use the first that is already available. Each needs nothing else installed:

1. **uv:** run commands as `uvx agentplay@latest <command>`. uv brings its own Python. `@latest` stops uv reusing an older cached copy.
2. **pipx:** `pipx run agentplay <command>`.
3. **pip:** Python 3.11 or newer in a virtual environment, then `pip install -U agentplay` and run `agentplay <command>`.

If none of these is available, ask me before installing uv with its official installer: `curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS and Linux, or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"` on Windows.

Below, `agentplay` means whichever form you picked.

## Confirm the version

Run `agentplay --version` and tell me the version.

## Find the agents with sessions on this machine

Run `agentplay agents`. A filled mark (`●`) means that agent's folder exists on this machine, not that it holds sessions; the build step below confirms that.

If no agent is marked, or the build finds no sessions, ask me where my sessions live and set the matching variable for the remaining commands: `AGENTPLAY_CLAUDE_HOMES`, `AGENTPLAY_CODEX_HOMES`, `AGENTPLAY_CURSOR_HOMES`, `AGENTPLAY_COPILOT_HOMES`, `AGENTPLAY_COPILOT_VSCODE_HOMES`, `AGENTPLAY_OPENCODE_HOMES`, `AGENTPLAY_KIRO_HOMES` or `AGENTPLAY_ROO_HOMES` (comma-separated paths). On WSL, Windows-side sessions are under `/mnt/c/Users/<name>/`.

## Build the session maps

Run `agentplay build` and summarise its output: how many sessions were found, built, unchanged and skipped. If it found none, go back to the previous step and ask me where my sessions live. Then run `agentplay list --limit 5` and show me the newest sessions.

## Start the viewer

Start it in the background, because it keeps running: `agentplay view --no-build`. It serves on `http://127.0.0.1:7317` and opens my browser. If port 7317 is busy, it picks a free port and says so; either way it prints the URL it is serving on (`Viewer → http://127.0.0.1:<port>/`). Use that URL from here on.

## Verify it and hand over

Check `/api/status` on the port the viewer printed, for example `http://127.0.0.1:7317/api/status` (with `curl -s` on macOS and Linux, `curl.exe -s` or `Invoke-RestMethod` on Windows). It should return `"available": true` with a `mapCount` above 0.

Then tell me the URL, how to stop the server, and that later runs of `agentplay` rebuild only the sessions that changed.

EXECUTE NOW: complete the TODO list above until the DONE WHEN condition holds.
