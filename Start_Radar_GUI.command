#!/bin/sh
# Engineer Paper Radar: double-click (macOS) to open the Setup & Control Center (STARTUP.html) with working buttons.
# It runs "python3 briefing.py --gui": a small server on http://127.0.0.1 (this computer only) that opens
# your browser. Keep this window open while you use the page; close it (or Ctrl+C) to stop.
# It first looks for Python 3.8 or newer (python3, then python) and explains the fix if there is none.
cd "$(dirname "$0")" || exit 1
PY=""
FOUND=""
for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    # runs under any Python (2 or 3); empty when the command is only a stub (e.g. no Command Line Tools yet)
    v=$("$c" -c 'import sys; sys.stdout.write(sys.version.split()[0])' 2>/dev/null) || continue
    [ -n "$v" ] || continue
    [ -n "$FOUND" ] || FOUND="Python $v ($(command -v "$c"))"
    if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
if [ -n "$PY" ]; then
    exec "$PY" briefing.py --gui
fi
[ -n "$FOUND" ] || FOUND="no Python found / 未找到 Python"
cat <<EOF

  Engineer Paper Radar needs Python 3.8 or newer. Found: $FOUND
  Engineer Paper Radar 需要 Python 3.8 或更新版本。当前：$FOUND

  Install a current Python with one line / 一行命令安装新版 Python：

      brew install python

  (No Homebrew? Get it from https://www.python.org/downloads/ instead.)
  （没有 Homebrew？也可以从 python.org 下载安装。）
  Then double-click this file again. / 然后重新双击本文件。

EOF
printf '  Press Return to close. / 按回车键关闭。 '
read -r _
exit 1
