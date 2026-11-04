# Mimir has moved

Mimir now lives at **[SquashMerge/mimir](https://github.com/SquashMerge/mimir)**.

This repository is only a pointer. Issues, pull requests, releases and development
all happen in the new repository.

## Update an existing clone

```bash
git remote set-url origin https://github.com/SquashMerge/mimir.git
git fetch origin
```

SquashMerge/mimir starts from a fresh history (a single import commit), so an old
clone's branches do not share ancestry with the new `main`. Re-clone, or check out
`origin/main` explicitly rather than merging.

## Installing

The package name is unchanged:

```bash
pip install mimir-agent
```

Releases from 1.0.0 on are built and published from SquashMerge/mimir.

## Overrides that named this repository

If you set `MIMIR_GIT_URL`, pinned a `git+https://github.com/jasoncarreira/mimir`
install URL, or referenced this repository in a Dockerfile, point it at
`https://github.com/SquashMerge/mimir.git` instead.
