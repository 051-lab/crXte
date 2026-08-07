# crXte Stage 0 Baseline and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible, CI-gated crXte `v0.3.0` baseline on `main` without changing application behavior.

**Architecture:** Add one permanent read-only GitHub Actions workflow that installs the locked Python 3.12 development environment, runs Ruff, executes the complete pytest suite, and compiles `src` and `tests`. After the CI pull request merges and the push-triggered `main` run succeeds, use one temporary unmerged operations branch to create the annotated baseline tag and remove every obsolete setup branch, including the operations branch itself.

**Tech Stack:** GitHub Actions, Ubuntu, Python 3.12, `uv`, Ruff, pytest, `ffmpeg`, Git refs.

## Global Constraints

- Baseline commit: `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`.
- Baseline tag: immutable annotated tag `v0.3.0` pointing exactly to the baseline commit.
- Runtime target: WSL/Linux with Python 3.12 or newer.
- CI runner: `ubuntu-latest` with Python `3.12` only.
- Dependencies: install with `uv sync --locked --dev`; do not silently rewrite `uv.lock`.
- Required gates: Ruff, the complete pytest suite, and `compileall` for `src` and `tests`.
- No test exclusions, allowed failures, product behavior changes, UI changes, export changes, unrelated refactors, dependency upgrades, packaging, deployment, coverage reporting, or Python-version matrix.
- Permanent workflow permissions: `contents: read` only.
- Long-lived branch after cleanup: `main` only.
- Any reproducibility correction must be the smallest possible separate commit and must preserve intended behavior.
- Official action majors verified on 2026-08-06: `actions/checkout@v7`, `actions/setup-python@v7`, and `astral-sh/setup-uv@v9`.

---

## File Map

- Create: `.github/workflows/ci.yml` — permanent pull-request and `main` verification workflow.
- Existing: `docs/superpowers/specs/2026-08-04-stage-0-baseline-ci-design.md` — approved governing specification.
- Existing: `docs/superpowers/plans/2026-08-06-stage-0-baseline-ci.md` — this execution checklist.
- Temporary, never merged: `.github/workflows/stage0-cleanup.yml` on `ops/stage0-cleanup` — creates `v0.3.0` and removes obsolete branches after `main` is green.
- Modify only if CI proves necessary: the smallest affected source, test, configuration, or lockfile path required to restore the declared baseline.

---

### Task 1: Add the permanent CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Reference: `pyproject.toml`
- Reference: `uv.lock`
- Reference: `docs/superpowers/specs/2026-08-04-stage-0-baseline-ci-design.md`

**Interfaces:**
- Consumes: repository source, `pyproject.toml`, `uv.lock`, tests, and Ubuntu's `ffmpeg` package.
- Produces: one GitHub Actions job named `verify` that later tasks use as the merge gate.

- [ ] **Step 1: Confirm branch ancestry and scope before implementation**

Run:

```bash
git fetch origin main chore/baseline-ci
git merge-base --is-ancestor 5261eb4d864cfc50e6d59b2b05cdad7be9b45210 origin/chore/baseline-ci
git diff --name-status origin/main...origin/chore/baseline-ci
```

Expected:

- `git merge-base --is-ancestor` exits `0`.
- The diff contains only the approved design and implementation-plan documents.
- No production source file differs from `main`.

- [ ] **Step 2: Create the permanent workflow**

Create `.github/workflows/ci.yml` with exactly:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 20

    steps:
      - name: Check out repository
        uses: actions/checkout@v7

      - name: Set up Python
        uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - name: Install uv
        uses: astral-sh/setup-uv@v9
        with:
          version: "0.11.30"
          enable-cache: true
          cache-dependency-glob: uv.lock
          prune-cache: true

      - name: Install system dependencies
        run: sudo apt-get update && sudo apt-get install -y ffmpeg

      - name: Sync locked development environment
        run: uv sync --locked --dev

      - name: Run Ruff
        run: uv run --locked ruff check .

      - name: Run complete test suite
        run: uv run --locked pytest

      - name: Compile source and tests
        run: uv run --locked python -m compileall -q src tests
