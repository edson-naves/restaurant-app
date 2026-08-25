# PAYMENT_BRANCH_RECONCILIATION.md

Read-only Git/history reconciliation of the payment/security remediation vs. the
currently-inspected branch. **Nothing was modified** — only inspection commands
were run (`git status`, `branch -a`, `log`, `log --all -- <path>`, `ls-tree`,
`merge-base`, `branch --contains`, `rev-list`, `show`, `grep <sha>`). No checkout,
merge, cherry-pick, reset, rebase, commit, stash, or clean.

Evidence tags: **GIT-CONFIRMED** = directly shown by git object/ref inspection ·
**DOCUMENT-CONFIRMED** = from review artifacts on the branch · **UNRESOLVED** =
git cannot settle it.

---

## A. Current branch

- **`feat/floor-map`** @ `8a0e349` (HEAD). Working tree **not clean**: modified
  `app/migrate.py`, `app/models/oltp.py` (plus the untracked Kitchen Stations
  artifacts and `docs/`). GIT-CONFIRMED.
- Note: the Kitchen Stations **B1 / PreparationTask** code is **not committed** on
  this branch either — `git grep "class PreparationTask" 8a0e349 -- app/models/oltp.py`
  → 0; `test_prep_tasks.py` is untracked. B1 exists only in the uncommitted working
  tree. (GIT-CONFIRMED; relevant to §G/§H.)

## B. Relevant payment/security branches

- **`fix/p0-security-and-payments`** @ `58e0324` — the remediation branch. Present
  both locally and as **`origin/fix/p0-security-and-payments`**, and the two point
  at the **same** SHA `58e0324a2f72e94506b28e172c26492d2508a6b7` (pushed / in sync).
  GIT-CONFIRMED.
- No other branch contains any of the payment/security commits (see §D).
- Remote: `origin = https://github.com/edson-naves/restaurant-app.git`.

Full local/remote ref list (GIT-CONFIRMED):
`feat/floor-map`(8a0e349), `feat/happy-hour`(f4350e5), `feat/happy-hour-nudge`(fea30c1),
`feat/upsell`(fa5c7f9, +origin), `feat/upsell-by-category`(489c557),
`fix/p0-security-and-payments`(58e0324, +origin), `main`(1756830, +origin).

## C. Relevant commits (all on `fix/p0-security-and-payments`, chronological — GIT-CONFIRMED)

| SHA | Date (local) | Subject |
|---|---|---|
| `07ee4f1` | 2026-08-08 | *common ancestor* — "Kitchen: don't show 'All statuses'…" |
| `81d0cd6` | 2026-08-09 | **fix(stage1): fail closed on production config, split liveness/readiness** (adds `app/config.py`) |
| `0a98b12` | 2026-08-09 | **feat(stage2a): durable PaymentAttempt record + state machine** |
| `67aebfd` | 2026-08-09 | **feat(stage2b): pluggable payment-provider layer (any method, per instrument)** |
| `1d88455` | | fix(2a/2b review A/B): concurrency-safe attempts + separate refund lifecycle |
| `555dc62` | | fix(2a/2b review C): generalize provider contract; real key forwarding |
| `69860db` | | fix(2a/2b review D): generic /readyz, Square config doc/validation agree |
| `a6addbe` | | test(2a/2b review E): real-FK fixtures + Postgres concurrency proofs |
| `0621b6a`,`3a4e552`,`be115ee`,`68b5f84`,`58abfd1`,`911b61f`,`ceb2004`,`5572a16`,`9dfe1d7`,`b8b0636`,`3b6f7bd`,`3166c67`,`cfe…`,`2c2e676`,`9c2e954`,`f44bb3f` | | h2–h6 review-series hardening (processor evidence, refund idempotency, currency, capabilities) |
| `50f687a` | | feat(2c slice 1): charge settlement service + paid-item selection on the attempt (adds `settlement.py`) |
| `e3c28f7` | | fix(2c slice 1 review): transaction contract, CAS convergence, selection ownership |
| `f55f048`,`a0c0cf9`,`ca9ea30` | | 2c slice-1 fixes + tests (fingerprint versioning, backfill) |
| `58e0324` | 2026-08-14 | **WIP snapshot: Stage 2c slices 2a/2b + slice-2b review fixes** (branch tip) |

