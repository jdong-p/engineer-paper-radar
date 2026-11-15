#!/bin/sh
# Engineer Paper Radar: double-click (macOS) to install what is missing (Git, Python 3.8+), update this folder
# (git pull) and open the setup (install.py --setup), which ends in STARTUP.html.
# The same as the one-line installer:  curl -fsSL https://raw.githubusercontent.com/jdong-p/engineer-paper-radar/main/install.sh | bash
cd "$(dirname "$0")" || exit 1
bash ./install.sh
rc=$?
if [ "$rc" -ne 0 ]; then
    printf '\n  The installer stopped with an error (see above). Press Return to close. '
    read -r _
fi
exit "$rc"
