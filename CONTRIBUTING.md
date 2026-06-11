# Contributing

Thanks for your interest in improving Unstoppable Archive.

## Development setup

All development happens inside Docker — you don't need a local Python toolchain.

```bash
make dev          # start the hot-reload dev stack (app + worker + postgres)
```

The app is served at http://localhost:8000.

## Quality gates

Every change must pass the same gates CI enforces:

```bash
make lint         # ruff (E,W,F,UP,B,SIM,I,N,C4,RUF,S) — must be clean
make typecheck    # pyright strict — zero errors
make test         # pytest + coverage — must stay >= 95%
make all          # lint + typecheck + test
make fmt          # auto-format (run before committing)
```

These run against your live working tree (the `make` targets bind-mount the
source), so edits are reflected without a rebuild.

Standards:

- **Python 3.12**, type-annotated, `beartype` on public functions.
- **Coverage >= 95%** (branch coverage). New code needs tests.
- **pyright strict, zero errors.** Untyped third-party cascades are handled with
  narrow `cast()`s or per-file pragmas — don't disable rules globally.
- Keep it simple and readable; match the style of the surrounding file.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/) are enforced via
pre-commit. Install the hooks once:

```bash
pip install pre-commit && pre-commit install
```

Examples: `feat: add X`, `fix: handle Y`, `test: cover Z`, `chore: ...`.

## Pull requests

1. Branch off `main`.
2. Make the change with tests; keep `make all` green.
3. Open a PR describing the what and why. CI runs the gates on your PR.

## Reporting bugs / security issues

- Functional bugs: open a GitHub issue with reproduction steps.
- Security vulnerabilities: see [SECURITY.md](SECURITY.md) — report privately,
  not via a public issue.
