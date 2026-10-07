---
name: titles
description: Manage the terminal-tab title auto-restore that keeps Claude Code's `✳ <topic>` tab names alive across VS Code window reloads. Use for "/titles", "/titles restore|status|install|uninstall", "my terminal tab names disappeared after reloading VS Code", "bring back the auto-named terminal titles", "restore the terminal titles". Duplicates Claude Code's own OSC-0 titling (cli.js) but re-emits every turn instead of only on new-topic, so a reload can't strand a blank tab.
---

# titles — reload-proof terminal tab names

## Problem it solves
Claude Code auto-names each VS Code terminal tab `✳ <aiTitle>` by writing an OSC 0 escape
sequence (`\x1b]0;✳ <title>\x07`) to the terminal — but `cli.js` only emits it when it
detects a **new topic** (`if (Z.isNewTopic && Z.title) QG1(Z.title)`). A VS Code window
reload wipes the tab title, and because a normal follow-up message isn't a *new topic*,
Claude never re-emits it. The tab stays blank until an unrelated new topic happens to start.

## How the fix works
A hook script shipped with this skill — `~/.claude/skills/titles/terminal-title.py` — runs
on **every** `Stop` and `UserPromptSubmit`. It reads the session's transcript (exact
`transcript_path` from the hook's stdin JSON), grabs the most recent
`{"type":"ai-title","aiTitle":"…"}` record via a reverse scan from EOF, finds its own
terminal, and re-emits `✳ <aiTitle>`. Because it fires every turn (not only on new topics),
the title comes back the moment a reloaded session next does anything. Each session
self-restores independently (own transcript, own tty).

It is **opt-in**: nothing runs until `/titles install` wires it into `settings.json`.
macOS only — it targets `ttysNNN` devices and uses `ps -o lstart` / `lsof`.

Key invariants (don't break these when editing):
- **Targets the tty via the parent-process walk, NOT `/dev/tty`.** Hooks and tool
  subprocesses run without a controlling terminal (`open('/dev/tty')` → ENXIO), but they're
  descendants of the `claude` TUI which is on a real `ttysNNN` — `own_tty()` walks up ppid
  to find it and writes to `/dev/ttysNNN`. (`/dev/tty` is only a last-resort fallback.)
- Writes ONLY to the tty device, never stdout — a `UserPromptSubmit` hook's stdout is
  injected into Claude's context, so the script prints nothing there (the `--all` summary
  goes to stderr).
- No regeneration / no model call: it mirrors the exact `aiTitle` Claude already generated.
- Strips control characters from the title before emitting, so a model-generated title can't
  terminate the OSC sequence early or inject further escapes.
- Fallback to the cwd basename only when no `aiTitle` exists yet, so a tab is never blank.

`--all` maps every open tab to its session by **time-correlation**: tabs often share a cwd
(e.g. all launched from `~`) and neither the process env (`CLAUDE_CODE_SSE_PORT` is
window-wide, not per-session) nor the transcript records carry a tty/pid — but a claude
process writes its first transcript record 1–2s after launch, so matching each tab's
process-start (`ps -o lstart`) to the session whose first record is closest (one-to-one,
±20s) is exact.

Wiring lives in `~/.claude/settings.json` under `hooks.Stop` and `hooks.UserPromptSubmit`.

## Commands

Parse the argument after `/titles` (default = `status`):

- **`status`** (default) — confirm it's live:
  - script exists + is executable: `test -x ~/.claude/skills/titles/terminal-title.py`
  - hooks wired: grep `terminal-title.py` in `~/.claude/settings.json` under `Stop` and `UserPromptSubmit`
  - JSON still valid: `python3 -m json.tool ~/.claude/settings.json >/dev/null && echo OK`.
- **`restore`** — retitle **every open tab** right now (the "they all went blank after a
  reload, fix them" button): `python3 ~/.claude/skills/titles/terminal-title.py --all`. The
  summary (tty → title) prints on stderr. Tabs that don't match a session within ±20s fall
  back to the cwd basename and self-correct on their next turn.
- **`install`** — idempotent (re)install. Ensure the script is executable
  (`chmod +x ~/.claude/skills/titles/terminal-title.py`), then ensure `settings.json` has a
  `{"type": "command", "command": "~/.claude/skills/titles/terminal-title.py"}` hook in
  both `hooks.Stop` and `hooks.UserPromptSubmit` (append a new group; never clobber existing
  hooks; skip a section that already references `terminal-title.py`). Validate the file
  still parses: `python3 -m json.tool ~/.claude/settings.json`.
- **`uninstall`** — remove ONLY the two `terminal-title.py` hook groups from `settings.json`
  (leave every other hook untouched), re-validate JSON. Leave the script on disk.

## Limitation (state it honestly)
No hook fires at the literal instant of a reload — the `claude` process persists across it —
so restoration lands on the session's **next interaction** (its next `Stop`/prompt), not the
millisecond you reload. `/titles restore` is the manual "do it now" for every open tab.

## Verifying an actual emit
The Bash tool has no VS Code tab, so a real end-to-end check requires the live terminal:
reload VS Code, send one message, and confirm the tab re-reads `✳ <topic>`. From a script
you can only verify title *extraction* (the `aiTitle` it resolves) and that stdout stays
empty — which is what `tests/test_terminal_title.py` covers.