```

- [ ] **Step 3: Inspect the workflow for forbidden weakening**

Run:

```bash
grep -nE 'contents: read|python-version: "3.12"|uv sync --locked --dev|ruff check|pytest|compileall' .github/workflows/ci.yml
! grep -nE 'continue-on-error|pytest .*--ignore|pytest .*--deselect|contents: write' .github/workflows/ci.yml
```

Expected:

- The first command prints every required gate.
- The second command exits `0` and prints nothing.

- [ ] **Step 4: Run the equivalent checks locally when network access permits**

Run:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked pytest | tee /tmp/crxte-pytest.log
uv run --locked python -m compileall -q src tests
git diff --exit-code -- uv.lock
```

Expected:

- Locked synchronization succeeds without modifying `uv.lock`.
- Ruff passes.
- The complete suite passes.
- The pytest summary is preserved in `/tmp/crxte-pytest.log`.
- Compilation exits `0`.

When the local sandbox cannot reach the package index, record that constraint and continue to the GitHub-hosted PR run. Do not weaken the workflow.

- [ ] **Step 5: Commit only the permanent workflow**

Run:

```bash
git add .github/workflows/ci.yml
git commit -m "ci: establish crXte baseline verification"
git push origin chore/baseline-ci
```

Expected:

- The existing design and plan commits remain separate.
- The new commit contains only `.github/workflows/ci.yml`.
- `git diff --name-only origin/main...HEAD` contains documentation and CI only, unless a later deterministic baseline correction is required.

---

### Task 2: Open the Stage 0 pull request and make the baseline green

**Files:**
- Inspect: `.github/workflows/ci.yml`
- Modify only if required by deterministic CI evidence: the smallest affected source, test, configuration, or lockfile file.

**Interfaces:**
- Consumes: Task 1 branch and the `verify` job.
- Produces: a green pull request with the exact pytest summary and no intentional product behavior changes.

- [ ] **Step 1: Open the pull request**

Use:

```text
Title: ci: establish crXte v0.3.0 baseline
Head: chore/baseline-ci
Base: main
```

Use this body:

```markdown
## Summary

- adds permanent Python 3.12 CI for crXte
- installs the locked development environment with `uv`
- runs Ruff, the complete pytest suite, and bytecode compilation
- documents the approved Stage 0 design and implementation plan

## Baseline

The immutable `v0.3.0` tag will point to:

`5261eb4d864cfc50e6d59b2b05cdad7be9b45210`

## Verification commands

- `uv sync --locked --dev`
- `uv run --locked ruff check .`
- `uv run --locked pytest`
- `uv run --locked python -m compileall -q src tests`

## Scope

No application behavior is intentionally changed. Any correction added after the initial CI run will be limited to a deterministic reproducibility defect exposed by the declared environment.
```

- [ ] **Step 2: Inspect the first PR workflow run**

Retrieve the workflow run, job, steps, and logs for the pull-request head commit.

Expected:

- Workflow name: `CI`.
- Job name: `verify`.
- Every setup and verification step succeeds, or the logs identify one deterministic failing gate.

- [ ] **Step 3: Classify any failure before editing**

Use this decision table:

```text
Package-index or transient network failure -> rerun once; make no repository change.
Lockfile mismatch -> regenerate only the required lock entries under the existing dependency bounds; commit separately.
Missing Ubuntu package -> add only that apt package to ci.yml; commit separately.
Ruff violation -> make the smallest non-behavioral correction; commit separately.
Test failure -> reproduce the exact test, determine environment/test/application cause, and make the smallest behavior-preserving correction; commit separately.
Compile failure -> fix the exact syntax or encoding defect; commit separately.
```

Tests must not be skipped, ignored, deselected, or allowed to fail.

- [ ] **Step 4: Verify every correction independently**

Run the narrow failing command first, then all gates:

```bash
uv run --locked ruff check .
uv run --locked pytest | tee /tmp/crxte-pytest.log
uv run --locked python -m compileall -q src tests
```

