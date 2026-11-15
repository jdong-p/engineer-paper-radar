#!/usr/bin/env bash
# Engineer Paper Radar - one-line installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/jdong-p/engineer-paper-radar/main/install.sh | bash
#
# 1. Git and Python 3.8+: when missing, installs them with Homebrew if you have it; otherwise prints the one
#    command to run (macOS: xcode-select --install; Linux: apt / dnf).
# 2. The radar: clones it into ~/Documents/engineer-paper-radar (or `git pull` when it is already there, or when
#    this runs from inside a radar folder, e.g. Install_Radar.command).
# 3. Hands off to `install.py --setup`: two optional questions, then STARTUP.html opens.
#
# Options (environment variables):  RADAR_DIR=~/radar  clone folder;  RADAR_DRY_RUN=1  only print what would happen.
# Everything is inside main(), called on the last line, so a half-downloaded script never runs.

main() {
    REPO_URL="https://github.com/jdong-p/engineer-paper-radar.git"
    DRY="${RADAR_DRY_RUN:-}"
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"  # Homebrew's git / python3, also right after installing them
    say() { printf '  %s\n' "$*"; }
    act() {  # act "description" command args...
        local what="$1"; shift
        if [ -n "$DRY" ]; then say "[dry run] $what"; else say "$what"; "$@"; fi
    }
    die() { printf '\n  %s\n\n' "$*" >&2; exit 1; }
    os="$(uname -s)"
    has_brew() { command -v brew >/dev/null 2>&1 || [ -x /opt/homebrew/bin/brew ] || [ -x /usr/local/bin/brew ]; }
    brew_cmd() { command -v brew 2>/dev/null || { [ -x /opt/homebrew/bin/brew ] && echo /opt/homebrew/bin/brew; } || echo /usr/local/bin/brew; }

    echo
    say "Engineer Paper Radar - installer"
    [ -n "$DRY" ] && say "dry run: nothing will be installed, cloned or started"

    # 1. Git (on a Mac without the Command Line Tools, /usr/bin/git is only a stub that opens a dialog)
    git_ok() {
        command -v git >/dev/null 2>&1 || return 1
        if [ "$os" = Darwin ] && [ "$(command -v git)" = /usr/bin/git ]; then xcode-select -p >/dev/null 2>&1 || return 1; fi
        git --version >/dev/null 2>&1
    }
    if git_ok; then
        say "Git: $(git --version)"
    elif has_brew; then
        act "brew install git" "$(brew_cmd)" install git
    elif [ "$os" = Darwin ]; then
        act "xcode-select --install (a dialog opens)" xcode-select --install
        die "Finish the Command Line Tools install in the dialog, then run this installer again."
    else
        die "Git is missing. Install it (sudo apt install git  /  sudo dnf install git), then run this again."
    fi

    # 2. Python 3.8+
    find_python() {
        local c
        for c in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
            command -v "$c" >/dev/null 2>&1 || continue
            if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
                echo "$c"; return 0
            fi
        done
        return 1
    }
    PY="$(find_python)"
    if [ -n "$PY" ]; then
        say "Python 3.8+: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"
    elif has_brew; then
        act "brew install python" "$(brew_cmd)" install python
        PY="$(find_python)"
    fi
    if [ -z "$PY" ]; then
        [ -n "$DRY" ] && PY=python3
        [ -n "$PY" ] || die "Python 3.8+ is missing. Install it (macOS: brew install python, or https://www.python.org/downloads/; Linux: sudo apt install python3), then run this again."
    fi

    # 3. The radar folder
    if [ -f ./briefing.py ] && [ -f ./install.py ]; then
        DIR="$(pwd)"
    else
        DIR="${RADAR_DIR:-$HOME/Documents/engineer-paper-radar}"
    fi
    if [ -d "$DIR/.git" ]; then
        act "git pull --ff-only in $DIR" git -C "$DIR" pull --ff-only || say "git pull did not run (local changes?): continuing with this version"
    elif [ -f "$DIR/briefing.py" ]; then
        say "$DIR is not a git clone (ZIP copy): STARTUP.html offers Update Radar Now"
    elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
        die "$DIR exists and is not the radar. Run again with RADAR_DIR=<another folder>."
    else
        act "git clone $REPO_URL $DIR" git clone "$REPO_URL" "$DIR" || die "git clone failed (offline?)"
    fi

    # 4. Setup and the Control Center. Under `curl | bash` stdin is this script, so the questions read the terminal.
    if [ -n "$DRY" ]; then
        say "[dry run] cd $DIR && $PY install.py --setup"
        return 0
    fi
    cd "$DIR" || die "cannot enter $DIR"
    if [ -t 0 ] || ! { : </dev/tty; } 2>/dev/null; then
        exec "$PY" install.py --setup
    fi
    exec "$PY" install.py --setup </dev/tty
}

main "$@"
