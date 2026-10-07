#!/bin/bash
# shell-code-only.sh — shared library: keep only the parts of a Bash command
# that Bash would treat as CODE, and drop everything it would treat as DATA
# (heredoc bodies, here-strings, quoted spans, comments).
#
# Sourced by the PreToolUse/PostToolUse hooks that must detect what a command
# DOES rather than what its text mentions:
#   hooks/branch-name-gate.sh   (is a branch really being created?)
#   hooks/linear-startwork.sh   (same detection, PostToolUse side)
#
# WHY: both hooks used to strip heredocs with
#
#     scan="${cmd%%<<*}"
#
# which truncates the command at the FIRST `<<`, so everything after a heredoc
# was invisible to the gate. A command shaped like
#
#     cat > notes.md <<'EOF'
#     …
#     EOF
#     git checkout -b some-branch-with-no-dev-token
#
# created a branch branch-name-gate.sh never saw (no dev-NNN enforcement) and
# linear-startwork.sh never reported (ticket left in Backlog, unassigned). It
# failed OPEN, quietly. This walk drops the heredoc BODY and keeps the code that
# follows it, so both directions hold: a create AFTER a heredoc is still gated,
# and a create-looking string INSIDE a heredoc body still is not.
#
# A character walk rather than a regex, because quoting and heredocs are not
# separable: a quoted `<<` must not register a heredoc (`echo "note << EOF"` …
# `EOF` would otherwise swallow the lines between as a body), while a heredoc
# opened inside a double-quoted command substitution IS real
# (`-m "$(cat <<'EOF' … EOF)"`). Quote state therefore has to be tracked across
# lines, and `$(` / `` ` `` / `(` re-enter code context from inside a string.

# shell_code_only <command-text>
#   Prints the code-only projection of the command on stdout, one line per input
#   line. Never fails: unparseable input degrades toward printing MORE, not less
#   (see the END clause), so a caller scanning the output cannot lose a real
#   invocation to a parse quirk.
shell_code_only() {
  awk -v SQ="'" '
    function pop(  v) { v = stack[sp]; sp--; return v }
    BEGIN { qh = 1; qt = 0; sp = 0; bt = 0; q = 0; nb = 0 }
    {
      line = $0
      if (qt >= qh) {                          # inside a heredoc body
        t = line
        if (dash[qh]) sub(/^\t+/, "", t)       # <<- strips leading TABS only
        if (t == delim[qh]) { qh++; nb = 0 }   # closed: the body really was data
        else buf[++nb] = line
        next
      }
      out = ""; n = length(line); i = 1
      while (i <= n) {
        c = substr(line, i, 1); d = substr(line, i + 1, 1)
        if (q == 1) {                          # single quotes: all literal
          if (c == SQ) q = 0
          i++; continue
        }
        if (c == "\\") { i += 2; continue }    # escaped char is never syntax
        if (c == "$" && d == "(") { stack[++sp] = q; q = 0; out = out "("; i += 2; continue }
        if (c == "`") {                        # legacy command substitution
          if (bt) { q = pop(); bt = 0; out = out ")" }
          else    { stack[++sp] = q; q = 0; bt = 1; out = out "(" }
          i++; continue
        }
        if (c == ")" && sp > 0) { q = pop(); out = out ")"; i++; continue }
        if (q == 2) {                          # double quotes: data
          if (c == "\"") q = 0
          i++; continue
        }
        # ---- code context ----
        if (c == SQ)   { q = 1; i++; continue }
        if (c == "\"") { q = 2; i++; continue }
        if (c == "(")  { stack[++sp] = q; out = out "("; i++; continue }
        if (c == "#" && (out == "" || substr(line, i - 1, 1) ~ /[ \t]/)) break   # comment
        if (c == "<" && d == "<") {
          if (substr(line, i + 2, 1) == "<") { i += 3; continue }   # here-string
          i += 2
          isdash = 0
          if (substr(line, i, 1) == "-") { isdash = 1; i++ }
          while (substr(line, i, 1) ~ /[ \t]/) i++
          w = ""
          while (i <= n) {
            c2 = substr(line, i, 1)
            if (c2 ~ /[ \t;&|<>()]/) break
            if (c2 != SQ && c2 != "\"" && c2 != "\\") w = w c2
            i++
          }
          if (w != "") { qt++; delim[qt] = w; dash[qt] = isdash }
          continue
        }
        out = out c; i++
      }
      print out
    }
    END {
      # An unterminated heredoc means the delimiter never closed. Bash rejects
      # that outright, so re-emitting the lines we skipped costs nothing real
      # and keeps a swallowed invocation visible — fail CLOSED rather than
      # silently eating the rest of the command.
      for (i = 1; i <= nb; i++) print buf[i]
    }
  ' <<<"$1"
}