Source files that exist at `58e0324` but on **no** other branch (GIT-CONFIRMED
`git ls-tree -r 58e0324`): `app/config.py`, `app/services/payment_attempts.py`,
`app/services/refund_attempts.py`, `app/services/payment_providers.py`,
`app/services/settlement.py`, `app/services/charge.py`, plus
`tests/test_payment_attempts.py`, `tests/test_payment_providers.py`,
`tests/test_refund_attempts.py`, `tests/test_settlement.py`, and remediation docs
(`REMEDIATION_PLAN.md`, `REVIEW_HANDOFF*.md`).

## D. Branch containment matrix (GIT-CONFIRMED)

`git branch -a --contains <sha>` for the stage anchors → **only**
`fix/p0-security-and-payments` (+ its origin remote-tracking ref):

| Commit | main | feat/floor-map | fix/p0-security-and-payments | origin/fix/p0… | any other local |
|---|---|---|---|---|---|
| `81d0cd6` stage1 | ✗ | ✗ | ✓ | ✓ | ✗ |
| `0a98b12` stage2a | ✗ | ✗ | ✓ | ✓ | ✗ |
| `67aebfd` stage2b | ✗ | ✗ | ✓ | ✓ | ✗ |
| `1d88455` 2a/2b review | ✗ | ✗ | ✓ | ✓ | ✗ |
| `50f687a` 2c slice1 | ✗ | ✗ | ✓ | ✓ | ✗ |
| `58e0324` tip (2c WIP) | ✗ | ✗ | ✓ | ✓ | ✗ |

Payment source files present on the **main** tree (`1756830`): **none**. On the
**feat/floor-map** tree (`8a0e349`): **none**. GIT-CONFIRMED.

## E. Merge / divergence explanation (GIT-CONFIRMED)

