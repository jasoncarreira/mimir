---
name: tmux
description: Remote-control tmux sessions for interactive CLIs by sending keystrokes and scraping pane output. Use when you need an interactive TTY (REPLs, agents that prompt) or want to run multiple long-lived processes in parallel and poll their state. For non-interactive long-running jobs prefer the long-running-jobs skill.
---

<!-- desc: Remote-control tmux sessions for interactive CLIs by sending keystrokes and scraping pane output. -->

# tmux

Use tmux only when you need an interactive TTY. For non-interactive long-running
tasks, prefer Bash's `run_in_background` (the long-running-jobs skill).

## Quickstart (isolated socket)

```bash
SOCKET_DIR="${MIMIR_TMUX_SOCKET_DIR:-${TMPDIR:-/tmp}/mimir-tmux-sockets}"
mkdir -p "$SOCKET_DIR"
SOCKET="$SOCKET_DIR/mimir.sock"
SESSION=mimir-python

tmux -S "$SOCKET" new -d -s "$SESSION" -n shell
tmux -S "$SOCKET" send-keys -t "$SESSION":0.0 -- 'PYTHON_BASIC_REPL=1 python3 -q' Enter
tmux -S "$SOCKET" capture-pane -p -J -t "$SESSION":0.0 -S -200
```

After starting a session, print the monitor command so the operator can attach
if they want to peek:

```
To monitor:
  tmux -S "$SOCKET" attach -t "$SESSION"
  tmux -S "$SOCKET" capture-pane -p -J -t "$SESSION":0.0 -S -200
```

## Socket convention

- Use `MIMIR_TMUX_SOCKET_DIR` (default `${TMPDIR:-/tmp}/mimir-tmux-sockets`).
- Default socket path: `"$MIMIR_TMUX_SOCKET_DIR/mimir.sock"`.
- Isolated sockets keep mimir's tmux sessions out of any operator-owned tmux
  server running in the same container.

## Targeting panes

- Target format: `session:window.pane` (defaults to `:0.0`).
- Keep session names short; avoid spaces.
- Inspect: `tmux -S "$SOCKET" list-sessions`, `tmux -S "$SOCKET" list-panes -a`.

## Finding sessions

- List sessions on your socket: `skills/tmux/scripts/find-sessions.sh -S "$SOCKET"`.
- Scan all sockets in `MIMIR_TMUX_SOCKET_DIR`: `skills/tmux/scripts/find-sessions.sh --all`.

## Sending input safely

- Prefer literal sends: `tmux -S "$SOCKET" send-keys -t target -l -- "$cmd"`.
- Control keys: `tmux -S "$SOCKET" send-keys -t target C-c`.

## Watching output

- Capture recent history: `tmux -S "$SOCKET" capture-pane -p -J -t target -S -200`.
- Wait for a prompt / pattern: `skills/tmux/scripts/wait-for-text.sh -t session:0.0 -p 'pattern'`.
- Attaching is OK; detach with `Ctrl+b d`.

## Spawning processes

- For Python REPLs set `PYTHON_BASIC_REPL=1` (the rich REPL breaks send-keys flows).

## Cleanup

- Kill a session: `tmux -S "$SOCKET" kill-session -t "$SESSION"`.
- Kill all sessions on a socket: `tmux -S "$SOCKET" list-sessions -F '#{session_name}' | xargs -r -n1 tmux -S "$SOCKET" kill-session -t`.
- Remove the socket entirely: `tmux -S "$SOCKET" kill-server`.

## Helper: wait-for-text.sh

`skills/tmux/scripts/wait-for-text.sh` polls a pane for a regex (or fixed
string) with a timeout.

```bash
skills/tmux/scripts/wait-for-text.sh -t session:0.0 -p 'pattern' [-F] [-T 20] [-i 0.5] [-l 2000]
```

- `-t`/`--target` pane target (required)
- `-p`/`--pattern` regex to match (required); add `-F` for fixed string
- `-T` timeout seconds (integer, default 15)
- `-i` poll interval seconds (default 0.5)
- `-l` history lines to search (integer, default 1000)
