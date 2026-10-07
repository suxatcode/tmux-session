"""Which foreground commands are safe to start again.

These assert the restore decision itself. A command that would mutate on
launch must stay a plain shell, even if it looks similar to an allowed one.
"""

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "tmux_session", Path(__file__).resolve().parents[1] / "tmux-session.py"
)
tmux_session = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tmux_session)

classify = tmux_session.classify_restorable


# command, kind or None when it must not be restarted
CASES = [
    ("htop", "monitor"),
    ("/opt/homebrew/bin/htop -d 10", "monitor"),
    ("btm", "monitor"),
    ("btm --basic", "monitor"),
    ("bottom", "monitor"),
    ("btop --utf-force", "monitor"),
    ("top", "monitor"),
    ("glances", "monitor"),
    ("nvtop", "monitor"),
    ("k9s", "tui"),
    ("k9s -n ns --context dev", "tui"),
    ("k9s --command pods", None),
    ("k9s -c pod", None),
    ("lazygit", "tui"),
    ("tig", "tui"),
    ("ranger", "tui"),
    ("ranger --cmd shell", None),
    ("vim main.py", "vim"),
    ("/usr/bin/vim -R notes.md", "vim"),
    ("nvim -R notes.md", "vim"),
    ("vimdiff a b", "vim"),
    ("vim -c 'wq' file", None),
    ("vim --cmd 'qa!' file", None),
    ("vi +%d file", None),
    ("notvim file", None),
    ("nano file", "editor"),
    ("hx main.rs", "editor"),
    ("emacs -nw file", "editor"),
    ("emacs --eval '(delete-file \"x\")'", None),
    ("ssh user@host", "remote"),
    ("ssh -p 22 -i ~/.ssh/id_ed25519 user@host", "remote"),
    ("ssh -p22 user@host", "remote"),
    ("ssh -tt -o StrictHostKeyChecking=accept-new user@host", "remote"),
    ("ssh -J bastion user@host", "remote"),
    ("ssh -L 127.0.0.1:8080:localhost:80 user@host", "remote"),
    ("ssh -N -L 8080:localhost:80 user@host", "remote"),
    ("ssh -4 -C -t user@host", "remote"),
    ("ssh -- user@host", "remote"),
    ("ssh -o ProxyJump=bastion user@host", "remote"),
    ("ssh -W target:22 jumphost", "remote"),
    ("ssh user@host htop", None),
    ("ssh user@host -- ls -la", None),
    ('ssh host "echo hi"', None),
    ("ssh -- user@host ls", None),
    ("ssh -o ProxyCommand=nc user@host", None),
    ("ssh -oProxyCommand=nc user@host", None),
    ("ssh -o RemoteCommand=uptime user@host", None),
    ("ssh -o LocalCommand=hostname user@host", None),
    ("ssh -G user@host", None),
    ("ssh -O stop user@host", None),
    ("ssh -s user@host sftp", None),
    ("ssh", None),
    ("autossh -M 0 user@host", "remote"),
    ("autossh -M 0 -N -L 8080:localhost:80 user@host", "remote"),
    ("mosh user@host", "remote"),
    ("mosh -p 60001 user@host", "remote"),
    ("mosh -p60001 user@host", "remote"),
    ("mosh --port=60001 user@host", "remote"),
    ("mosh --ssh='ssh -p 2222' user@host", "remote"),
    ("mosh -- user@host", "remote"),
    ("mosh user@host -- htop", None),
    ("mosh --ssh='ssh -o ProxyCommand=nc' user@host", None),
    ("mosh-client 10.0.0.1 60001 secret", None),
    ("tmux attach -t 0", "remote"),
    ("tmux a -t work", "remote"),
    ("tmux new -s work", None),
    ("tmux attach -t 0 extra", None),
    ("sudo htop", "monitor"),
    ("sudo -u root htop", "monitor"),
    ("sudo -n htop", "monitor"),
    ("sudo -u root ssh user@host", "remote"),
    ("sudo reboot", None),
    ("sudo ssh user@host reboot", None),
    ("nice -n 5 btm", "monitor"),
    ("nice -10 htop", "monitor"),
    ("nohup htop", "monitor"),
    ("tail -f /var/log/system.log", "log"),
    ("tail -n +1 -F log", "log"),
    ("tail --follow=name log", "log"),
    ("tail -n 100 file", None),
    ("journalctl -u nginx -f", "log"),
    ("journalctl -u nginx", None),
    ("stern deploy/api", "log"),
    ("kubectl logs -f deploy/api", "log"),
    ("kubectl --context dev -n ns logs -f pod/x", "log"),
    ("kubectl logs --follow deploy/api", "log"),
    ("kubectl get pods -w", "log"),
    ("kubectl get pods --watch", "log"),
    ("kubectl get pods", None),
    ("kubectl delete pod x", None),
    ("kubectl apply -f deploy.yaml", None),
    ("kubectl exec -it pod -- bash", "remote"),
    ("kubectl exec -n ns -it pod -- zsh", "remote"),
    ("kubectl exec -it pod -- bash -lc 'rm -rf /'", None),
    ("kubectl port-forward pod/api 8080:80", "remote"),
    ("docker logs -f web", "log"),
    ("docker --context prod logs -f web", "log"),
    ("docker logs web", None),
    ("docker stats", "monitor"),
    ("docker attach web", "remote"),
    ("docker exec -it web bash", "remote"),
    ("docker exec -it -u root web -- bash -l", "remote"),
    ("docker exec -it web bash -lc 'echo hi'", None),
    ("docker exec -it web rm -rf /", None),
    ("docker run --rm -it ubuntu", None),
    ("podman logs -f web", "log"),
    ("podman exec -it web sh", "remote"),
    ("ping example.com", "probe"),
    ("mtr example.com", "probe"),
    ("watch -n 2 df -h", "watch"),
    ("watch -d -n 1 ls", "watch"),
    ("watch -n1 df", "watch"),
    ("watch -n 1 ssh user@host", "watch"),
    ("watch -n 2 kubectl get pods", "watch"),
    ("watch -n 1 kubectl delete pod x", None),
    ("watch -n 1 rm -rf /tmp/x", None),
    ("watch -d ls", "watch"),
    ("python3", "repl"),
    ("python3 -i -q", "repl"),
    ("python3 script.py", None),
    ("python3 -c 'print(1)'", None),
    ("python3 -m http.server", None),
    ("node", "repl"),
    ("node --interactive", "repl"),
    ("node server.js", None),
    ("node --eval 'console.log(1)'", None),
    ("node --eval=code", None),
    ("psql -h localhost app", "client"),
    ("psql -c 'drop table t'", None),
    ("psql -cSELECT", None),
    ("mysql -h db app", "client"),
    ("mysql -e 'drop table t'", None),
    ("redis-cli -h localhost", "client"),
    ("redis-cli GET secret", None),
    ("sqlite3 app.db", "client"),
    ("sqlite3 app.db 'delete from t'", None),
    ("sqlite3 -init seed.sql app.db", None),
    ("less +F /var/log/system.log", "pager"),
    ("less file", "pager"),
    ("less +!rm", None),
    ("man htop", "pager"),
    ("man -P 'sh -c true' htop", None),
    ("make -j", None),
    ("git status", None),
    ("rm -rf /tmp/x", None),
    ("ssh host \"unterminated", None),
]


