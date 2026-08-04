# crXte Stage 0 Baseline and CI Design

**Status:** Approved design, awaiting final written-spec review  
**Date:** 2026-08-04  
**Repository:** `051-lab/crXte`  
**Implementation branch:** `chore/baseline-ci`  
**Current clean-source baseline commit:** `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`

## 1. Purpose

Stage 0 creates a trustworthy development baseline before any crXte behavior changes. It establishes one permanent automated verification path, records the imported source as version `v0.3.0`, and removes obsolete import branches after their history is no longer needed as active branch state.

This stage does not modify export behavior, the user interface, storage layout, or runtime features.

## 2. Goals

Stage 0 must:

1. Preserve the current clean imported source at commit `5261eb4d864cfc50e6d59b2b05cdad7be9b45210` with the tag `v0.3.0`.
2. Add a permanent GitHub Actions workflow at `.github/workflows/ci.yml`.
3. Run the locked development environment on Python 3.12 in Ubuntu.
4. Verify Ruff, the complete pytest suite, and Python bytecode compilation.
5. Merge the CI change through a pull request into `main`.
6. Delete the obsolete branches `source-import-final` and `finalize-source-import` after the baseline tag exists and the CI pull request is merged.
7. Leave `main` as the only long-lived branch.
8. Create future work from short-lived, purpose-specific branches.

## 3. Non-goals

Stage 0 will not:

- change crXte application behavior;
- refactor production code;
- alter dependencies or regenerate `uv.lock` unless the existing lockfile cannot install as written;
- add coverage reporting, packaging publication, release binaries, or deployment automation;
- add a multi-version Python test matrix;
- add a permanent `develop` branch;
- change the repository-level software-license status;
- begin the export-fidelity implementation.

Any source or lockfile defect exposed by CI will be documented and fixed in the smallest separate commit required to make the existing baseline reproducible. Such a fix must not add new product behavior.

## 4. Repository state and branch disposition

At design time the repository contains:

- `main` — complete clean crXte source;
- `source-import-final` — identical to `main` and no longer needed;
- `finalize-source-import` — obsolete import-recovery history and no longer needed;
- `chore/baseline-ci` — Stage 0 documentation and implementation branch.

The two import branches are deleted only after:

1. `v0.3.0` points to the clean-source baseline commit;
2. the Stage 0 CI pull request has merged successfully;
3. `main` has passed the permanent CI workflow.

Deleting the branch references does not delete the referenced commits from Git history immediately; the baseline commit remains permanently named by `v0.3.0`.

## 5. CI workflow design

### 5.1 File

```text
.github/workflows/ci.yml
```

### 5.2 Triggers

The workflow runs for:

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
```

Manual dispatch is not required for Stage 0 because the workflow can be exercised by the implementation pull request and every later pull request.

### 5.3 Permissions

The workflow uses read-only repository contents permission:

```yaml
permissions:
  contents: read
```

No secrets, write token, deployment permission, or release permission is required.

### 5.4 Concurrency

Superseded runs for the same branch or pull request are cancelled:

```yaml
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

This prevents stale runs from consuming time while retaining independent runs for different branches.

### 5.5 Runner and language

- Runner: `ubuntu-latest`
- Python: `3.12`
- Package manager: `uv`

Python 3.12 is the sole Stage 0 target because it is the project's declared minimum version and matches the current WSL/Linux product target. A compatibility matrix can be introduced only when crXte intentionally supports and tests additional Python versions.

### 5.6 System dependencies

The workflow installs:

```text
ffmpeg
```

The Ubuntu `ffmpeg` package also provides `ffprobe`, both of which are runtime requirements checked by crXte.

### 5.7 Dependency installation

The workflow installs `uv` through the maintained `astral-sh/setup-uv` GitHub Action and then runs:

```bash
uv sync --locked --dev
```

Requirements:

- `--locked` must fail if `uv.lock` is inconsistent with `pyproject.toml`;
- dependency resolution must not silently rewrite `uv.lock`;
- development dependencies must be installed so Ruff and pytest are available.

### 5.8 Verification sequence

The job runs these gates in order:

```bash
uv run --locked ruff check .
uv run --locked pytest
python -m compileall -q src tests
```

