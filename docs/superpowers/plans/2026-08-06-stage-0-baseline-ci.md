# crXte Stage 0 Baseline and CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish a reproducible, CI-gated crXte `v0.3.0` baseline on `main` without changing application behavior.

**Architecture:** Add one permanent read-only GitHub Actions workflow that installs the locked Python 3.12 environment, runs Ruff, runs the complete pytest suite, and compiles `src` and `tests`. After the CI pull request merges and `main` is green, use one temporary unmerged operations branch to create the annotated baseline tag and remove all obsolete setup branches, including the temporary operations branch itself.

**Tech Stack:** GitHub Actions, Ubuntu, Python 3.12, `uv`, Ruff, pytest, `ffmpeg`, Git refs.

## Global Constraints

- Baseline commit: `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`.
- Baseline tag: immutable annotated tag `v0.3.0` pointing exactly to the baseline commit.
- Runtime target: WSL/Linux with Python 3.12 or newer.
- CI runner: `ubuntu-latest` with Python `3.12` only.
- Dependencies: install with `uv sync --locked --dev`; do not rewrite `uv.lock` silently.
- Required gates: Ruff, the complete pytest suite, and `compileall` for `src` and `tests`.
- No test exclusions, allowed failures, application behavior changes, UI changes, export changes, refactors, dependency upgrades, packaging, deployment, coverage reporting, or Python-version matrix.
- Permanent workflow permissions: `contents: read` only.
- Long-lived branch after cleanup: `main` only.
- Any reproducibility correction must be the smallest possible separate commit and must preserve intended behavior.
- Current official action majors verified on 2026-08-06: `actions/checkout@v7`, `actions/setup-python@v7`, and `astral-sh/setup-uv@v9`.

---

## File Map

- Create: `.github/workflows/ci.yml` — permanent pull-request and `main` verification workflow.
- Existing: `docs/superpowers/specs/2026-08-04-stage-0-baseline-ci-design.md` — approved governing specification.
- Existing: `docs/superpowers/plans/2026-08-06-stage-0-baseline-ci.md` — this execution checklist.
- Temporary, never merged: `.github/workflows/stage0-cleanup.yml` on branch `ops/stage0-cleanup` — creates `v0.3.0` and removes obsolete branches after `main` is green.
- Modify only if CI proves necessary: the smallest affected source, test, configuration, or lockfile path required to restore the declared baseline.

---

### Task 1: Add the permanent CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Reference: `pyproject.toml`
- Reference: `uv.lock`
- Reference: `docs/superpowers/specs/2026-08-04-stage-0-baseline-ci-design.md`

**Interfaces:**
- Consumes: repository source, `pyproject.toml`, `uv.lock`, tests, and the Ubuntu `ffmpeg` package.
- Produces: one GitHub Actions job named `verify` that later tasks use as the merge gate.

- [ ] **Step 1: Confirm the implementation branch still descends from the approved baseline**

Run:

```bash
git fetch origin main chore/baseline-ci
git merge-base --is-ancestor 5261eb4d864cfc50e6d59b2b05cdad7be9b45210 origin/chore/baseline-ci
git diff --name-status origin/main...origin/chore/baseline-ci
```

Expected:

- `git merge-base --is-ancestor` exits `0`.
- The diff contains only the approved design and plan documents before CI is added.
- No production source file differs from `main`.

- [ ] **Step 2: Create the permanent workflow exactly as specified**

Create `.github/workflows/ci.yml` with:

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

- [ ] **Step 3: Review the workflow for Stage 0 constraints**

Check:

```bash
grep -nE 'contents: read|python-version: "3.12"|uv sync --locked --dev|ruff check|pytest|compileall' .github/workflows/ci.yml
! grep -nE 'continue-on-error|pytest .*--ignore|pytest .*--deselect|permissions: write|contents: write' .github/workflows/ci.yml
```

Expected:

- The first command prints every required gate.
- The second command exits `0` and prints nothing.

- [ ] **Step 4: Run the same checks locally when the environment permits**

