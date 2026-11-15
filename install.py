# -*- coding: utf-8 -*-
"""Engineer Paper Radar installer and environment doctor (v3.2.1).

    python install.py --doctor     health report (green / yellow / red) with the exact fix commands
    python install.py --setup      2 questions (optional shared repo URL, optional e-mail), then the Control Center
    python install.py              the same as --setup

install.ps1 / install.sh (the one-line installers) and Install_Radar.cmd / .command (double-click) end here.
Non-interactive: --setup --repo <owner/name or URL> --email <address> --yes [--no-gui].

The whole file is plain Python 2/3 syntax (no f-strings, no annotations), so any Python can read it. Run by an
old Python, it looks for Python 3.8+ (py -3, python3, python, the usual install folders) and runs itself again
with that one, or prints the one-line fix. Nothing secret is printed: IEEE_API_KEY shows only as set / not set.
"""
from __future__ import print_function

import os
import platform
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MIN = (3, 8)
REPO_URL = "https://github.com/jdong-p/engineer-paper-radar"
WIN = os.name == "nt"
MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if WIN else 0
EMAIL_RX = re.compile(r"^[^@\s\"<>]+@[^@\s\"<>]+\.[A-Za-z]{2,}$")

# ---------- output ----------
if WIN:
    os.system("")  # turns on ANSI colours in the Windows 10/11 console
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
try:  # a UTF-8 console shows the symbols; anything else gets plain ASCII
    u"\u2714 \u26a0 \u2716".encode(getattr(sys.stdout, "encoding", None) or "ascii")
    MARK = {"ok": u"\u2714", "warn": u"\u26a0", "bad": u"\u2716", "info": u"\u2022"}
except (UnicodeEncodeError, LookupError):
    MARK = {"ok": "OK", "warn": "!!", "bad": "XX", "info": "--"}
CODES = {"ok": "32", "warn": "33", "bad": "31", "info": "36"}


def paint(level, text):
    return "\033[%sm%s\033[0m" % (CODES[level], text) if COLOR else text


def say(level, label, detail="", fix=None):
    print("  %s %-18s %s" % (paint(level, MARK[level]), label, detail))
    for f in fix or []:
        print("      " + paint("info", f))


