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
