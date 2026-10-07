#!/bin/sh
# Install tmux-session into ~/.local/bin.
# From a clone this copies the files beside this script. Piped from curl, it
# downloads them from the same GitHub revision the installer came from.
set -eu

dest="${TMUX_SESSION_INSTALL_DIR:-$HOME/.local/bin}"
base_url="${TMUX_SESSION_BASE_URL:-https://raw.githubusercontent.com/suxatcode/tmux-session/main}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "tmux-session needs python3 on PATH" >&2
  exit 1
fi

mkdir -p "$dest"

script_dir=""
if [ -f "$0" ]; then
  script_dir=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
fi

install_one() {
  name=$1
  tmp=$(mktemp)
  if [ -n "$script_dir" ] && [ -f "$script_dir/$name" ]; then
    cp "$script_dir/$name" "$tmp"
  else
    curl -fsSL "$base_url/$name" -o "$tmp"
  fi
  chmod 755 "$tmp"
  mv "$tmp" "$dest/$name"
}

for name in tmux-session.py tmux-session-save tmux-session-start; do
  install_one "$name"
done

echo "Installed tmux-session-save and tmux-session-start into $dest"
echo "Session file: \${TMUX_SESSION_FILE:-\$HOME/.local/tmux-session.json}"

case ":$PATH:" in
  *":$dest:"*) ;;
  *) echo "Add this to your shell config so the commands are found:"
     echo "  export PATH=\"$dest:\$PATH\"" ;;
esac

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not on PATH yet; install it before saving or starting a session" >&2
fi
