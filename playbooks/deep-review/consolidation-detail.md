# Deep review reference: consolidation detail

Part of the deep-review process. The core that runs on every review is section 1 of `~/.claude/commands/PRlaunch.md`; `~/.claude/commands/deep-review.md` maps every step to its file. **Read this file when:** every review, at Step 4.

## 4. Consolidation

### Severity decision table (v6.9)

**Effort axis for CRITICAL/HIGH (v7.7):** every CRITICAL/HIGH finding carries `Effort: quick-win | heavy-lift` plus a one-line reason. **quick-win** = the fix is local to this PR's diff, needs no design decision, and the author can land it in this PR. **heavy-lift** = the fix needs a redesign, a migration, a cross-module/cross-repo change, or a decision the PR author can't make alone. Disposition comes from severity × effort:

| Severity | Effort | Disposition |
| -- | -- | -- |
| **CRITICAL** | quick-win | Fix in this PR before merge. |
| **CRITICAL** | heavy-lift | **Merge-blocking**, or explicitly **waived** with a written reason **and** a filed follow-up ticket linked from the review. Never silently deferred. |
| **HIGH** | quick-win | Fix in this PR before merge. |
| **HIGH** | heavy-lift | **Merge-blocking**, or explicitly **waived** with a written reason **and** a filed follow-up ticket linked from the review. Never silently deferred. |

* **Effort changes disposition, never severity.** Do not downgrade a heavy-lift finding to make it non-blocking, and do not upgrade a quick-win to force it.
* **"We'll do it later" is not a disposition.** A heavy-lift finding raised at merge time with neither a block nor a waiver+ticket becomes an unresolved thread — post-hoc review records heavy things, it doesn't get them fixed. The waiver ticket follows the follow-up filing buckets in `CLAUDE.md` § Work Taxonomy (bucket 2, same epic, for IN-SCOPE findings); an OUT-OF-SCOPE finding's separate tracking issue (§ Classify finding origin) already satisfies the ticket half. A waiver+ticket is the one sanctioned exception to that table's IN-SCOPE "must fix if MEDIUM+" — and only for heavy-lift CRITICAL/HIGH.
* **This is deliberately counterintuitive** — see the v7.7 line in `principles-and-history.md`. Escalate-by-severity is the policy that left Criticals the *least* resolved row; do not "fix" this table back to it.

### Ticket-compliance severity mapping (v7.4)

Feeds the severity decision table in the core — not a parallel severity axis. Applies only to Step 3's ticket-compliance agent output:

| Compliance verdict | Severity | Action |
| -- | -- | -- |
| `not_compliant_requirement` the PR's own title/description/body **claims** to deliver | **HIGH** | The PR is not what it says it is — same row as any other demonstrated broken-functionality finding. Evidence: quote the claim next to the missing/wrong code. |
| `not_compliant_requirement` the PR is **silent** on | **MEDIUM** | File a follow-up ticket under the source ticket's epic (a nit/ops follow-up on the feature being shipped; the epic stays open until it's done). Never blocks. |
| `requires_further_human_verification` item | **Surfaced, never a severity, never blocking** | No Step 5 recourse — it genuinely can't be settled from code. Stays a standing note for the PR owner/LT. |
| Scope drift — code the diff adds that no ticket requirement asked for | **INFO**, escalate to **MEDIUM** only if it touches auth/payments/data | Catches an agent quietly widening its own mandate. Evidence: the ticket requirement list plus the diff hunk with no corresponding requirement. |

### Classify finding origin

**Why this matters:** in one review, the most impactful finding (XSS via `javascript:` URIs) was in a file the PR didn't modify. Without origin classification, it's tempting to either block the PR unfairly or ignore the finding entirely. The correct action: fix it opportunistically since the PR touched adjacent components, but don't hold the PR hostage for pre-existing debt.

### Structural severity calibration (v6.8)

Structural findings (spaghetti growth, thin wrappers, boundary muddying, layer leaks, file-size crossings) follow stricter severity rules than functional ones:

* Default severity: **MEDIUM**.
* **HIGH** only when the PR materially tangles a *shared* path AND the cleaner structure is obvious and scoped (the fix doesn't require a redesign).
* **Never CRITICAL.**
* A *missed simplification opportunity* — code that works but could be dramatically simpler — is a **suggestion (INFO)**, never a blocker. Don't gut working complexity; recommend the reframing and move on.

### The code-judo question (v6.8)

After consolidating findings, ask once, explicitly: **is there a reframing of this change that deletes whole branches, helpers, modes, or layers while preserving behavior?** Refactors that move complexity around without reducing the number of concepts a reader must hold are the target. If a reframing exists, record it as an IN-SCOPE suggestion (INFO) with a concrete sketch of the simpler shape. This question informs the review — it does not gate the merge.