def _pane(pid="10"):
    return {
        "pid": pid,
        "index": "0",
        "left": "0",
        "top": "0",
        "width": "80",
        "height": "24",
        "cwd": "/tmp",
        "title": "pane",
    }


class RestoreDecisionTest(unittest.TestCase):
    def test_commands(self):
        for command, expected in CASES:
            with self.subTest(command=command):
                self.assertEqual(classify(command), expected)

    def test_htop_pane_keeps_the_original_command(self):
        record = tmux_session.pane_record(
            _pane(),
            {"10": ["11"]},
            {"10": "-zsh", "11": "sudo -u root htop -d 10"},
        )
        self.assertEqual(record["kind"], "monitor")
        self.assertEqual(record["command"], "sudo -u root htop -d 10")

    def test_ssh_with_a_remote_command_stays_a_shell(self):
        record = tmux_session.pane_record(
            _pane(),
            {"10": ["11"]},
            {"10": "-zsh", "11": "ssh user@host reboot"},
        )
        self.assertEqual(record["kind"], "shell")
        self.assertNotIn("command", record)

    def test_vim_and_nvim_are_restored(self):
        vim = tmux_session.pane_record(
            _pane(), {"10": ["11"]}, {"11": "vim main.py"}
        )
        nvim = tmux_session.pane_record(
            _pane(), {"10": ["11"]}, {"11": "/opt/homebrew/bin/nvim -R notes.md"}
        )
        self.assertEqual(vim["kind"], "vim")
        self.assertEqual(vim["command"], "vim main.py")
        self.assertEqual(nvim["kind"], "vim")
        self.assertEqual(nvim["command"], "/opt/homebrew/bin/nvim -R notes.md")

    def test_agent_pane_still_resumes_the_chat(self):
        record = tmux_session.pane_record(
            _pane(),
            {"10": ["11"]},
            {"11": "/Users/me/.local/bin/cursor-agent --resume=chat-123"},
        )
        self.assertEqual(record["kind"], "agent")
        self.assertEqual(record["agent_id"], "chat-123")
        self.assertEqual(record["command"], "agent --resume=chat-123")


if __name__ == "__main__":
    unittest.main()