Merge-bases:
- `merge-base(fix, main)` = `07ee4f1`
- `merge-base(fix, feat/floor-map)` = `07ee4f1`
- `merge-base(main, feat/floor-map)` = `1756830` (**= main's tip**)

Divergence counts:
- `main..fix` = **33** commits (payment/security work exclusive to fix).
- `fix..main` = **17** commits (main advanced separately after the split).
- `main..feat/floor-map` = **41**; `feat/floor-map..main` = **0** → **main is fully
  contained in feat/floor-map** (floor = main + 41 floor/kitchen commits).

Resulting shape:

```
07ee4f1  (2026-08-08, common ancestor)
   │
   ├──● fix/p0-security-and-payments  (+33: stage1 → 2a → 2b → reviews → 2c WIP)
   │      tip 58e0324 (2026-08-14)   [local == origin]   ← NEVER merged back
   │
   └──● main line (+17)  tip 1756830 main (2026-08-18)
          └──● feat/floor-map (+41 floor/kitchen)  tip 8a0e349 (2026-08-19, HEAD)
```

- **Was the payment work merged into main?** No — 33 commits remain exclusive to
  fix; `git ls-tree` shows the files absent from main. GIT-CONFIRMED.
- **Reverted / dropped / cherry-picked elsewhere?** No revert or cherry-pick found:
  the stage-marked subjects appear on no other branch (`git log main feat/floor-map
  --grep=stage…` empty), and the files exist on no other tree. The work **remains
  isolated** on `fix/p0-security-and-payments` (+origin). GIT-CONFIRMED.

## F. Status of Stage 1 / 2a / 2b / 2c

- **Existence & lineage (GIT-CONFIRMED):** Stage 1 (`81d0cd6`), 2a (`0a98b12`),
  2b (`67aebfd`), plus 2a/2b + h-series review commits, and 2c slice-1 + a 2c WIP
  tip (`58e0324`, subject "Stage 2c slices 2a/2b + slice-2b review fixes") all exist
  and are committed on `fix/p0-security-and-payments`.
- **Approval (DOCUMENT-CONFIRMED, not a git fact):** the branch carries review/
  handoff artifacts (`REVIEW_HANDOFF*.md`, `REMEDIATION_PLAN.md`) consistent with the
  reported Stage 1 / 2a / 2b APPROVED and 2c authorized-not-closed. Git can confirm
  the commits and their messages, **not** the approval decision itself.
- **2c is not closed:** the tip is an explicit **"WIP snapshot"** for 2c; no
  closing/approval commit follows it. GIT-CONFIRMED (branch tip subject).

## G. Why current `feat/floor-map` lacks the approved payment code

**Branch divergence, not code removal.** GIT-CONFIRMED:
- `feat/floor-map` and `fix/p0-security-and-payments` share ancestor `07ee4f1`
  (2026-08-08). The payment/security commits were made **on the fix branch only**,
  after that split, and were **never merged** into main or into feat/floor-map.
- The files were therefore **never present** on the feat/floor-map line — there is
  no deletion commit, no revert (the files simply do not exist in any commit reachable
  from `8a0e349`).
- **Orphan `.pyc` explanation (GIT-CONFIRMED):** the stray
  `app/services/__pycache__/{charge,settlement,payment_attempts,payment_providers,refund_attempts}.cpython-314.pyc`
  correspond **exactly** to source files that exist at `58e0324` (fix branch) and
  nowhere on feat/floor-map. They are compiled bytecode left in `__pycache__`
  (git-ignored) from a **prior local checkout of `fix/p0-security-and-payments` in
  this same working directory**; switching back to `feat/floor-map` removed the
  tracked `.py` but not the ignored `.pyc`. They are not evidence of code on this
  branch — they are residue of the other branch.
- The prior architecture audit inspected `feat/floor-map` only, so it correctly
  reported the payment remediation as absent **from that branch**; it did not survey
  `fix/p0-security-and-payments`. Both statements are true and now reconciled.

## H. Recommended wording for persistent docs (status labels only — no architecture advice)

- **CURRENT BASELINE (what runs / is checked out):** `feat/floor-map` @ `8a0e349`
  (contains all of `main`; adds floor/kitchen UI). It does **not** contain the
  payment/security remediation. The seat-ledger + direct-Square payment model
  described in the architecture audit is what exists on this branch.
- **APPROVED BUT UNMERGED:** the payment/security remediation on
  **`fix/p0-security-and-payments` @ `58e0324`** — Stage 1, 2a, 2b (reported
  APPROVED) and Stage 2c (WIP / authorized-not-closed). Introduces `app/config.py`
  (production fail-closed), `PaymentAttempt`/`RefundAttempt`, `payment_providers`
  (provider abstraction), `settlement`/`charge`, durable processor evidence, and the
  matching tests. **Committed and pushed to origin, but never merged into `main` or
  `feat/floor-map`.**
- **HISTORICAL / SUPERSEDED:** none identified — no payment commit was reverted,
  dropped, or superseded by a later branch. (The orphan `.pyc` are residue, not a
  historical code state.)
- **CURRENTLY ACTIVE:** Kitchen Stations B2 **design** on `feat/floor-map`
  (uncommitted); note that even Kitchen B1/PreparationTask is uncommitted here.
- **UNKNOWN (git cannot decide):** whether the approvals recorded in the fix branch's
  handoff docs are still considered current direction, and whether an integration of
  fix ↔ floor is intended. These are governance facts, not git facts.

## I. Unresolved Git-history ambiguities

1. **No single branch holds both work streams.** Payment remediation (`fix/p0…`,
   +33) and floor/kitchen work (`feat/floor-map`, +41 over main) are **fully
   divergent** from `07ee4f1`. Git shows the divergence; it cannot tell you which is
   the intended trunk. (GIT-CONFIRMED divergence; intent UNRESOLVED.)
2. **"Approved" is external to git.** Stage approvals come from review artifacts, not
   commit metadata. Git confirms the commits exist and are stage-labeled; it does not
   confirm the approval state — treat that as DOCUMENT-CONFIRMED.
3. **2c is a WIP snapshot**, not a closed slice — the exact intended end-state of 2c
   is not recoverable from git alone.
4. **`main` is stale relative to both** (last main commit 2026-08-18 `1756830`; it is
   an ancestor of feat/floor-map and diverged from fix). Whether `main` is meant to be
   the integration target is a governance question git cannot answer.

---

*End of PAYMENT_BRANCH_RECONCILIATION.md — read-only history reconciliation; no code, docs, branches, commits, working tree, dependencies, schema, or data were modified.*