Expected:

- The narrow failure is resolved.
- All complete gates pass.
- `git diff` shows no unrelated cleanup.

- [ ] **Step 5: Commit each required correction separately**

Choose the one commit message matching the evidence:

```bash
git commit -m "build: repair locked baseline environment"
git commit -m "test: make baseline test deterministic"
git commit -m "fix: preserve baseline behavior on Python 3.12"
```

Do not combine unrelated failure classes.

- [ ] **Step 6: Record exact verification evidence**

From the successful CI log, copy the pytest terminal summary line verbatim. It will contain the observed integer count and timing, for example the same structural form as `80 passed in 4.21s`; the actual CI text, not this example, is authoritative.

Add a PR comment with exactly these fixed lines plus the copied pytest summary:

```markdown
## Final verification

- Locked sync: PASS
- Ruff: PASS
- Pytest: PASS
- Pytest summary: [paste the successful CI summary line verbatim]
- Compileall: PASS
- Application behavior intentionally changed: No
- Baseline corrections: None.
```

When a correction was required, replace only the final line with a bullet for each correction commit in this form:

```markdown
- Baseline correction `FULL_COMMIT_SHA`: concise evidence-based reason.
```

The implementation agent must substitute the real full commit SHA and the actual reason from the CI failure; no estimate or abbreviated SHA is accepted.

- [ ] **Step 7: Merge only after the final PR head is green**

Confirm:

```text
PR state: open
PR mergeability: mergeable
Latest CI conclusion: success
Changed files: design, plan, ci.yml, and only documented reproducibility corrections when present
```

Merge using the repository's enabled merge method while preserving the pull-request record.

---

### Task 3: Verify `main`, create `v0.3.0`, and remove obsolete branches

**Files:**
- Temporary create on `ops/stage0-cleanup`: `.github/workflows/stage0-cleanup.yml`
- Permanent files modified: none.

**Interfaces:**
- Consumes: merged green `main` and baseline commit `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`.
- Produces: annotated tag `v0.3.0`, deletion of every obsolete setup branch, and no cleanup workflow on `main`.

- [ ] **Step 1: Confirm the post-merge `main` workflow is green**

Fetch the new `main` head and its push-triggered `CI` run.

Expected:

- The run concludes `success`.
- Ruff, complete pytest, and compileall pass.
- The exact pytest summary is copied verbatim into the completion record.

Do not perform tag or branch cleanup before this succeeds.

- [ ] **Step 2: Create a temporary operations branch from verified `main`**

Run:

```bash
git fetch origin main
git switch --create ops/stage0-cleanup origin/main
```

Expected:

- The branch starts at verified `main`.
- It is never opened as a pull request and never merged.

- [ ] **Step 3: Add the one-time cleanup workflow**

Create `.github/workflows/stage0-cleanup.yml` on `ops/stage0-cleanup` with exactly:

```yaml
name: Stage 0 cleanup

on:
  push:
    branches: [ops/stage0-cleanup]

permissions:
  contents: write

jobs:
  cleanup:
    if: github.repository == '051-lab/crXte'
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Check out repository history
        uses: actions/checkout@v7
        with:
          fetch-depth: 0

      - name: Create baseline tag and remove obsolete branches
        shell: bash
        env:
          BASELINE_SHA: 5261eb4d864cfc50e6d59b2b05cdad7be9b45210
        run: |
          set -euo pipefail

          git config user.name 'github-actions[bot]'
          git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
          git fetch origin --tags --force

          if git show-ref --verify --quiet refs/tags/v0.3.0; then
            actual="$(git rev-list -n 1 v0.3.0)"
            test "$actual" = "$BASELINE_SHA"
          else
            git cat-file -e "$BASELINE_SHA^{commit}"
            git tag -a v0.3.0 "$BASELINE_SHA" -m 'Verified imported crXte v0.3.0 baseline'
            git push origin refs/tags/v0.3.0
          fi

          for branch in \
            source-import-final \
            finalize-source-import \
            chore/baseline-ci \
            chore/baseline-ci-plan \
            chore/baseline-ci-temp \
            chore/baseline-ci-doc \
            chore/baseline-ci-impl
          do
            if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
              git push origin --delete "$branch"
            fi
          done

          git push origin --delete ops/stage0-cleanup
```