Run:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked pytest
uv run --locked python -m compileall -q src tests
```

Expected:

- Dependency synchronization does not modify `uv.lock`.
- Ruff passes.
- The complete pytest suite passes and prints an exact test count.
- Compilation exits `0`.

If dependency installation is blocked only by the local sandbox network, record that limitation and continue to the GitHub-hosted PR run. Do not weaken the workflow.

- [ ] **Step 5: Commit the permanent workflow**

Run:

```bash
git add .github/workflows/ci.yml docs/superpowers/specs/2026-08-04-stage-0-baseline-ci-design.md docs/superpowers/plans/2026-08-06-stage-0-baseline-ci.md
git commit -m "ci: establish crXte baseline verification"
git push origin chore/baseline-ci
```

Expected:

- One focused commit contains the design, plan, and CI workflow.
- `git diff --name-only origin/main...HEAD` lists no production application files.

---

### Task 2: Open the Stage 0 pull request and make the baseline green

**Files:**
- Inspect: `.github/workflows/ci.yml`
- Modify only if required by deterministic CI evidence: the smallest affected source, test, configuration, or lockfile file.

**Interfaces:**
- Consumes: Task 1 branch and the `verify` job.
- Produces: a green pull request with an exact test count and no intentional behavior changes.

- [ ] **Step 1: Open the pull request**

Use:

```text
Title: ci: establish crXte v0.3.0 baseline
Head: chore/baseline-ci
Base: main
```

Use this pull-request body:

```markdown
## Summary

- adds permanent Python 3.12 CI for crXte
- installs the locked development environment with `uv`
- runs Ruff, the complete pytest suite, and bytecode compilation
- documents the approved Stage 0 design and implementation plan

## Baseline

The immutable `v0.3.0` tag will point to:

`5261eb4d864cfc50e6d59b2b05cdad7be9b45210`

## Verification

- `uv sync --locked --dev`
- `uv run --locked ruff check .`
- `uv run --locked pytest`
- `uv run --locked python -m compileall -q src tests`

## Scope

No application behavior is intentionally changed. Any correction added after the initial CI run will be limited to a deterministic reproducibility defect exposed by the declared environment.
```

- [ ] **Step 2: Inspect the first PR workflow run**

Retrieve the run, jobs, and logs for the pull-request head commit.

Expected:

- Workflow: `CI`.
- Job: `verify`.
- Every setup and verification step completes successfully, or the logs identify one deterministic failing gate.

- [ ] **Step 3: Classify any failure before editing**

Use this decision table:

```text
Package-index/network failure -> rerun once; make no repository change.
Lockfile mismatch -> regenerate only the required lockfile entries with the same declared dependency bounds; commit separately.
Missing Ubuntu package -> add only the required apt package to the workflow; commit separately.
Ruff violation -> make the smallest non-behavioral correction; commit separately.
Test failure -> reproduce the exact test, identify environment/test/application cause, then make the smallest behavior-preserving correction; commit separately.
Compile failure -> fix the exact syntax or encoding defect; commit separately.
```

Do not skip, deselect, ignore, or allow any failing test.

- [ ] **Step 4: Verify each correction independently**

For every correction, run the narrowest failing command first, then the complete gates:

```bash
uv run --locked ruff check .
uv run --locked pytest
uv run --locked python -m compileall -q src tests
```

Expected:

- The narrow failure is resolved.
- All complete gates pass.
- `git diff` shows no unrelated cleanup.

- [ ] **Step 5: Commit each required correction separately**

Use one of these exact commit forms according to the evidence:

```bash
git commit -m "build: repair locked baseline environment"
git commit -m "test: make baseline test deterministic"
git commit -m "fix: preserve baseline behavior on Python 3.12"
```

Do not combine unrelated failure classes in one correction commit.

- [ ] **Step 6: Record final verification evidence in the pull request**

Update the PR body or add a top-level comment containing:

```markdown
## Final verification

