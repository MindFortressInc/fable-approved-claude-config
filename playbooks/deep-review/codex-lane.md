# Deep review reference: Codex second-opinion lane

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** you turned the optional Codex lane on and its run wrote a result file, or you want to change its model. The run command is in the core. This file also holds the lane's model setting, rate-limit notes and exit behaviour, the Codex-origin confirmation rule (Step 5b) and the publish gate (Step 8).

### Codex second-opinion lane (optional, OFF by default, v7.5)

A second, independent model family (Codex, via a `~/.claude/hooks/codex-review.sh` wrapper) can run **in parallel** with the Step 3 agents (core, Step 3), reviewing the same diff read-only and reporting its own `findings[]` as structured JSON (`{file, line, severity, claim, evidence}` per entry). It is **never blocking**.

**Off unless you turn it on.** The lane runs only when **both** `CODEX_REVIEW_ENABLED=1` is set **and** the wrapper is installed. This repo does not ship the wrapper or its result schema yet; without it, or with the flag unset, skip the lane — Step 3 and everything after runs exactly as before. Enablement lives in configuration, never in the tool: a wrapper must exit non-zero (no dispatch) unless `CODEX_REVIEW_ENABLED=1`, so it stays safe for anyone who runs it with a clean environment. If you export the flag from `settings.json`'s `env` block, remember every shell Claude Code spawns inherits it — a test that needs the disabled path must unset it explicitly.

**Model is a config value.** `CODEX_REVIEW_MODEL` selects it; unset means the Codex account default (no `-m` flag). There is no built-in default model id. Verify flag behaviour (e.g. `--output-schema`) per model and per surface; never assume it carries over from another model.

**Review turns per window are the constraint.** A subscription-backed Codex has message-windowed rate limits; the resource that runs out is review turns per window, not dollars. When the window is spent, switch `CODEX_REVIEW_MODEL` or skip the lane with `CODEX_REVIEW_ENABLED=0`. An unreachable model at review time is a non-zero exit like any other, and the review proceeds without the lane.

**Mechanism:** dispatch plain `codex exec` with the review instructions folded into an inline prompt — **not** `codex exec review`. The `review` subcommand was proven live to ignore `--output-schema` entirely, even on a valid schema, and always returns freeform prose; it can never produce a structured result. Plain `codex exec <prompt>` does honor `--output-schema`.

* **The diff target is `--base origin/develop`**, matching this process's own `origin/develop...HEAD` convention. Plain `codex exec` has no `--base` flag, so the wrapper builds the exact git command itself — `git diff --no-textconv <ref>...HEAD`, three-dot — and states it explicitly in the prompt for Codex to run.
* Record the resolved model next to the findings (e.g. an `<output>.meta.json` sidecar), so you can always say which model produced each finding.
* Read-only is not a knob — always dispatch `-s read-only`, never `--dangerously-bypass-approvals-and-sandbox`. A second-opinion reviewer does not get write access, full stop.
* Instruct Codex to use `python3`, never plain `python`, for anything it decides to run itself — Codex's own sandbox may have only `python3` on `PATH` (observed live: a Codex-run command failed with `command not found: python` before this instruction was added).

**What comes out of this step is a candidate findings file, not findings.** Codex's `findings[]` are its own unverified claims about the code — see § Codex-origin findings: publish gate (Step 8) for why none of them may surface anywhere until Step 5b has confirmed them. This step only produces the file; Step 4 is where it gets reconciled against Claude's own consolidated findings.

## 4. Consolidation

### Codex second-opinion divergence (v7.5)

Only runs when Step 3's Codex lane actually produced a result file (§ Codex second-opinion lane) — skip this whole section when the lane is off, the wrapper isn't installed, or it exited non-zero, exactly as instructed there.

**Consolidate Claude's own findings first** (the core's Step 4 bullets, through Step 3.5's refute pass), as its own JSON array in the same shape the lane's output uses (`{file, line, severity, claim, evidence}` per entry — so no translation step is needed). Then run the three-bucket join (with the wrapper installed, its dispatch-free `buckets` subcommand does it):

```bash
~/.claude/hooks/codex-review.sh buckets <claude-consolidated-findings.json> <codex-output.json>
# -> {"agreed_count": N, "claude_only": [...], "codex_only": [...]}
```

The join is on **`(file, line)` with a small tolerance — never on claim text**: two model families paraphrase the same defect differently, and matching on wording would manufacture divergence that isn't real.

* **`agreed_count`** — report this as a single number in the internal review doc ("Codex independently corroborated N of Claude's own findings"). **Never re-render the agreed set finding-by-finding** — it is corroboration, not new information, and re-listing it is noise.
* **`claude_only`** — no change to this process. These are already real, already-consolidated findings; they proceed through Steps 4/5 exactly as if the Codex lane had never run.
* **`codex_only` is the entire product of this lane.** Every entry here is a claim Claude's own agents did NOT independently make. **None of them are findings yet** — each one is a fresh candidate that must go through Step 5b's confirmation (file:line verified against actual source, CONFIRMED/PARTIALLY CONFIRMED/NOT CONFIRMED) and the Step 3 evidence/severity rules exactly like a brand-new agent finding, before it may be assigned an origin (§ Classify finding origin, in the core) and merged into the unified findings list. A `codex_only` candidate that fails confirmation is dropped — noted in the internal doc as "Codex claimed X, not confirmed: <why>", never published anywhere (§ Codex-origin findings: publish gate, Step 8).

### 5b. Code validation

* **Codex-origin candidates are not exempt (v7.5):** every `codex_only` entry from § Codex second-opinion divergence (Step 4) goes through this exact same check — file:line verified against the actual source, CONFIRMED/PARTIALLY CONFIRMED/NOT CONFIRMED — before it is anything more than a claim. Codex's own `evidence` field is Codex's self-report, not Claude's verification; it does not substitute for actually reading the code at that file:line. **A Codex finding is a claim, not evidence, until this step confirms it.**

## 8. Publish

### Codex-origin findings: publish gate (v7.5)

**Hard rule: no finding that originated from the Codex second-opinion lane may reach a GitHub/PR comment, a tracker comment, or a memory write until Step 5b has independently confirmed it against the actual code.** Codex's self-report is a claim, not evidence — treat it with exactly the confirmation standard this process already applies to its own agents' UNVERIFIED findings, never less.

In practice this is automatic, not a step you have to remember: § Codex second-opinion divergence (Step 4) never adds a `codex_only` candidate to the unified findings list in the first place — Step 5b promotes it in, or it is dropped. By the time this Publish step runs, every finding on the list (Codex-origin or not) has already cleared the same bar. If you find yourself about to paste a `codex_only` entry straight into a review comment or a tracker note without it having gone through Step 5b first, stop — that is exactly the shortcut this rule exists to block.

When a Codex-origin finding IS published (i.e., it was confirmed and promoted), attribute it plainly — "(cross-checked via Codex second-opinion lane, confirmed)" next to the finding — so the PR owner and any later reader know a second model family caught it, and so you have a clean signal of which lane actually surfaced which defect.
