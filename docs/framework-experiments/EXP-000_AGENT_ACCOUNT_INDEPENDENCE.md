# EXP-000 — Agent & Account Independence

Status: **CLOSED / PASS** (2026-09-03). Checkpoint at closeout: `main` = `ac8236c`;
`origin/main` / recorded PROD = `489f6d2`.

## Objective / Hypothesis

Test whether a fresh agent can safely recover the Restaurant App from repository
artifacts, Git state and durable evidence alone — without prior conversation or
account context.

## Experimental Method

- real multi-worktree topology;
- started from the misleading historical `restaurant_app` / `feat/floor-map` checkout;
- prior chat/context not supplied;
- repository / Git / tracked evidence only;
- sealed answer key held outside the repository.

## Baseline Result

```text
safety-critical facts recovered   16/16
invented safety-critical claims   0
prior-chat dependencies           0
Kitchen recovery                  PASS
```

Four portability gaps demonstrated:

1. canonical recovery/worktree procedure not explicit;
2. recent Release B / Stage 2c evidence not durably reachable;
3. stale/contradictory current-state governance;
4. Stage 2c Git implementation lacked durable provenance/governance.

## Changes Made

- explicit recovery rule (`AGENTS.md`, `docs/05_AI_HANDOFF.md` Step 0);
- current-state authority/status consolidation;
- Stage 2c provenance recorded **without** authorization;
- durable Payments evidence promotion into `docs/Evidence/Payments/`;
- stale governance cleanup and ADR-029.

## Independent Review

Claude implemented; Codex independently reviewed. Initial blockers were
documentation inconsistencies only; they were corrected. Final recheck:
`BLOCKERS REMAINING: NO` / `SAFE TO COMMIT: YES`.

## Final Retest

```text
safety-critical facts recovered   16/16
invented safety-critical claims   0
prior-chat dependencies           0
```

Evidence is now durably reachable, and the canonical recovery rule was used
successfully from the stale worktree.

## Final Verdict

**PASS / APPROVED.**

A fresh agent can safely recover the project using repository artifacts, Git
state, and durable evidence without depending on prior agent conversations.

## Scope Boundary

EXP-000 repository portability = **PASS**.

EXP-000 validates Restaurant repository portability only. Overall account migration is
not established by EXP-000 and remains outside the experiment's scope.

## Known Non-Blocking Limitations

- historical branches may carry an older `AGENTS.md`;
- the repository records the deployed PROD SHA but does not prove live runtime;
- detailed G3/variance investigation remains intentionally off-repository;
- operational restore-drill evidence remains distinct from the G5 local restore
  validation.

## Framework Principle Confirmed

```text
Agent conversations are working memory.
Repository artifacts are institutional memory.
```
