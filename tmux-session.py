#!/usr/bin/env python3
"""Save and restore a tmux session.

The layout string tmux prints cannot be replayed: the numbers in it are pane
ids from the live session. Geometry is stored instead, and restore splits
along the same seams. Each agent pane records the chat id so `agent --resume`
opens that same conversation. Other foreground programs are restored only when
starting them again does not itself change local or remote state.
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


class _Unsafe(Exception):
    """This argv is not known to be safe to start again."""


# Viewers with no launch-time payload. Keystrokes can still change things
# later, which is the same tradeoff as restoring vim.
_MONITORS = {
    "htop", "top", "atop", "btop", "btm", "bottom",
    "bashtop", "bpytop", "gotop", "ytop", "zenith", "gtop", "vtop",
    "glances", "nvitop", "nvtop", "radeontop", "intel_gpu_top", "s-tui",
    "iftop", "iotop", "iotop-c", "nethogs", "bandwhich", "bmon", "nload",
}
_PASSIVE_TUIS = {"tig", "lazygit", "lazydocker", "ctop"}
_VIM = {"vi", "vim", "nvim", "gvim", "view", "nview", "vimdiff", "nvimdiff"}
_SIMPLE_EDITORS = {"nano", "hx", "helix", "micro"}
_SHELLS = {"bash", "sh", "zsh", "fish", "ash", "dash", "ksh"}
_SHELL_FLAGS = {"-l", "--login", "-i", "--interactive"}

# OpenSSH options. A remote command, a subsystem, or a *Command option runs
# something on launch, so those are rejected. Port forwards and a plain login
# only reconnect.
# Both -I (PKCS11) and -i (identity file) take an argument.
_SSH_WITH_ARG = set("BbcDEeFIiJLlmOoPpRSWw")
_SSH_BARE = set("46AaCfGgKkMNnqTtVvXxYy")
_SSH_REJECT = set("GOQVs")

_KUBECTL_WITH_ARG = {
    "--kubeconfig", "--context", "--cluster", "--user", "--namespace", "-n",
    "--server", "-s", "--token", "--client-certificate", "--client-key",
    "--certificate-authority", "--as", "--as-group", "--as-uid",
    "--request-timeout", "--cache-dir", "--profile", "--profile-output",
    "--v", "-v", "--vmodule", "--log-file", "--log-dir", "--tls-server-name",
}
_KUBECTL_BARE = {
    "--insecure-skip-tls-verify", "--disable-compression", "--warnings-as-errors",
}
_DOCKER_WITH_ARG = {
    "-H", "--host", "-c", "--context", "-l", "--log-level", "--config",
    "--tlscacert", "--tlscert", "--tlskey",
}
_DOCKER_BARE = {"-D", "--debug", "--tls", "--tlsverify"}
_PODMAN_WITH_ARG = _DOCKER_WITH_ARG | {
    "--url", "--connection", "-c", "--connection", "--storage-opt",
}
_EXEC_WITH_ARG = {
    "-e", "--env", "-u", "--user", "-w", "--workdir", "--detach-keys",
    "--env-file", "-c", "--container", "-n", "--namespace",
    "--pod-running-timeout",
}
_EXEC_BARE = {
    "-i", "-t", "-d", "--interactive", "--tty", "--detach", "--privileged",
    "--stdin", "--quiet", "-q",
}


def _argv(command):
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    return tokens or None


def _name(token):
    return Path(token).name


def classify_restorable(command):
    """Kind of a foreground command that is safe to start again, or None.

    Safe means the launch itself does not mutate anything. A login `ssh host`
    qualifies; `ssh host reboot` does not.
    """
    tokens = _argv(command)
    if not tokens:
        return None
    try:
        return _classify(tokens)
    except _Unsafe:
        return None


def _classify(tokens):
    tokens = _unwrap(tokens)
    if not tokens:
        raise _Unsafe()
    name = _name(tokens[0])
    # mosh execs into mosh-client. That argv is a live session key, not the
    # command the user typed, and replaying it would store the key.
    if name in {"mosh-client", "mosh-server"}:
        raise _Unsafe()
    if name in _MONITORS:
        return "monitor"
    if name == "k9s":
        # --command can start a plugin, which may run a shell.
        _reject_named(tokens, {"-c", "--command"})
        return "tui"
    if name in _PASSIVE_TUIS:
        return "tui"
    if name in _VIM:
        _reject_vim_payload(tokens)
        return "vim"
    if name in _SIMPLE_EDITORS:
        return "editor"
    if name == "emacs" or name == "emacsclient":
        _reject_emacs_payload(tokens)
        return "editor"
    if name == "kak":
        _reject_named(tokens, {"-e", "-E"})
        return "editor"
    if name == "ranger":
        _reject_named(tokens, {"-c", "--cmd"})
        return "tui"
    if name == "yazi":
        _reject_named(tokens, {"-c", "--cmd", "--chooser-file"})
        return "tui"
    if name in {"ssh", "autossh"}:
        _ssh(tokens, name)
        return "remote"
    if name == "mosh":
        _mosh(tokens)
        return "remote"
    if name == "tmux":
        _tmux_attach(tokens)
        return "remote"
    if name == "tail":
        _require_flag(tokens[1:], {"-f", "-F", "--follow"})
        return "log"
    if name == "journalctl":
        _require_flag(tokens[1:], {"-f", "--follow"})
        return "log"
    if name == "stern":
        return "log"
    if name == "lnav":
        _reject_named(tokens, {"-c", "--command"})
        return "log"
    if name == "multitail":
        _reject_named(tokens, {"-l", "-L", "-R"})
        return "log"
    if name in {"less", "more"}:
        _less(tokens)
        return "pager"
    if name == "man":
        _reject_named(tokens, {"-P", "--pager"})
        return "pager"
    if name in {"ping", "ping6", "mtr", "traceroute", "tracepath"}:
        return "probe"
    if name in {"kubectl", "kubectl.exe"}:
        return _kubectl(tokens)
    if name in {"docker", "podman"}:
        return _docker(tokens)
    if name == "watch":
        _watch(tokens)
        return "watch"
    if _is_python(name) or name == "node" or name in {"irb", "pry"}:
        _repl(tokens, name)
        return "repl"
    if name in {"psql", "pgcli", "mysql", "mariadb", "mycli", "redis-cli", "sqlite3"}:
        _db_client(tokens, name)
        return "client"
    raise _Unsafe()


def _unwrap(tokens):
    """Drop harmless launch wrappers so `sudo htop` is judged as htop.

    The stored command stays the original string, wrappers included.
    """
    while tokens and _name(tokens[0]) in {"sudo", "doas", "nice", "nohup", "time"}:
        tokens = _wrapper_inner(tokens)
    return tokens


def _wrapper_inner(tokens):
    name = _name(tokens[0])
    if name == "sudo":
        return _after_options(
            tokens,
            1,
            with_arg={
                "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
                "-C", "--close-from", "-D", "--chdir", "-R", "--chroot",
                "-T", "--command-timeout", "-U", "--other-user", "-r", "--role",
                "-t", "--type",
            },
            bare={
                "-A", "--askpass", "-E", "--preserve-env", "-H", "--set-home",
                "-i", "--login", "-n", "--non-interactive", "-S", "--stdin",
                "-s", "--shell", "-b", "--background", "-k", "--reset-timestamp",
                "-K", "--remove-timestamp", "-v", "--validate",
            },
        )
    if name == "doas":
        return _after_options(
            tokens, 1, with_arg={"-u", "-C", "-a"}, bare={"-n", "-s", "-L"}
        )
    if name == "nice":
        index = 1
        if index < len(tokens) and (
            tokens[index] == "-n" or tokens[index] == "--adjustment"
        ):
            index += 2
        elif index < len(tokens) and tokens[index].startswith("-") and tokens[index][1:].isdigit():
            index += 1
        if index > len(tokens):
            raise _Unsafe()
        return tokens[index:]
    if name == "nohup":
        return tokens[1:]
    if name == "time":
        _reject_named(tokens, {"-o", "--output", "-a", "--append"})
        return _after_options(tokens, 1, with_arg={"-f", "--format"}, bare={"-p", "--portable"})
    raise _Unsafe()


def _after_options(tokens, start, with_arg, bare):
    index = _consume_options(tokens, start, with_arg=with_arg, bare=bare)
    if index < len(tokens) and tokens[index] == "--":
        index += 1
    return tokens[index:]


def _consume_options(tokens, index, with_arg, bare):
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index
        if not token.startswith("-") or token == "-":
            return index
        index = _consume_one_option(tokens, index, with_arg, bare)
    return index


def _consume_one_option(tokens, index, with_arg, bare):
    token = tokens[index]
    if token.startswith("--"):
        name, sep, _value = token.partition("=")
        if name not in with_arg and name not in bare:
            raise _Unsafe()
        if name in with_arg and not sep:
            if index + 1 >= len(tokens):
                raise _Unsafe()
            return index + 2
        return index + 1
    cluster = token[1:]
    pos = 0
    while pos < len(cluster):
        flag = "-" + cluster[pos]
        if flag not in with_arg and flag not in bare:
            raise _Unsafe()
        if flag in with_arg:
            rest = cluster[pos + 1:]
            if rest:
                return index + 1
            if index + 1 >= len(tokens):
                raise _Unsafe()
            return index + 2
        pos += 1
    return index + 1


def _reject_named(tokens, names):
    for token in tokens[1:]:
        base = token.split("=", 1)[0]
        if base in names or token in names:
            raise _Unsafe()


def _reject_payload_flags(tokens, names):
    """Reject a flag even when its argument is glued on (`-cSELECT`)."""
    shorts = {name for name in names if len(name) == 2 and name.startswith("-")}
    for token in tokens[1:]:
        base = token.split("=", 1)[0]
        if base in names or token in names:
            raise _Unsafe()
        if token.startswith("--"):
            continue
        for short in shorts:
            if token.startswith(short):
                raise _Unsafe()


def _require_flag(tokens, names):
    for token in tokens:
        base = token.split("=", 1)[0]
        if token in names or base in names:
            return
    raise _Unsafe()


def _reject_vim_payload(tokens):
    # `+cmd`, `-c`, and script/ex modes run commands as the file opens.
    for token in tokens[1:]:
        if token.startswith("+") or token.startswith("--cmd") or token.startswith("-c"):
            raise _Unsafe()
        if token in {"-s", "-S", "-w", "-W", "-e", "-E", "-es", "-Es"}:
            raise _Unsafe()


def _reject_emacs_payload(tokens):
    _reject_named(
        tokens,
        {
            "--eval", "--execute", "-e", "--funcall", "-f", "--batch",
            "--script", "-l", "--load", "--insert",
        },
    )


def _ssh_option_safe(value):
    # LocalCommand / ProxyCommand / RemoteCommand run during connect.
    key = value.split("=", 1)[0].strip().lower()
    return "command" not in key


def _ssh(tokens, name):
    if name == "autossh":
        tokens = ["ssh", *_autossh_ssh_args(tokens)]
    if _name(tokens[0]) != "ssh":
        raise _Unsafe()
    index = 1
    saw_destination = False
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            saw_destination = True
            index += 1
            break
        if token.startswith("--"):
            raise _Unsafe()
        index = _ssh_short(tokens, index)
    if not saw_destination:
        if index < len(tokens) and not tokens[index].startswith("-"):
            saw_destination = True
            index += 1
    if not saw_destination or index != len(tokens):
        raise _Unsafe()


def _ssh_short(tokens, index):
    cluster = tokens[index][1:]
    pos = 0
    while pos < len(cluster):
        opt = cluster[pos]
        if opt in _SSH_REJECT:
            raise _Unsafe()
        if opt in _SSH_WITH_ARG:
            rest = cluster[pos + 1:]
            if rest:
                arg = rest
                index += 1
            else:
                index += 1
                if index >= len(tokens):
                    raise _Unsafe()
                arg = tokens[index]
                index += 1
            if opt == "o" and not _ssh_option_safe(arg):
                raise _Unsafe()
            return index
        if opt in _SSH_BARE:
            pos += 1
            continue
        raise _Unsafe()
    return index + 1


def _autossh_ssh_args(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-M":
            index += 2
            continue
        if token.startswith("-M") and token != "-M":
            index += 1
            continue
        if token in {"-f", "-V"}:
            index += 1
            continue
        break
    if index > len(tokens):
        raise _Unsafe()
    return tokens[index:]


def _mosh(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            break
        name, sep, value = token.partition("=")
        if name in {"--client", "--server", "--predict", "--port", "--family", "--bind-server", "--experimental-remote-ip", "-p"} or (
            name == "--ssh"
        ):
            if name == "--ssh":
                ssh_value = value if sep else tokens[index + 1] if index + 1 < len(tokens) else ""
                if not sep:
                    index += 1
                _mosh_ssh_hook(ssh_value)
            elif not sep:
                index += 1
                if index >= len(tokens):
                    raise _Unsafe()
            index += 1
            continue
        if name in {"-a", "-4", "-6", "--no-init", "--local", "-n"}:
            index += 1
            continue
        # -p60001 keeps the port attached to the flag.
        if token.startswith("-p") and not token.startswith("--"):
            index += 1
            continue
        raise _Unsafe()
    if index >= len(tokens):
        raise _Unsafe()
    index += 1  # host
    if index < len(tokens) and tokens[index] == "--":
        index += 1
    if index != len(tokens):
        raise _Unsafe()


def _mosh_ssh_hook(value):
    parts = _argv(value)
    if not parts or _name(parts[0]) not in {"ssh", "autossh"}:
        raise _Unsafe()
    # mosh appends the destination itself. Any positional here is unexpected.
    saved = parts
    if _name(saved[0]) == "autossh":
        saved = ["ssh", *_autossh_ssh_args(saved)]
    index = 1
    while index < len(saved):
        token = saved[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-") or token == "-":
            raise _Unsafe()
        index = _ssh_short(saved, index)
    if index != len(saved):
        raise _Unsafe()


def _tmux_attach(tokens):
    if len(tokens) < 2 or tokens[1] not in {"attach", "attach-session", "a"}:
        raise _Unsafe()
    # Anything left after attach flags would be a tmux command to run.
    rest = _after_options(tokens, 2, with_arg={"-t", "-f", "-L", "-S"}, bare={"-d", "-r", "-E"})
    if rest:
        raise _Unsafe()


def _less(tokens):
    for token in tokens[1:]:
        if token.startswith("+") and token not in {"+F", "++F"}:
            raise _Unsafe()


def _kubectl(tokens):
    index = _consume_options(tokens, 1, with_arg=_KUBECTL_WITH_ARG, bare=_KUBECTL_BARE)
    if index >= len(tokens) or tokens[index].startswith("-"):
        raise _Unsafe()
    sub = tokens[index]
    rest = tokens[index + 1:]
    if sub == "logs":
        _require_flag(rest, {"-f", "--follow"})
        return "log"
    if sub == "get":
        _require_flag(rest, {"-w", "--watch"})
        return "log"
    if sub == "port-forward":
        return "remote"
    if sub == "exec":
        _bare_shell_after(_positionals(rest, with_arg=_EXEC_WITH_ARG, bare=_EXEC_BARE), skip=1)
        return "remote"
    raise _Unsafe()


def _docker(tokens):
    name = _name(tokens[0])
    with_arg = _PODMAN_WITH_ARG if name == "podman" else _DOCKER_WITH_ARG
    bare = _DOCKER_BARE | ({"--remote"} if name == "podman" else set())
    index = _consume_options(tokens, 1, with_arg=with_arg, bare=bare)
    if index >= len(tokens):
        raise _Unsafe()
    sub = tokens[index]
    rest = tokens[index + 1:]
    if sub == "logs":
        _require_flag(rest, {"-f", "--follow"})
        return "log"
    if sub == "stats":
        return "monitor"
    if sub == "events":
        return "log"
    if sub == "attach":
        return "remote"
    if sub == "exec":
        _bare_shell_after(_positionals(rest, with_arg=_EXEC_WITH_ARG, bare=_EXEC_BARE), skip=1)
        return "remote"
    raise _Unsafe()


def _positionals(tokens, with_arg, bare):
    found = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            found.extend(tokens[index + 1:])
            break
        if token.startswith("-") and token != "-":
            index = _consume_one_option(tokens, index, with_arg, bare)
            continue
        found.append(token)
        index += 1
    return found


def _bare_shell_after(positionals, skip):
    if len(positionals) <= skip:
        raise _Unsafe()
    command = positionals[skip:]
    if _name(command[0]) not in _SHELLS:
        raise _Unsafe()
    for token in command[1:]:
        if token not in _SHELL_FLAGS:
            raise _Unsafe()


def _watch(tokens):
    rest = _after_options(
        tokens,
        1,
        with_arg={"-n", "--interval"},
        # -d/--differences take an optional =value. Treating them as bare
        # keeps `watch -d df` from swallowing df as the flag argument.
        bare={
            "-d", "--differences", "-t", "--no-title", "-b", "--beep",
            "-p", "--precise", "-c", "--color", "-x", "--exec",
            "-e", "--errexit", "-g", "--chgexit", "-w", "--equexit",
        },
    )
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        raise _Unsafe()
    try:
        _classify(rest)
        return
    except _Unsafe:
        pass
    if not _oneshot_readonly(rest):
        raise _Unsafe()


def _oneshot_readonly(tokens):
    tokens = _unwrap(tokens)
    if not tokens:
        return False
    name = _name(tokens[0])
    if name in {"df", "free", "uptime", "w", "who", "users", "date", "sensors", "nvidia-smi", "vmstat", "iostat", "ss", "netstat", "ls", "cat", "ps", "du", "hostname", "uname", "id", "groups", "nproc", "lscpu", "lsblk", "sw_vers", "pwd"}:
        return True
    if name == "dmesg":
        try:
            _reject_named(tokens, {"-c", "-C", "--clear", "--read-clear"})
        except _Unsafe:
            return False
        return True
    if name == "sysctl":
        if any("=" in token or token in {"-w", "--write"} for token in tokens[1:]):
            return False
        return True
    if name in {"kubectl", "kubectl.exe"}:
        try:
            index = _consume_options(tokens, 1, with_arg=_KUBECTL_WITH_ARG, bare=_KUBECTL_BARE)
        except _Unsafe:
            return False
        return index < len(tokens) and tokens[index] in {"get", "describe", "api-resources", "version", "cluster-info"}
    if name in {"docker", "podman"}:
        try:
            with_arg = _PODMAN_WITH_ARG if name == "podman" else _DOCKER_WITH_ARG
            index = _consume_options(tokens, 1, with_arg=with_arg, bare=_DOCKER_BARE)
        except _Unsafe:
            return False
        return index < len(tokens) and tokens[index] in {"ps", "images", "info", "version", "inspect"}
    return False


def _is_python(name):
    return name == "python" or name.startswith("python3") or name in {"ipython", "bpython", "ptpython"}


def _repl(tokens, name):
    if _is_python(name):
        _python_repl(tokens)
        return
    if name == "node":
        _node_repl(tokens)
        return
    for token in tokens[1:]:
        base = token.split("=", 1)[0]
        if base in {"-e", "--eval"} or not token.startswith("-"):
            raise _Unsafe()


def _python_repl(tokens):
    index = 1
    while index < len(tokens):
        token = tokens[index]
        base = token.split("=", 1)[0]
        if base in {"-c", "--command", "-m", "--module"} or token.startswith("-c"):
            raise _Unsafe()
        if token.startswith("-m") and not token.startswith("--"):
            raise _Unsafe()
        if token in {"-X", "-W", "--check-hash-based-pycs"}:
            index += 2
            continue
        if token.startswith("-") and token != "-":
            index += 1
            continue
        raise _Unsafe()
    return


def _node_repl(tokens):
    for token in tokens[1:]:
        base = token.split("=", 1)[0]
        if base in {"-e", "--eval", "-p", "--print"} or not token.startswith("-"):
            raise _Unsafe()


def _db_client(tokens, name):
    if name in {"psql", "pgcli"}:
        _reject_payload_flags(tokens, {"-c", "--command", "-f", "--file"})
        return
    if name in {"mysql", "mariadb", "mycli"}:
        _reject_payload_flags(tokens, {"-e", "--execute"})
        return
    if name == "redis-cli":
        rest = _after_options(
            tokens,
            1,
            with_arg={"-h", "--host", "-p", "--port", "-a", "--pass", "-n", "--db", "-u", "--user"},
            bare={"--raw", "--tls", "--stat", "--ldb", "--pipe", "--csv", "--json", "-x"},
        )
        if rest and rest[0] == "--":
            rest = rest[1:]
        if rest:
            raise _Unsafe()
        return
    if name == "sqlite3":
        # -init runs SQL from a file before the prompt.
        _reject_named(tokens, {"-cmd", "--cmd", "-init", "--init"})
        positionals = [token for token in tokens[1:] if not token.startswith("-")]
        if len(positionals) > 1:
            raise _Unsafe()


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
    kind = classify_restorable(command) if command else None
    if kind:
        record["kind"] = kind
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