def run(cmd, timeout=20):
    """(returncode, output) of a command; (None, '') when it can't start."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
                             creationflags=NO_WINDOW)
        out = p.communicate(b"")[0]
        return p.returncode, out.decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return None, ""


def which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for ext in ((os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(";") if WIN else []) + [""]):
            p = os.path.join(d.strip('"'), name + ext)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


# ---------- Python 3.8+ ----------
def py_candidates():
    """Commands that may start a Python, best first (lists, so a path with spaces stays one argument)."""
    out = []
    if WIN:
        out += [["py", "-3"], ["python"], ["python3"]]
        roots = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
                 os.environ.get("ProgramFiles", r"C:\Program Files")]
        for r in roots:
            try:
                names = sorted(os.listdir(r), reverse=True)
            except OSError:
                continue
            out += [[os.path.join(r, n, "python.exe")] for n in names
                    if n.lower().startswith("python3") and os.path.isfile(os.path.join(r, n, "python.exe"))]
    else:
        out += [["python3"], ["python"]]
        out += [[p] for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")
                if os.path.isfile(p)]
        try:
            fw = "/Library/Frameworks/Python.framework/Versions"
            out += [[os.path.join(fw, v, "bin", "python3")] for v in sorted(os.listdir(fw), reverse=True)
                    if os.path.isfile(os.path.join(fw, v, "bin", "python3"))]
        except OSError:
            pass
    return out


def py_version(cmd):
    """(major, minor, micro) of that Python, or None (missing, or only the Microsoft Store stub)."""
    rc, out = run(cmd + ["-c", "import sys; sys.stdout.write('%d.%d.%d' % sys.version_info[:3])"], 15)
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", out or "")
    return tuple(int(x) for x in m.groups()) if rc == 0 and m else None


def find_python():
    """(command, version) of the first Python 3.8+, and the list of (command, version) seen."""
    seen = []
    for c in py_candidates():
        v = py_version(c)
        if v:
            seen.append((c, v))
            if v >= MIN:
                return c, v, seen
    return None, None, seen


def py_fix():
    if WIN:
        return ["winget install --id Python.Python.3.12 -e", "or https://www.python.org/downloads/ (tick \"Add python.exe to PATH\")"]
    if MAC:
        return ["brew install python", "or https://www.python.org/downloads/"]
    return ["sudo apt install python3   (Fedora: sudo dnf install python3)"]


def git_fix():
    if WIN:
        return ["winget install --id Git.Git -e"]
    if MAC:
        return ["xcode-select --install   (or: brew install git)"]
    return ["sudo apt install git   (Fedora: sudo dnf install git)"]


def ensure_new_python():
    """Run this file again with Python 3.8+ when the current one is older; exit with the fix if there is none."""
    if sys.version_info[:2] >= MIN:
        return
    cmd, v, seen = find_python()
    if cmd:
        print("Python %s is too old here; using Python %s (%s)." % (platform.python_version(), ".".join(map(str, v)), " ".join(cmd)))
        sys.stdout.flush()
        rc = subprocess.call(cmd + [os.path.abspath(__file__)] + sys.argv[1:])
        sys.exit(rc)
    print("\n  Engineer Paper Radar needs Python 3.8 or newer. Found: Python %s" % platform.python_version())
    print("\n  Install a current Python with one line, then run this again:\n")
    for f in py_fix():
        print("      " + f)
    print("")
    sys.exit(1)


# ---------- --doctor ----------
def doctor():
    """Print the health report. Returns the number of red items (0 = ready)."""
    red = [0]

    def bad(*a, **k):
        red[0] += 1
        say("bad", *a, **k)

    print("\n" + paint("info", "Engineer Paper Radar - environment doctor") + "  (%s)\n" % HERE)
    osname = "Windows %s" % platform.release() if WIN else ("macOS %s" % platform.mac_ver()[0] if MAC else platform.platform())
    say("ok", "Operating system", osname + " (" + platform.machine() + ")")

    cmd, v, seen = find_python()
    here_v = ".".join(map(str, sys.version_info[:3]))
    if cmd:
        say("ok", "Python 3.8+", "%s  (%s)" % (".".join(map(str, v)), " ".join(cmd)))
    else:
        found = ", ".join("%s %s" % (" ".join(c), ".".join(map(str, x))) for c, x in seen) or "none"
        bad("Python 3.8+", "not found (seen: %s; running %s)" % (found, here_v), py_fix())

    g = which("git")
    rc, out = run(["git", "--version"]) if g else (None, "")
    if rc == 0:
        say("ok", "Git", out.replace("git version ", ""))
    else:
        bad("Git", "not found: needed to clone, update and share", git_fix())

    if os.path.isfile(os.path.join(HERE, "briefing.py")):
        if os.path.isdir(os.path.join(HERE, ".git")):
            rc, url = run(["git", "-C", HERE, "remote", "get-url", "origin"])
            say("ok", "Radar folder", "git clone" + (" of " + url if rc == 0 and url else ""))
        else:
            say("warn", "Radar folder", "not a git clone (ZIP copy?): it can't update itself",
                ["STARTUP.html: Update Radar Now turns it into a clone, or: git clone %s.git" % REPO_URL])
    else:
        bad("Radar folder", "briefing.py is not next to install.py", ["git clone %s.git" % REPO_URL])

    if os.path.isfile(os.path.join(HERE, "config.json")):
        say("ok", "config.json", "present")
    else:
        say("info", "config.json", "created from config.example.json on the first run")

    c = which((os.environ.get("CLAUDE_COMMAND") or "claude"))
    if c:
        say("ok", "Claude CLI", "found (" + c + "): AI digests and the weekly synthesis")
    else:
        say("warn", "Claude CLI", "Optional - 0-Token free mode works out of the box",
            ["npm install -g @anthropic-ai/claude-code   (only for AI digests)"])
    if os.environ.get("IEEE_API_KEY", "").strip():
        say("ok", "IEEE_API_KEY", "set")
    else:
        say("warn", "IEEE_API_KEY", "Optional - 0-Token free mode works out of the box (Crossref / OpenAlex)",
            ["free key: https://developer.ieee.org (README: Free IEEE API key)"])

    net = net_check()
    if net is True:
        say("ok", "Network", "api.crossref.org and github.com reachable")
    elif net is None:
        say("warn", "Network", "not checked (needs Python 3.8+)")
    else:
        say("warn", "Network", "not reachable: " + net, ["check your internet connection or proxy"])

    print("")
    if red[0]:
        print("  " + paint("bad", "%d problem(s): run the fix commands above, then: python install.py --doctor" % red[0]))
    else:
        print("  " + paint("ok", "Ready.") + " Start it with:  " + (" ".join(cmd) if cmd else "python") + " install.py --setup")
    print("")
    return red[0]


def net_check():
    if sys.version_info[:2] < MIN:
        return None
    import socket
    down = []
    for host in ("api.crossref.org", "github.com"):
        try:
            socket.create_connection((host, 443), timeout=4).close()
        except (OSError, socket.error):
            down.append(host)
    return True if not down else ", ".join(down)


# ---------- --setup ----------
def ask(question, default=""):
    try:
        reader = raw_input  # noqa: F821  (Python 2)
    except NameError:
        reader = input
    try:
        v = reader("  %s%s: " % (question, " [%s]" % default if default else "")).strip()
    except EOFError:
        v = ""
    return v or default


def setup(repo=None, email=None, yes=False, gui=True):
    sys.path.insert(0, HERE)
    os.chdir(HERE)
    import config_file
    import control_center
    import data_repo
    config_file.ensure()
    cfg = control_center.load_cfg()
    dr = cfg.get("data_repo") or {}
    print("\n" + paint("info", "Engineer Paper Radar - setup") + "  (Enter skips a question; everything can be changed later in STARTUP.html)\n")
    if repo is None and not yes:
        print("  1. Did a team invite you to a private shared paper repo? Paste its URL or owner/name (optional).")
        repo = ask("   Shared repo", dr.get("remote_url") or "")
    if email is None and not yes:
        print("  2. Your e-mail for Crossref / OpenAlex (faster answers; sent only to them). Optional, recommended.")
        email = ask("   Contact e-mail", cfg.get("contact_email") or "")
    changes = []
    repo, email = (repo or "").strip(), (email or "").strip()
    if repo and repo != (dr.get("remote_url") or ""):
        try:
            data_repo.repo_url(repo)
        except ValueError as e:
            print("  " + paint("warn", "Shared repo skipped: %s" % e))
        else:
            changes.append((["data_repo", "remote_url"], repo))
    if email and email != (cfg.get("contact_email") or ""):
        if EMAIL_RX.match(email):
            changes.append((["contact_email"], email))
        else:
            print("  " + paint("warn", "E-mail skipped: %r is not an e-mail address" % email))
    if changes:
        done = control_center.write_config(changes, cfg)
        print("  " + paint("ok", "config.json: " + ", ".join(done)))
    cfg = control_center.load_cfg()
    if control_center.clone_needed(cfg):
        print("  Cloning your shared repo next to the radar folder (Git may ask you to sign in to GitHub once) ...")
        sys.stdout.flush()
        rc = subprocess.call([sys.executable, os.path.join(HERE, "briefing.py"), "--clone-data-repo"])
        if rc:
            print("  " + paint("warn", "Not cloned now: STARTUP.html Step 2 retries it (Read / Pull, Full Sync)."))
    if not gui:
        print("  Done. Start the Control Center with:  python briefing.py --gui")
        return 0
    print("\n  Opening the Control Center (STARTUP.html). Keep this window open while you use it; Ctrl+C stops it.\n")
    sys.stdout.flush()
    return subprocess.call([sys.executable, os.path.join(HERE, "briefing.py"), "--gui"])


def main(argv):
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if "--doctor" in argv:
        return 1 if doctor() else 0
    ensure_new_python()

    def opt(name):
        if name in argv:
            i = argv.index(name)
            return argv[i + 1] if i + 1 < len(argv) else ""
        return None
    if doctor():
        print("  Fix the red items first (see above), then run this again.")
        return 1
    return setup(repo=opt("--repo"), email=opt("--email"), yes="--yes" in argv, gui="--no-gui" not in argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("")
        sys.exit(130)