# unquote_plain_operands <command-text>
#   Prints the command with DECORATIVE quotes removed from the operand of a
#   branch-creation flag (-b/-B, -c/-C, `git branch`). Run it BEFORE
#   shell_code_only(), which drops every quoted span as data and would
#   otherwise eat the branch NAME along with the quotes around it.
#
# WHY: quoting a branch name is ordinary defensive practice, and it
# made both callers misread the command. This
#
#     git worktree add <path> -b "me/dev-1-x" origin/main
#
# reached the extraction as `git worktree add <path> -b  origin/main`, so the
# gate denied naming `origin/main` — the START POINT — as the proposed branch,
# while this
#
#     git checkout -b "me/dev-1-x"
#
# reached it as `git checkout -b ` with no operand at all: no create was found
# and ANY quoted branch name was silently allowed through, dev-NNN token or not.
# The gate disarmed itself on a quote.
#
# Only a span of plain word characters is unquoted, and only directly after a
# create flag. Such a span expands to exactly the bare token, so what bash does
# with the command is unchanged — and nothing that must stay DATA can be
# revealed, because every create-looking phrase ("git checkout -b x") contains
# whitespace, which is not a plain word character. An enclosing commit message
# or heredoc body therefore keeps its own quotes and is still dropped as data.
#
# WHY the character walk (review follow-up, same operand-recovery bug): the operand is a single bash WORD, and bash concatenates adjacent
# quoted/bare fragments of one word with no space between them —
#
#     git worktree add <path> -b me/dev-2242"-quickfix" origin/main
#
# creates the branch `me/dev-2242-quickfix`, one token. A regex that only
# matched a flag followed by ONE whole quoted-or-bare span (the original fix)
# never matches this shape at all — the operand starts bare — so it passed
# through untouched, `shell_code_only` then dropped the still-quoted
# `"-quickfix"` tail as data, and the gate validated `me/dev-2242`: a
# canonical name allowing a non-canonical branch. Walking the word char-by-char
# and stripping quote characters wherever they fall (opening or closing,
# leading, trailing, or mid-word) collapses any number of adjacent fragments
# into the one bare token bash actually creates. The safe-charset check is
# unchanged and still governs acceptance: if anything outside a branch-name
# character turns up (quoted or not) before an unquoted word boundary, or a
# quote is left open at the boundary, the walk aborts and the text is left
# exactly as it was — same fail-safe as the original.
unquote_plain_operands() {
  awk -v SQ="'" '
    function is_safe(c) { return c ~ /^[A-Za-z0-9._\/@:+=-]$/ }
    {
      line = $0
      out = ""; n = length(line); i = 1
      while (i <= n) {
        rest = substr(line, i)
        flaglen = 0
        if (match(rest, /^-[bBcC][[:blank:]]+/)) flaglen = RLENGTH
        else if (match(rest, /^git[[:blank:]]+branch[[:blank:]]+/)) flaglen = RLENGTH
        if (flaglen > 0) {
          # Walk the whole operand WORD (to the next unquoted boundary),
          # stripping quote characters as they are seen so adjacent
          # quoted/bare fragments collapse into one token, exactly as bash
          # concatenates them. `ok` guards the original safe-charset
          # invariant; `q` must return to 0 (no quote left open) at the
          # boundary for the walk to be accepted.
          j = i + flaglen
          q = 0; word = ""; ok = 1
          while (j <= n) {
            c = substr(line, j, 1)
            if (q == 0 && c ~ /[ \t;&|<>()]/) break
            if (c == "\"" && q != 1) { q = (q == 2) ? 0 : 2; j++; continue }
            if (c == SQ && q != 2)   { q = (q == 1) ? 0 : 1; j++; continue }
            if (!is_safe(c)) { ok = 0; break }
            word = word c
            j++
          }
          if (ok && q == 0) {
            out = out substr(line, i, flaglen) word
            i = j
            continue
          }
          # Not a clean, fully branch-name-safe word (unsafe char, or a quote
          # left open at the boundary) -> leave it untouched: fall through to
          # the plain copy below, one character at a time, same as before.
        }
        out = out substr(line, i, 1)
        i++
      }
      print out
    }
  ' <<<"$1"
}
