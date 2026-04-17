---
name: flow-dev
description: Use when working in the flow repository and you need repo-specific development commands, especially where the Python virtual environment lives and how to run tests here.
---

# Flow Dev

- The project-local Python virtual environment is at `.venv` in the repo root.
- Prefer running Python tooling through the venv directly:
  - `.venv/bin/python`
  - `.venv/bin/pytest`
- If you activate it first, use `. .venv/bin/activate`.
- For this repo, the standard dev setup is:
  - `python -m venv .venv`
  - `. .venv/bin/activate`
  - `python -m pip install -e ".[dev]"`
- When `pytest` is not available on `PATH`, that usually just means the venv is not active. Run `.venv/bin/pytest` instead.
- PyPI publishing for this repo is triggered by pushing a git tag that matches `v*` (`.github/workflows/workflow.yml`).
- Before cutting a release tag, keep the package version in sync in:
  - `pyproject.toml`
  - `src/flow/__init__.py`
- Confirm the intended release commit is the one that bumped the version, then create an annotated tag on that commit:
  - `git log --oneline -- pyproject.toml src/flow/__init__.py`
  - `version=$(.venv/bin/python - <<'PY'\nimport tomllib\nfrom pathlib import Path\nprint(tomllib.loads(Path('pyproject.toml').read_text())['project']['version'])\nPY\n)`
  - `git tag -a "v${version}" -m "Release v${version}" <release-commit>`
- Push the branch and the tag explicitly so the publish workflow runs even if the branch was already pushed:
  - `git push origin main`
  - `git push origin "v${version}"`
- If the version bump is already on `main` but PyPI did not publish, recover by tagging the existing version-bump commit and pushing just that missing tag:
  - `git tag -a vX.Y.Z -m "Release vX.Y.Z" <release-commit>`
  - `git push origin vX.Y.Z`
