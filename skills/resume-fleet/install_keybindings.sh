#!/bin/bash
# resume-fleet v0.1 — idempotently add the 3 F-key terminal keybindings the fleet
# cycler needs, to the editor's keybindings.json. Safe to run repeatedly.
#   EDITOR_DIR: "Code" (default) | "Cursor" | "Code - Insiders" | "VSCodium"
set -euo pipefail
EDITOR_DIR="${EDITOR_DIR:-Code}"
KB="$HOME/Library/Application Support/$EDITOR_DIR/User/keybindings.json"

mkdir -p "$(dirname "$KB")"
[ -f "$KB" ] || printf '// Place your key bindings in this file to override the defaults\n[\n]\n' > "$KB"

# Pair key and command within ONE binding entry: two separate entries (f17 -> X,
# some-other-key -> focusNext) must not read as "already installed".
# exit 0 = f17 -> focusNext present, 1 = f17 bound to something else, 2 = unbound.
f17=0
python3 - "$KB" <<'CHECK' || f17=$?
import re, sys
entries = [e for e in re.findall(r"\{[^{}]*\}", open(sys.argv[1]).read())
           if re.search(r'"key"\s*:\s*"f17"', e)]
if not entries:
    sys.exit(2)
ok = any(re.search(r'"command"\s*:\s*"workbench\.action\.terminal\.focusNext"', e)
         for e in entries)
sys.exit(0 if ok else 1)
CHECK
if [ "$f17" -eq 0 ]; then
  echo "resume-fleet keybindings already present in $KB"; exit 0
fi
if [ "$f17" -ne 2 ]; then
  # f17 is bound to something ELSE — installing on top would make the fleet's
  # F17 presses fire the user's binding. Bail loudly instead of claiming success.
  echo "ERROR: f17 already bound to a non-resume-fleet command in $KB — resolve manually" >&2
  exit 1
fi

BLOCK='    // --- resume-fleet automation (rare F-keys) ---
    { "key": "f17", "command": "workbench.action.terminal.focusNext" },
    { "key": "f18", "command": "workbench.action.terminal.selectAll", "when": "terminalFocus" },
    { "key": "f19", "command": "workbench.action.terminal.copySelection", "when": "terminalFocus" }'

# insert before the final top-level ']'. If the array already has entries, add a comma
# to the previous last entry.
python3 - "$KB" <<'PY'
import sys, re
p = sys.argv[1]
s = open(p).read()
block = '''    // --- resume-fleet automation (rare F-keys) ---
    { "key": "f17", "command": "workbench.action.terminal.focusNext" },
    { "key": "f18", "command": "workbench.action.terminal.selectAll", "when": "terminalFocus" },
    { "key": "f19", "command": "workbench.action.terminal.copySelection", "when": "terminalFocus" }
'''
idx = s.rstrip().rfind(']')
head = s[:idx].rstrip()
# if the array is non-empty (last non-ws char is '}'), we need a comma before our block
if head.rstrip().endswith('}'):
    head = head + ','
elif head.rstrip().endswith('['):
    pass
open(p, 'w').write(head + '\n' + block + ']\n')
PY
echo "installed resume-fleet keybindings into $KB"