- Locked sync: PASS
- Ruff: PASS
- Pytest: PASS — `<exact count>` tests
- Compileall: PASS
- Application behavior intentionally changed: No
- Baseline corrections: `<none, or exact commit and reason>`
```

Replace the bracketed fields with observed evidence; do not estimate the test count.

- [ ] **Step 7: Merge only after the final PR head is green**

Before merging, confirm:

```text
PR state: open
PR mergeability: mergeable
Latest CI conclusion: success
Changed files: design, plan, ci.yml, plus only documented reproducibility corrections if any
```

Merge using squash or merge commit according to the repository default, preserving the PR record.

---

### Task 3: Verify `main`, create `v0.3.0`, and remove obsolete branches

**Files:**
- Temporary create on `ops/stage0-cleanup`: `.github/workflows/stage0-cleanup.yml`
- Permanent files modified: none.

**Interfaces:**
- Consumes: merged green `main` from Task 2 and baseline commit `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`.
- Produces: annotated tag `v0.3.0`, deletion of every obsolete setup branch, and no cleanup workflow on `main`.

- [ ] **Step 1: Confirm the post-merge `main` workflow is green**

Fetch the `main` head commit and its `CI` workflow run.

Expected:

- The push-triggered `CI` run concludes `success`.
- Ruff, complete pytest, and compileall all pass on `main`.
- Record the exact pytest count again.

Do not perform tag or branch cleanup before this succeeds.

- [ ] **Step 2: Create the temporary operations branch from verified `main`**

Run:

```bash
git fetch origin main
git switch --create ops/stage0-cleanup origin/main
```

Expected:

- `ops/stage0-cleanup` begins at the verified `main` commit.
- The branch is not intended for a pull request or merge.

- [ ] **Step 3: Add the one-time cleanup workflow exactly as specified**

Create `.github/workflows/stage0-cleanup.yml` on `ops/stage0-cleanup` with:

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

The extra `chore/baseline-ci-*` branches are accidental setup branches and must not survive Stage 0.

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

- [ ] **Step 5: Inspect the cleanup workflow logs**

Expected evidence:

- `v0.3.0` is created or verified at the exact baseline commit.
- Every listed obsolete branch is deleted when present.
- `ops/stage0-cleanup` deletes itself at the end.
- The job concludes `success`.

If self-deletion is rejected while all other operations succeed, run this authenticated fallback once:

```bash
git push origin --delete ops/stage0-cleanup
```

Do not merge the cleanup workflow into `main`.

---

### Task 4: Prove final repository state and close Stage 0

**Files:**
- Inspect: `.github/workflows/ci.yml` on `main`.
- Inspect: tag `v0.3.0`.
- Inspect: repository branch list.
- Inspect: merged Stage 0 pull request.

**Interfaces:**
- Consumes: Task 3 repository refs and Task 2 CI evidence.
- Produces: a verified Stage 0 completion record and a clean starting point for the export-fidelity design cycle.

- [ ] **Step 1: Verify the tag target exactly**

Run:

```bash
git fetch origin --tags --force
test "$(git rev-list -n 1 v0.3.0)" = "5261eb4d864cfc50e6d59b2b05cdad7be9b45210"
git tag -n99 v0.3.0
```

Expected:

- The comparison exits `0`.
- The tag message identifies the verified imported crXte baseline.

- [ ] **Step 2: Verify branch cleanup**

Run:

```bash
git ls-remote --heads origin
```

Expected:

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
- Temporary cleanup workflow does not exist on `main`.
- No importer directory or importer workflow exists.

- [ ] **Step 4: Verify final CI evidence**

Record:

```text
PR CI conclusion: success
Post-merge main CI conclusion: success
Ruff: pass
Pytest: pass, exact count recorded
Compileall: pass
Intentional application behavior changes: none
```

- [ ] **Step 5: Confirm Stage 0 completion**

Stage 0 is complete only when all of the following are simultaneously true:

```text
v0.3.0 -> 5261eb4d864cfc50e6d59b2b05cdad7be9b45210
main contains .github/workflows/ci.yml
latest main CI run is green
only main remains as a branch
no temporary cleanup workflow is on main
no import artifacts are on main
complete pytest count is documented
no product behavior was intentionally changed
```

- [ ] **Step 6: Begin the next design cycle without creating product code yet**

Use `main` as the source of truth and start the separate Export Fidelity brainstorming/specification process. Do not create `feature/export-fidelity` or modify production code until that design is approved.
