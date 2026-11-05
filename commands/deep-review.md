---
description: Standardized PR review with empirical validation
argument-hint: <pr-url-or-number> [paired-pr-url-or-number]
---

# Deep Review Process

Standardized process for PR reviews with empirical validation. This file is the map: the core that runs on every review is inline in section 1 of `~/.claude/commands/PRlaunch.md`, and every other section moved unchanged to `~/.claude/playbooks/deep-review/`.

**Read the core first.** It prints with:

```bash
awk '/deep-review-core:end/{f=0} f; /deep-review-core:start/{f=1}' ~/.claude/commands/PRlaunch.md
```

**Arguments:** `$ARGUMENTS` — one or two PR identifiers (links or numbers). If two are given, treat as a paired multi-repo review (backend + frontend).

---

## Where each step lives

| Step | File | Notes |
| -- | -- | -- |
| 0. Input, re-review | `~/.claude/playbooks/deep-review/triage-and-branch.md` | Ticket resolution is in `reviewer-prompts.md` |
| 1. Triage | `~/.claude/playbooks/deep-review/triage-and-branch.md` | Classify depth, File-size threshold and Check cross-PR conflicts are in the core; Generated-code exclusions are in `reviewer-prompts.md` |
| 1.5. AI-cheat mechanical pass | core (detector names), `~/.claude/playbooks/deep-review/mechanical-passes.md` (the checklist, turning a hit into a finding) |  |
| 1.6. Changed-surface enumeration | core (summary), `~/.claude/playbooks/deep-review/mechanical-passes.md` (recipe) |  |
| 2. Branch alignment | core (check), `~/.claude/playbooks/deep-review/triage-and-branch.md` (process) |  |
| 3. Static analysis | core (agents, repo invariants, commonly missed, Codex run), `~/.claude/playbooks/deep-review/reviewer-prompts.md` (severity criteria, finding format, detection prompts, ticket-compliance agent, diff format) | Codex lane detail is in `~/.claude/playbooks/deep-review/codex-lane.md` |
| 3.5. Adversarial refute pass | core, `~/.claude/playbooks/deep-review/refute-and-precedents.md` |  |
| 4. Consolidation | core (severity table, origin), `~/.claude/playbooks/deep-review/consolidation-detail.md` (effort axis, ticket-compliance mapping, structural calibration, code-judo) | Codex divergence is in `codex-lane.md` |
| 5. Empirical validation | core (5b, 5e, 5f), `~/.claude/playbooks/deep-review/validation.md` (5a, 5b backstop, 5c, 5d) |  |
| 6-10. Plan mode, LT review, publish, re-review, LT direct fix | `~/.claude/playbooks/deep-review/publish-and-fix.md` | Codex publish gate is in `codex-lane.md` |
| Principles, version history | `~/.claude/playbooks/deep-review/principles-and-history.md` |  |
| Review precedents | `~/.claude/playbooks/deep-review/refute-and-precedents.md` |  |

A `/deep-review` of a PR reads the core, `triage-and-branch.md`, `mechanical-passes.md`, `reviewer-prompts.md`, `publish-and-fix.md` and `principles-and-history.md`, plus any other file whose row in the core applies. Read these files from `~/.claude/`, never a copy in the repo under review.

---

# Begin

Now execute the process for `$ARGUMENTS`. Start at Step 0 (Input) — identify whether it's a single PR or a paired multi-repo review and whether this is a first review or re-review. Then proceed through the steps with judgment.
