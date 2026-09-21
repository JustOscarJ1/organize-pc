---
name: organize-pc
description: Use when the user says "organize my PC", "clean up my computer", "what are my agents doing", "which Claudes are done", "close everything I don't need", "sort my notepads", or comes back to a machine covered in terminals and tabs. Finds every live Claude Code session and what it waits on, banks and closes spent Notepad tabs, closes everything not load-bearing, kills leaked servers, and lays out virtual desktops (Claude left, subject centre, state right) with one global STATE pane.
---

# Organize PC

The machine is the state. Every Claude Code session is a lane; a lane is done when nobody has spoken in it for six hours and nothing is spinning. Notepad tabs are prompts that already went to an agent, or thoughts that have not. Everything else on screen is either load-bearing today or a leak.

The tool is `pc` (launcher in `%USERPROFILE%\.local\bin\pc.cmd`, source `pc.py` beside this file, policy in `policy.json`). Dry run by default; `--apply` arms.

## The run

1. `pc scan` and read it. Lanes carry a verdict: `self`, `busy` (title spinner or a subagent transcript touched in the last 10 min), `idle` (spoken in under 6 h), `done`, `empty` (no transcript). Notepad tabs carry `close-saved` (file on disk), `close-empty`, `bank-close` (text already appears in a user message of some session), `keep` (unsent thought or personal).
2. Disagree with a verdict by looking, not by editing the tool: open the lane's transcript tail, read the tab. If the rule is wrong, change `policy.json` (hours, patterns, app lists) and rerun.
3. `pc organize --apply`. Order inside: bank every unsaved tab to `~/pc/bank/` and read it back; write `lanes/<title>.txt` for every lane that will close; kill done and empty sessions with their cmd host (their terminal windows go with them); close spent Notepad tabs (Ctrl+W, answer Don't save); WM_CLOSE then kill the close-list apps; kill leaked servers and orphans; build desktops; write `STATE.txt`.
4. Look at the result. `pc scan` again, and switch through the desktops. Report what closed, what stayed, what each live lane waits on, and the rulings that closed lanes left behind (they are in STATE.txt under CLOSED, RESUMABLE).

## Desktops

Monitor names come from `policy.json`; with fewer monitors than named, the primary is split into thirds. Desktop `Now` holds the session running the organize: its terminal left, STATE centre. One desktop per other live lane, named by its title: terminal left, any window sharing two title words centre, the lane's state file right in a `pc view` pane. Desktop `Notes`: the Notepad windows that still need input tiled left and centre, STATE right. `pc view <file>` is a black pane, white text, red for lines that need the user, green headers; Ctrl+S writes back; it reloads when the file changes.

## Resuming a closed lane

`pc resume <words from the title>` prints `claude --resume <session id>`. Nothing is lost by closing: the transcript is the session.

## Traps

- Console titles come from `AttachConsole`; the tool frees its own console first, so run it from a fresh process, never import it into a live session's Python.
- Notepad exposes a tab's text only while the tab is selected and its window is on the current desktop; the scan flicks through every tab and switches desktops as needed. Do not touch Notepad while it runs.
- The tab close button exists only under the mouse; the tool uses Ctrl+W and finds the Don't save button by name. Another language pack needs the name changed in `notepad_close_tab`.
- Transcript mtimes get bumped by the harness long after the last message; idle hours come from the last user or assistant timestamp, never the file.
- Killing a session kills its subagents, MCP servers and any server an agent started from that shell. Busy is decided before kill, never after. STATE names what stopped listening.
- A new window is born on whichever desktop is current, and pyvda cannot always move a window it has just created. The tool switches to the target desktop, then spawns the pane with its geometry in argv. Keep that order.
