#!/usr/bin/env python3
"""Save and restore a tmux session.

The layout string tmux prints cannot be replayed: the numbers in it are pane
ids from the live session. Geometry is stored instead, and restore splits
along the same seams. Each agent pane records the chat id so `agent --resume`
opens that same conversation.
"""

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

# Full path, not a directory: one JSON document is the whole session.
ENV_SESSION_FILE = "TMUX_SESSION_FILE"


def default_session_file():
    override = os.environ.get(ENV_SESSION_FILE)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "tmux-session.json"


def tmux(*args, check=True):
    result = subprocess.run(
        ["tmux", *args], text=True, capture_output=True
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SystemExit(detail or f"tmux failed: {' '.join(args)}")
    return result


def attached_or_default(explicit):
    if explicit:
        return explicit
    listed = tmux(
        "list-sessions", "-F", "#{session_attached} #{session_name}", check=False
    )
    if listed.returncode != 0:
        raise SystemExit("no tmux server is running")
    attached = []
    for line in listed.stdout.splitlines():
        flag, _, name = line.partition(" ")
        if flag == "1":
            attached.append(name)
    if len(attached) == 1:
        return attached[0]
    if "0" in {line.split()[-1] for line in listed.stdout.splitlines() if line.strip()}:
        return "0"
    raise SystemExit("pass --session; more than one tmux session is attached")


def process_table():
    raw = subprocess.check_output(["ps", "-ax", "-o", "pid=,ppid=,command="], text=True)
    children = {}
    commands = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, command = parts
        commands[pid] = command
        children.setdefault(ppid, []).append(pid)
    return children, commands


def resume_from_command(command):
    marker = "--resume="
    if marker not in command:
        return None
    return command.split(marker, 1)[1].split()[0]


def chat_id_from_open_store(pid):
    listed = subprocess.run(
        ["lsof", "-p", pid], text=True, capture_output=True
    )
    if listed.returncode != 0:
        return None
    paths = []
    for line in listed.stdout.splitlines():
        path = line.split()[-1] if line.split() else ""
        if path.endswith("/store.db"):
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            paths.append((size, path))
    if not paths:
        return None
    # A fresh empty store can sit beside the real conversation. The live chat
    # is the larger database.
    _size, path = max(paths)
    return Path(path).parent.name


def agent_id_for_shell(shell_pid, children, commands):
    candidates = [shell_pid, *children.get(shell_pid, [])]
    for pid in candidates:
        command = commands.get(pid, "")
        found = resume_from_command(command)
        if found:
            return found
        if "cursor-agent" in command or command.endswith(" agent") or "/agent " in command:
            found = chat_id_from_open_store(pid)
            if found:
                return found
    return None


def foreground_command(shell_pid, children, commands):
    for pid in children.get(shell_pid, []):
        command = commands.get(pid, "")
        if "cursor-agent" in command or resume_from_command(command):
            continue
        # Skip the sandbox wrapper shells the agent itself spawns.
        if command.startswith("/bin/zsh -c") or command.startswith("builtin "):
            continue
        return command
    return None


def pane_record(fields, children, commands):
    shell_pid = fields["pid"]
    agent_id = agent_id_for_shell(shell_pid, children, commands)
    record = {
        "index": int(fields["index"]),
        "left": int(fields["left"]),
        "top": int(fields["top"]),
        "width": int(fields["width"]),
        "height": int(fields["height"]),
        "cwd": fields["cwd"],
        "title": fields["title"],
    }
    if agent_id:
        record["kind"] = "agent"
        record["agent_id"] = agent_id
        record["command"] = f"agent --resume={agent_id}"
        return record
    command = foreground_command(shell_pid, children, commands)
    if command and command.split()[0].endswith("vim"):
        record["kind"] = "vim"
        record["command"] = command
        return record
    record["kind"] = "shell"
    return record


def capture(session):
    windows_raw = tmux(
        "list-windows",
        "-t",
        session,
        "-F",
        "#{window_index}\t#{window_name}",
    ).stdout.splitlines()
    panes_raw = tmux(
        "list-panes",
        "-t",
        session,
        "-a",
        "-F",
        "\t".join(
            [
                "#{window_index}",
                "#{pane_index}",
                "#{pane_pid}",
                "#{pane_left}",
                "#{pane_top}",
                "#{pane_width}",
                "#{pane_height}",
                "#{pane_current_path}",
                "#{pane_title}",
            ]
        ),
    ).stdout.splitlines()
    children, commands = process_table()
    by_window = {}
    for line in panes_raw:
        index, pane, pid, left, top, width, height, cwd, title = line.split("\t", 8)
        by_window.setdefault(index, []).append(
            {
                "index": pane,
                "pid": pid,
                "left": left,
                "top": top,
                "width": width,
                "height": height,
                "cwd": cwd,
                "title": title,
            }
        )
    windows = []
    for line in windows_raw:
        index, name = line.split("\t", 1)
        panes = [
            pane_record(fields, children, commands)
            for fields in sorted(by_window.get(index, []), key=lambda item: int(item["index"]))
        ]
        windows.append({"index": int(index), "name": name, "panes": panes})
    return {"version": 1, "name": session, "windows": windows}


def span(panes, key, size_key):
    start = min(pane[key] for pane in panes)
    end = max(pane[key] + pane[size_key] for pane in panes)
    return end - start


def groups_at(panes, axis, cut):
    if axis == "x":
        first = [pane for pane in panes if pane["left"] + pane["width"] <= cut]
        second = [pane for pane in panes if pane["left"] >= cut]
    else:
        first = [pane for pane in panes if pane["top"] + pane["height"] <= cut]
        second = [pane for pane in panes if pane["top"] >= cut]
    return first, second


def split_tree(panes):
    """Binary split that matches how tmux actually divided the window.

    A one-column border sits between panes, so a seam is accepted when every
    pane lies wholly on one side of it.
    """
    if len(panes) == 1:
        return {"kind": "leaf", "pane": panes[0]}
    origin_x = min(pane["left"] for pane in panes)
    origin_y = min(pane["top"] for pane in panes)
    best = None
    for axis, key, size_key, origin in (
        ("x", "left", "width", origin_x),
        ("y", "top", "height", origin_y),
    ):
        total = span(panes, key, size_key)
        candidates = set()
        for pane in panes:
            candidates.add(pane[key])
            candidates.add(pane[key] - 1)
        for cut in candidates:
            if cut <= origin:
                continue
            first, second = groups_at(panes, axis, cut)
            if not first or not second or len(first) + len(second) != len(panes):
                continue
            first_span = span(first, key, size_key)
            balance = abs(first_span - total / 2)
            if best is None or balance < best[0]:
                best = (balance, axis, first, second, key, size_key)
    if best is None:
        raise SystemExit("could not find a split that covers every pane")
    _balance, axis, first, second, key, size_key = best
    whole = span(first, key, size_key) + span(second, key, size_key)
    percent = int(round(100 * span(second, key, size_key) / whole))
    percent = min(99, max(1, percent))
    return {
        "kind": "split",
        "axis": axis,
        "percent": percent,
        "first": split_tree(first),
        "second": split_tree(second),
    }


def leaf_cwd(node):
    if node["kind"] == "leaf":
        # split-window -c needs a directory even when the saved pane has none.
        return node["pane"].get("cwd") or str(Path.home())
    return leaf_cwd(node["first"])


def apply_tree(session, node, target):
    if node["kind"] == "leaf":
        return [(target, node["pane"])]
    flag = "-h" if node["axis"] == "x" else "-v"
    new_pane = tmux(
        "split-window",
        flag,
        "-d",
        "-t",
        target,
        "-p",
        str(node["percent"]),
        "-c",
        leaf_cwd(node["second"]),
        "-P",
        "-F",
        "#{pane_id}",
    ).stdout.strip()
    pending = apply_tree(session, node["first"], target)
    pending.extend(apply_tree(session, node["second"], new_pane))
    return pending


def launch_command(pane_id, command):
    # One argument so tmux's shell sees the quotes. Interactive zsh loads the
    # environment agent needs, and the pane stays open after that program exits.
    script = "zsh -ic " + shlex.quote(command + "; exec zsh -i")
    tmux("respawn-pane", "-k", "-t", pane_id, script)


def live_agent_ids():
    found = set()
    _children, commands = process_table()
    for pid, command in commands.items():
        if "cursor-agent" not in command and "--resume=" not in command:
            continue
        agent_id = resume_from_command(command) or chat_id_from_open_store(pid)
        if agent_id:
            found.add(agent_id)
    return found


def restore(data, session_name, attach, dry_run, force):
    name = session_name or data["name"]
    windows = data["windows"]
    if not windows:
        raise SystemExit("session file has no windows")
    if dry_run:
        print(f"would create session {name}")
        for window in windows:
            print(f"  window {window['index']} {window['name']}")
            for pane in window["panes"]:
                command = pane.get("command") or "(shell)"
                print(f"    pane {pane['index']} {pane['kind']}: {command}")
        return

    wanted = {
        pane["agent_id"]
        for window in windows
        for pane in window["panes"]
        if pane.get("agent_id")
    }
    already = wanted & live_agent_ids()
    if already and not force:
        raise SystemExit(
            "these agents are already running, so a second copy would open the same chats:\n"
            + "\n".join(sorted(already))
            + "\nclose that tmux session first, or pass --force"
        )

    exists = tmux("has-session", "-t", name, check=False)
    if exists.returncode == 0:
        raise SystemExit(
            f"tmux session {name!r} already exists; close it or pass --session NEWNAME"
        )

    first = windows[0]
    first_cwd = first["panes"][0].get("cwd") or str(Path.home())
    tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "-n",
        first["name"],
        "-c",
        first_cwd,
    )
    # Keep the saved indexes. A missing tab, such as a closed window 4, must
    # stay missing so later numbers still match the file.
    tmux("set-option", "-t", name, "renumber-windows", "off")
    if first["index"] != 0:
        tmux("move-window", "-s", f"{name}:0", "-t", f"{name}:{first['index']}")
    pending = []
    for number, window in enumerate(windows):
        cwd = window["panes"][0].get("cwd") or str(Path.home())
        if number == 0:
            target = f"{name}:{window['index']}.0"
        else:
            target = tmux(
                "new-window",
                "-d",
                "-t",
                f"{name}:{window['index']}",
                "-n",
                window["name"],
                "-c",
                cwd,
                "-P",
                "-F",
                "#{pane_id}",
            ).stdout.strip()
        pending.extend(apply_tree(name, split_tree(window["panes"]), target))
    for pane_id, pane in pending:
        command = pane.get("command")
        if command:
            launch_command(pane_id, command)
    tmux("select-window", "-t", f"{name}:{windows[0]['index']}")
    if attach and not os.environ.get("TMUX"):
        os.execvp("tmux", ["tmux", "attach", "-t", name])
    print(f"session {name} is ready; attach with: tmux attach -t {shlex.quote(name)}")


def save(args):
    session = attached_or_default(args.session)
    data = capture(session)
    path = args.file.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"wrote {path}")


def start(args):
    path = args.file.expanduser()
    data = json.loads(path.read_text())
    restore(data, args.session, attach=not args.no_attach, dry_run=args.dry_run, force=args.force)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    file_help = (
        f"session file (env {ENV_SESSION_FILE}, "
        "default ~/.local/tmux-session.json)"
    )

    save_parser = sub.add_parser("save", help="write the session file from a live tmux session")
    save_parser.add_argument("--session", help="tmux session name; default is the attached one, else 0")
    save_parser.add_argument("--file", type=Path, default=default_session_file(), help=file_help)
    save_parser.set_defaults(func=save)

    start_parser = sub.add_parser("start", help="create a tmux session from the session file")
    start_parser.add_argument("--file", type=Path, default=default_session_file(), help=file_help)
    start_parser.add_argument("--session", help="session name to create; default is the name in the file")
    start_parser.add_argument("--no-attach", action="store_true")
    start_parser.add_argument("--dry-run", action="store_true")
    start_parser.add_argument("--force", action="store_true", help="start even if those agents are already running")
    start_parser.set_defaults(func=start)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
