---
name: sequencing-for-launch
description: Use when prioritizing a large or multi-repo backlog, ordering what to build next, building a roadmap, scoping an MVP or launch cut, deciding what to defer, or weighing a partial/shim/stopgap against building the real thing. Symptoms: "hundreds of tickets", cross-repo dependencies, "what should we do first", "is this worth shimming for now".
---

# Sequencing for Launch

## Overview

Optimize for **total time to a live product**, not local velocity or breadth of progress. The fastest path to launch is a small number of *complete, shippable* chunks built on the *final* architecture — not broad partial progress everywhere.

Core principle: **A move is only allowed if it does not increase total time-to-complete.** Faster-right-now that creates rework later is slower overall. Reject it.

## The Three Laws

1. **Depth over breadth — finish one launchable surface before spreading.**
   100% of 1 thing beats 80% of 10 things. Only a *complete* surface earns revenue, users, or learning; a backlog of 80%-done features earns nothing and rots. Pick the smallest surface that can actually go live, and drive it to done.

2. **Net-neutral-or-better moves only — no shims that get ripped out.**
   Judge every step by its effect on TOTAL time, not time-to-this-PR. A stopgap that's quick now but must be replaced later is net-negative — it adds the build + the rip-out + the re-build. If you're going to build it, build it right the first time, on the final architecture. (This does NOT mean gold-plating — build the *right* thing once, not extra things.)

3. **Sequence by dependency into chunks that can go live.**
   Order so each chunk (a) has its dependencies already built, (b) is independently shippable to users, and (c) leaves the system on the final architecture (no backtracking). A "wave" is a set of chunks that together unlock a launch increment.

## The One Test

For any proposed task or ordering, ask:

> **Does this reduce the total time to the finished, live product?**

- Reduces total time → do it.
- Only reduces time-to-this-PR but adds rework/replacement later → **reject** (violates Law 2).
- Advances a surface we're not launching first → **park it** (violates Law 1).

## Building the Roadmap

1. **Define the launch surface.** The smallest *complete* product that can go live (e.g. "one great editor" — not the editor + 9 half-built verticals).
2. **Trace the critical path** to that surface. Everything not on it is "later," no matter how appealing.
3. **Order the path by dependency.** Substrate before consumer; shared capability before the features that need it. Use real dependency edges (blocks/blockedBy, parent epics), not vibes.
4. **Group into go-live chunks/waves.** Each wave should end at a state you could ship and demo.
5. **Build each chunk right** — final architecture, no shims (Law 2). If a true seam is unavoidable, make it a real interface you'd keep, not a throwaway.
6. **Park the rest** in an explicit "later" tier. Naming what you're *not* doing is half the prioritization.

## Before Adding Work: Find Existing First

Most "new" capability already exists in a mature codebase. Before sequencing a build, search the ecosystem for the *concern* (not the ticket name) and confirm it's genuinely unbuilt. A ticket that's already done elsewhere is the cheapest win — it's zero build time. (Triage closes/merges before you sequence.)

## Red Flags — STOP, you're optimizing the wrong thing

| Thought | Reality |
|---|---|
| "Let's get all 10 features to MVP, then polish" | 80% of 10 ships nothing. Drive 1 to 100%. |
| "A quick shim unblocks us now; we'll redo it later" | Net-negative: build + rip-out + rebuild. Build it right once. |
| "This PR is faster this way" | Wrong metric. Optimize total time, not this PR. |
| "It's progress, so it's good" | Progress off the launch path is motion, not progress. |
| "We'll need it eventually" | Eventually ≠ now. Park it; sequence the launch path. |
| "Build the substrate fully generic first" | Build it right for the launch consumer; generalize when the 2nd consumer is real — if that's net-neutral. |

## Common Mistakes

- **Breadth-first backlog burndown** (touch everything a little) → nothing reaches shippable. Go depth-first.
- **Local-velocity optimization** (whatever's fastest to merge today) → accumulates shims and rework. Optimize the total.
- **Sequencing before triage** → you order tickets already done/obsolete elsewhere. Find-existing + close first.
- **Implicit "later"** → without an explicit park list, deferred work keeps leaking into the critical path.
