# tmux-session

Save a live tmux session and start it again later: window indexes and names, pane geometry, working directories, vim commands, and Cursor agent chats (`agent --resume=<id>`).

The layout string tmux prints cannot be replayed. Those numbers are live pane ids. This stores geometry and splits along the same seams.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/suxatcode/tmux-session/main/install.sh | sh
```

That puts `tmux-session-save`, `tmux-session-start`, and `tmux-session.py` in `~/.local/bin`. Override the directory with `TMUX_SESSION_INSTALL_DIR`.

Needs `python3`, `tmux`, and `lsof` (used when an agent process does not show `--resume=` on its command line). `~/.local/bin` must be on `PATH`.

## Use

From inside the session you want to keep:

```bash
tmux-session-save
```

After that session is closed:

```bash
tmux-session-start
```

`start` attaches when you are not already inside tmux. It refuses to open a second copy of an agent chat that is still running. Pass `--force` only when you mean to.

```bash
tmux-session-save --session 0
tmux-session-start --dry-run
tmux-session-start --no-attach
tmux-session.py save --help
```

Empty shells are restored as empty shells. A vim pane is reopened with the saved command. Other foreground programs are not restarted.

## Where the session is stored

Default file: `$HOME/.local/tmux-session.json`

Point it somewhere else with `TMUX_SESSION_FILE` (a file path, not a directory):

```bash
export TMUX_SESSION_FILE="$HOME/code/tmux-session.json"
```

`--file` overrides the environment variable for one run.