The `chore/baseline-ci-*` branches are accidental setup branches and are explicitly retired here so the final branch model still matches the approved design.

- [ ] **Step 4: Commit and push the temporary workflow**

Run:

```bash
git add .github/workflows/stage0-cleanup.yml
git commit -m "chore: finalize Stage 0 repository refs"
git push origin ops/stage0-cleanup
```

Expected:

- The push starts `Stage 0 cleanup`.
- The workflow exists only on the temporary branch.
- No pull request is opened.

- [ ] **Step 5: Inspect cleanup logs**

Required evidence:

- `v0.3.0` is created or verified at the exact baseline commit.
- Every listed obsolete branch is deleted when present.
- `ops/stage0-cleanup` deletes itself.
- The job concludes `success`.

If self-deletion is the only rejected operation, run this authenticated fallback once:

```bash
git push origin --delete ops/stage0-cleanup
```

The cleanup workflow must never be merged into `main`.

---

### Task 4: Prove final repository state and close Stage 0

**Files:**
- Inspect: `.github/workflows/ci.yml` on `main`.
- Inspect: tag `v0.3.0`.
- Inspect: repository branch list.
- Inspect: merged Stage 0 pull request.

**Interfaces:**
- Consumes: Task 3 refs and Task 2 CI evidence.
- Produces: a verified Stage 0 completion record and a clean starting point for the Export Fidelity design cycle.

- [ ] **Step 1: Verify the tag target exactly**

Run:

```bash
git fetch origin --tags --force
test "$(git rev-list -n 1 v0.3.0)" = "5261eb4d864cfc50e6d59b2b05cdad7be9b45210"
git tag -n99 v0.3.0
```

Expected:

- The comparison exits `0`.
- The tag message identifies the verified imported baseline.

- [ ] **Step 2: Verify branch cleanup**

Run:

```bash
git ls-remote --heads origin
```

Expected output contains exactly one branch ref:

```text
refs/heads/main
```

No import, baseline, planning, implementation, or operations branch remains.

- [ ] **Step 3: Verify permanent CI and absence of maintenance artifacts**

Run:

```bash
git fetch origin main
git show origin/main:.github/workflows/ci.yml >/dev/null
! git cat-file -e origin/main:.github/workflows/stage0-cleanup.yml 2>/dev/null
! git ls-tree -r --name-only origin/main | grep -E '(^|/)(\.import|\.source-import)(/|$)|import-clean-source|finalize-source-import'
```

Expected:

- Permanent CI exists.
- Temporary cleanup workflow is absent from `main`.
- No importer directory or importer workflow exists.

- [ ] **Step 4: Publish the completion evidence**

Report the exact observed values:

```text
Baseline tag target: 5261eb4d864cfc50e6d59b2b05cdad7be9b45210
PR CI conclusion: success
Post-merge main CI conclusion: success
Ruff: pass
Pytest: pass
Pytest summary: copy the successful main-run summary line verbatim
Compileall: pass
Remaining branches: main
Intentional application behavior changes: none
```

- [ ] **Step 5: Confirm the Stage 0 completion gate**

Stage 0 is complete only when all conditions are simultaneously true:

```text
v0.3.0 resolves to the exact baseline commit
main contains .github/workflows/ci.yml
latest main CI run is green
only main remains as a branch
no temporary cleanup workflow is on main
no import artifacts are on main
complete pytest summary is documented verbatim
no product behavior was intentionally changed
```

- [ ] **Step 6: Begin the next design cycle without creating product code**

Use `main` as the source of truth and begin the separate Export Fidelity brainstorming and specification process. Do not create `feature/export-fidelity` or modify production code until that design is approved.