The complete pytest suite is mandatory. No test file is ignored, deselected, or marked as allowed to fail during Stage 0.

`compileall` runs after tests so syntax and importable bytecode coverage is explicitly verified for both production code and tests. Generated `__pycache__` directories exist only in the ephemeral runner and are never committed.

## 6. Failure handling

### 6.1 Dependency failure

If `uv sync --locked --dev` fails:

1. identify whether the issue is a transient package-index outage or a reproducible lockfile defect;
2. rerun only to distinguish transient infrastructure failure from deterministic failure;
3. for a deterministic lockfile defect, make the smallest lockfile-only correction needed and explain it in the pull request;
4. do not upgrade unrelated dependencies.

### 6.2 Ruff failure

Existing Ruff violations are fixed without behavior changes. Formatting or cleanup must be limited to the files necessary for a passing baseline.

### 6.3 Test failure

A failing test is treated as evidence that the imported source is not reproducible in the declared environment. The implementation must determine whether the failure is:

- an invalid test assumption;
- an environment dependency missing from CI;
- a genuine application defect;
- a network-dependent test that should use a fixture or mock.

The fix must preserve current intended behavior and include a clear pull-request explanation. Tests are not skipped merely to make CI green.

### 6.4 Compile failure

A compilation failure blocks merge. The exact syntax or encoding defect must be corrected before proceeding.

## 7. Pull-request structure

The Stage 0 pull request should contain only:

1. this approved design document;
2. `.github/workflows/ci.yml`;
3. narrowly required baseline corrections, only if the new CI exposes a reproducibility defect;
4. an optional short README development note only when the CI command differs materially from the existing documented commands.

The pull request description must report:

- the baseline commit and intended `v0.3.0` tag target;
- every CI command executed;
- whether the complete pytest suite passed and the exact test count;
- any baseline correction made;
- confirmation that no application behavior was intentionally changed.

## 8. Tagging design

The tag name is:

```text
v0.3.0
```

It must point exactly to:

```text
5261eb4d864cfc50e6d59b2b05cdad7be9b45210
```

The tag records the clean imported application before the permanent CI workflow is added. It is not moved later.

An annotated tag is preferred. Its message should identify it as the verified imported crXte baseline.

## 9. Merge and cleanup order

The implementation order is fixed:

1. Confirm `chore/baseline-ci` starts from the baseline commit.
2. Commit this design document.
3. Add `.github/workflows/ci.yml`.
4. Open a pull request targeting `main`.
5. Let CI run the complete verification sequence.
6. Correct only reproducibility defects revealed by CI.
7. Confirm the final pull-request run is green.
8. Create annotated tag `v0.3.0` at commit `5261eb4d864cfc50e6d59b2b05cdad7be9b45210`.
9. Merge the Stage 0 pull request into `main`.
10. Confirm the post-merge `main` workflow is green.
11. Delete `source-import-final`.
12. Delete `finalize-source-import`.
13. Confirm the remaining branches are `main` plus no active feature branch.
14. Begin the export-fidelity design and implementation cycle from the verified `main` branch.

## 10. Verification evidence required for completion

Stage 0 is complete only with evidence of all of the following:

- `v0.3.0` resolves to the exact baseline commit;
- the CI workflow exists on `main`;
- the final pull-request CI run succeeded;
- the post-merge `main` CI run succeeded;
- Ruff passed;
- the complete pytest suite passed, with the exact test count recorded;
- `compileall` passed for `src` and `tests`;
- no test exclusions were introduced;
- no importer workflow or importer directory returned to `main`;
- the two obsolete import branches no longer exist;
- the production application behavior remains unchanged.

## 11. Development model after Stage 0

After Stage 0, crXte uses:

```text
main
└── short-lived purpose-specific branch
    └── pull request
        └── merge after CI
```

The first product branch will be:

```text
feature/export-fidelity
```

That work begins with a regression fixture reproducing the previously observed missing X Article code and command blocks, followed by source-to-output completeness accounting. It does not begin until Stage 0 is complete.

## 12. Acceptance criteria

The design is accepted when the implementation produces a green, reproducible baseline without changing product behavior and leaves the repository with one clean long-lived branch, a permanent CI gate, and an immutable `v0.3.0` source reference.
