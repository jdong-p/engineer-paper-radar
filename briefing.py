# -*- coding: utf-8 -*-
"""Engineer Paper Radar: the command-line entry point, `python briefing.py [options]`. The options are in briefing_main.py.

This file only checks the Python version, and it is written so that every Python can read it, Python 2
included. An old Python then prints a clear message instead of a SyntaxError, which is all a file with
f-strings would get. It is the one module without `from __future__ import annotations`: that line is itself
a SyntaxError before Python 3.7. The radar itself is in briefing_main.py (Python 3.8+).
"""
from __future__ import print_function, unicode_literals

import sys

MIN_PYTHON = (3, 8)
TOO_OLD = 3  # exit code: this Python is older than MIN_PYTHON


def _say(line):
    try:
        sys.stderr.write(line + "\n")
    except Exception:  # a console that can't show Chinese (e.g. Python 2 on an English Windows)
        try:
            sys.stderr.write(line.encode("ascii", "replace").decode("ascii") + "\n")
        except Exception:
            pass


def _too_old():
    found = "Python " + ".".join(str(x) for x in sys.version_info[:3])
    try:
        found += " (%s)" % (sys.executable or "?")
    except Exception:  # Python 2: a non-ASCII path in a byte string
        pass
    for line in [
        "",
        "Engineer Paper Radar needs Python 3.8 or newer. Found: " + found,
        "Engineer Paper Radar 需要 Python 3.8 或更新版本。当前：" + found,
        "",
        "Install a current Python with one line / 一行命令安装新版 Python：",
        "    Windows:  winget install Python.Python.3.12",
        "    macOS:    brew install python",
        "    (or / 或  https://www.python.org/downloads/ )",
        "",
        "Then open a new terminal and run  py -3 briefing.py  (Windows) or  python3 briefing.py  (macOS / Linux).",
        "然后打开一个新终端，运行  py -3 briefing.py（Windows）或  python3 briefing.py（macOS / Linux）。",
        "",
    ]:
        _say(line)


if sys.version_info < MIN_PYTHON:
    _too_old()
    sys.exit(TOO_OLD)

import briefing_main  # noqa: E402  (Python 3.8+ from here on)

if __name__ == "__main__":
    sys.exit(briefing_main.main())
