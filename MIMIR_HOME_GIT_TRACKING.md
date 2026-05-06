# Spec: Replace VirtioFS bind-mount /mimir-home with Docker volume + git-tracked subset

<!-- desc: Docker-internal volume for /mimir-home + git-tracked human-readable subset; replaces VirtioFS bind-mount -->

**Status:** 2026-05-06 — ready for implementation. Open questions resolved by operator (§"Locked answers"). Companion to `BIND_MOUNT_HEALTH_PROBE.md` (current mitigation, PR #23).

> **Addendum (PR 4d, 2026-05-06):** the in-URL token approach
> originally specified for `inject_token_into_url` (§"Authentication
> + token rotation" / §"Skeleton") was replaced with a git
> credential-helper plumbing in `mimir/git_bootstrap.py`. The PAT
> now lives in `<home>/.git/credentials` (chmod 600) and the remote
> URL is stored in `.git/config` in the canonical clean form. `git
> remote -v` no longer leaks the token. Bootstrap detects and
> migrates a legacy PR4b-shape in-URL token on existing repos.
> Sections below describe the original design; the implementation
> diverges in this one place.

## Problem

The current container layout bind-mounts a host directory
(`<mimirbot>/state/home` on the macOS host) into the guest at
`/mimir-home` via VirtioFS. This has two structural failure modes:

1. **VirtioFS stale-inode corruption.** Twice in the 24h ending
   2026-05-05 23:58 UTC, the bind-mount source became annotated with
   `//deleted` in `/proc/self/mountinfo`. Every SDK turn fails (bundled
   claude binary spawns bash with `cwd=/mimir-home`, `getcwd()` returns
   ENOENT). The second incident corrupted ~2.5h of an active 500q
   longmemeval bench. PR #23's health probe is a reactive mitigation —
   it detects the condition and self-restarts via SIGTERM-to-PID-1 —
   but it doesn't eliminate the failure.

2. **No version control on the human-readable state.** Memory blocks,
   wiki pages, prompts, the heartbeat backlog — all valuable, all
   hand-curated, all currently sitting on a host directory with no
   commit log, no branch, no recovery path if a bug clobbers a file.
   The "intentional forgetting" engine, the reflection cycle, and the
   agent's own write loop all touch these files; we lack the audit
   trail to investigate "did mimir delete this on purpose, or did
   something corrupt it?"

Both failure modes have the same underlying shape: `/mimir-home`
straddles two contracts (durable host-side state + low-level binary
storage) and is served by the worst-fit transport for either.

## Goals

- **Eliminate VirtioFS dependency for `/mimir-home`.** Use a
  Docker-internal volume (overlay2) for everything mimir writes during
  a turn. No bind-mount, no host-FS dentry cache, no stale-inode
  surface area.
- **Track the human-readable subset in git.** `memory/`, `state/wiki/`,
  `state/heartbeat-backlog.md`, `state/proposed-changes.md`, `prompts/`,
  the INDEX.md files. Commit on every turn that mutates them. Push to
  a private GitHub repo for off-host durability + audit log.
- **Keep binary/log state out of git.** atoms.db (SAGA's SQLite),
  embedding caches, `.mimir/rate_limits.json`, `logs/turns.jsonl`,
  `logs/events.jsonl`, attachments. These live in the Docker volume
  but are gitignored. Their durability story is the volume backup, not
  git.
- **Fail closed on new file types.** Allowlist `.gitignore`, not
  blocklist — when an unfamiliar file appears, the default is "don't
  commit" rather than "leak it".
- **No new operator burden.** Setup is a one-shot `mimir setup`; daily
  use requires no manual git commands. Push failures must not block
  the next turn.

## Non-goals

- **Replacing VirtioFS for `/workspace/mimir` (the source clone) or
  `/benchmark`.** Those are already first-class git repos with their
  own remote and human-driven commit cadence. PR #23's health probe
  covers their stale-inode case for as long as the host runs a
  vulnerable VirtioFS.
- **A full backup story for atoms.db.** Out of scope for this spec.
  Volume snapshotting / rsync-to-host is a separate followup.
- **Multi-writer git coordination.** Mimir is single-tenant per
  container; there is exactly one process writing to `/mimir-home` at
  a time. Multi-instance is a non-goal for v1.
- **History rewriting / squash policies.** v1 keeps a full per-turn
  commit log. Squash/rotation is a v2 concern once we see real repo
  growth.

## Architecture overview

```
Container layout (proposed):

  /mimir-home/                   ← Docker volume (overlay2, no bind-mount)
    .git/                        ← git repo init'd here; remote = jasoncarreira/mimirbot-state (private)
    memory/                      ← tracked
    prompts/                     ← tracked
    state/
      wiki/                      ← tracked
      INDEX.md                   ← tracked (auto-regen, but committed)
      heartbeat-backlog.md       ← tracked
      proposed-changes.md        ← tracked
      atoms.db                   ← gitignored (SAGA SQLite)
      atoms.db-shm, atoms.db-wal ← gitignored
      embeddings/                ← gitignored (cache)
    logs/                        ← gitignored entirely
      turns.jsonl
      events.jsonl
    attachments/                 ← gitignored entirely
    .mimir/                      ← gitignored entirely
      rate_limits.json
      oauth_credentials.json
      ...
    .gitignore                   ← allowlist (see §"Allowlist .gitignore")
    .git/hooks/pre-commit        ← secret-scan (see §"Pre-commit hook")
```

The Docker volume is configured in `docker-compose.yml` once, persists
across container rebuilds (the existing `state/` named-volume pattern),
and is **not** mounted from the host filesystem.

## Allowlist .gitignore

Block-everything-then-unblock pattern. Easier to audit than a long
blocklist; new file types fail closed (don't get committed) until an
operator decides they should be tracked.

```gitignore
# Block everything by default.
*
!*/

# Tracked: human-readable curated state.
!memory/**
!prompts/**
!state/wiki/**
!state/INDEX.md
!state/heartbeat-backlog.md
!state/proposed-changes.md

# Top-level tracked files.
!.gitignore
!README.md            # if present
!.git/hooks/pre-commit  # NB: see §"Pre-commit hook" — hook is checked
                        #     in via a different mechanism, not actually
                        #     trackable here. Listed for clarity only.

# Re-block any binary/log artifact that slipped past the include rules.
*.db
*.db-shm
*.db-wal
*.jsonl
*.log
*.tmp
*.swp
*.pyc
__pycache__/
.DS_Store
```

The trailing block-list of binary suffixes is belt-and-suspenders — if
a future change drops `atoms.db` directly into `memory/`, the include
rule for `memory/**` would otherwise pick it up. The explicit
`*.db` rule wins.

## Pre-commit secret-scan hook

Refuse the commit (exit 1) if any staged content matches a secret
pattern. Runs before every auto-commit.

```bash
#!/usr/bin/env bash
# .git/hooks/pre-commit — secret scanner
set -euo pipefail

PATTERNS=(
  'Bearer [A-Za-z0-9_\-]{20,}'
  'sk-ant-[A-Za-z0-9_\-]{20,}'
  'sk-[A-Za-z0-9]{40,}'              # OpenAI-shaped
  'ghp_[A-Za-z0-9]{30,}'              # GitHub PAT
  'gho_[A-Za-z0-9]{30,}'              # GitHub OAuth
  'AKIA[0-9A-Z]{16}'                  # AWS access key
  '"refresh_token"\s*:\s*"[^"]{20,}"'
  '"access_token"\s*:\s*"[^"]{20,}"'
  '"client_secret"\s*:\s*"[^"]{20,}"'
)

# Filename heuristics — refuse outright.
NAME_PATTERNS=(
  '*token*'
  '*credential*'
  '*.key'
  '*.pem'
  'oauth_*.json'
  'rate_limits.json'   # belt; should already be gitignored
)

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

for f in $staged; do
  for np in "${NAME_PATTERNS[@]}"; do
    case "$f" in
      $np) echo "pre-commit: refusing to commit secret-shaped filename: $f" >&2; exit 1 ;;
    esac
  done
done

# Content scan (staged blobs only — git diff --cached -U0 captures the +lines).
for pat in "${PATTERNS[@]}"; do
  if git diff --cached -U0 | grep -E "^\+" | grep -E "$pat" >/dev/null 2>&1; then
    echo "pre-commit: refusing to commit content matching: $pat" >&2
    git diff --cached -U0 | grep -E "^\+" | grep -nE "$pat" >&2 || true
    exit 1
  fi
done

exit 0
```

Hook is shipped in the container image (`docker/post-create.sh` copies
it to `.git/hooks/` after `git init`), so it can't be bypassed by an
agent that loses access to the source. A failed hook surfaces as an
algedonic `git_commit_secret_scan_blocked` event with the matched
pattern — the next turn sees it and self-corrects.

## Post-turn commit hook contract

Runs at the end of every turn that wrote anything under `/mimir-home`.
Fast, async-safe, never blocks the next turn. **Commit per turn,
push debounced** — pushes coalesce on a 60s window so a burst of
turns produces one network round-trip instead of N. Empty-porcelain
turns skip both the commit and the push.

```python
# mimir/git_tracking.py (new module)

# Module-level coordination: at most one push pending at a time, with
# a 60s debounce window since the most recent commit. Pushes coalesce
# so a burst of turns becomes one network round-trip.
_push_debounce_lock = asyncio.Lock()
_pending_push_task: asyncio.Task | None = None
_push_debounce_seconds = 60


async def commit_turn_changes(
    *,
    turn_id: str,
    trigger: str,
) -> None:
    """Commit any human-readable state changes from this turn, then
    schedule a debounced push.

    Called from agent.run_turn() in the post-message phase, after
    saga writes and after the message buffer flush. The common case
    (no memory writes this turn) is a single porcelain check that
    returns early — no commit, no push, ~5ms overhead.
    """
    # 1. Fast check — anything to commit? Most turns hit this branch.
    porcelain = await _git("status", "--porcelain")
    if not porcelain.strip():
        return  # no-op fast path; do NOT schedule a push.

    # 2. Stage everything not gitignored. -A respects .gitignore.
    await _git("add", "-A")

    # 3. Commit. Auto message references turn_id + trigger.
    msg = f"turn {turn_id} ({trigger})\n\n{_porcelain_summary(porcelain)}"
    try:
        await _git("commit", "-m", msg)
    except GitError as e:
        if "nothing to commit" in str(e):
            return  # all changes were gitignored — also skip push.
        log_event("git_commit_failed", {"err": str(e), "turn_id": turn_id})
        return

    # 4. Schedule a debounced push. Subsequent calls within the
    #    debounce window cancel the pending task and reschedule, so
    #    a burst of N commits in <60s becomes 1 push at the end.
    await _schedule_debounced_push(turn_id=turn_id)


async def _schedule_debounced_push(*, turn_id: str) -> None:
    global _pending_push_task
    async with _push_debounce_lock:
        if _pending_push_task is not None and not _pending_push_task.done():
            _pending_push_task.cancel()
        _pending_push_task = asyncio.create_task(
            _debounced_push(turn_id=turn_id)
        )


async def _debounced_push(*, turn_id: str) -> None:
    try:
        await asyncio.sleep(_push_debounce_seconds)
    except asyncio.CancelledError:
        return  # superseded by a later turn's commit; that turn owns the push.
    try:
        await asyncio.wait_for(_git("push"), timeout=30)
    except asyncio.TimeoutError:
        log_event("git_push_failed", {"reason": "timeout", "turn_id": turn_id})
    except GitError as e:
        log_event("git_push_failed", {"reason": str(e), "turn_id": turn_id})
```

Contract guarantees:
- **Empty-porcelain turns are free.** A single `git status --porcelain`
  call (~5ms) and we return. No commit, no push, no debounce
  scheduling. Most turns don't write to `memory/`.
- **Per-turn commit, debounced push.** Each turn that touches tracked
  state gets its own commit (audit-trail granularity preserved) but
  pushes coalesce on a 60s window. A burst of 10 turns over 30s
  produces 10 commits and 1 push.
- **Debounce semantics.** Each new commit cancels the prior pending
  push task and schedules a fresh 60s timer. The push fires when the
  agent goes 60s without a new tracked-state commit. On a normal
  cadence, the worst-case delay between commit and push is 60s; the
  best-case (single commit then idle) is exactly 60s.
- **Push failures never block the next turn.** They surface as
  `git_push_failed` events, which the algedonic feedback block lifts
  to the next turn's prompt as a negative signal. Local commits stand;
  the next successful debounced push catches up.
- **Pre-commit hook refusal is logged + continues.** We log
  `git_commit_secret_scan_blocked` and the change stays staged for
  operator inspection. We do NOT schedule a push for a refused commit.
- **All git invocations go through `_git()`,** which wraps
  `subprocess.run` with `cwd=/mimir-home`, `timeout=10` per call (push
  uses a different outer timeout), and returns stdout/stderr structured.

## Lock coordination

Multiple writers under `/mimir-home`:
- The agent turn loop (this is the canonical writer).
- The OAuth usage poller (writes `.mimir/rate_limits.json` every
  ~3 min). **Already gitignored** under the proposed layout.
- The reflection skill (writes `memory/core/40-learned-behaviors.md`
  and proposed-changes.md). Runs inside a turn context, so it's
  serialized through the turn loop's commit hook.
- The wiki skill, journal skill, etc. — all in-turn writes, same
  serialization.

The only race is **two concurrent turns on different channels writing
the same memory file**. The dispatcher already serializes by
channel; cross-channel races on shared files are theoretically
possible but in practice rare (memory writes happen during reflection
and explicit memory operations, both of which gate on the agent
itself).

v1 design: trust the per-channel serialization; rely on `git add -A`
+ `git commit` being internally atomic against the index. If a
cross-channel race lands two commits in quick succession, both land
and the diff is whatever each turn wrote. Race condition manifests as
"unexpected merge in log", not "lost data".

v2 (if races prove real): wrap `commit_turn_changes` in a flock on
`/mimir-home/.git/MIMIR_TURN_LOCK`. Defer until we observe an actual
conflict.

## Algedonic signal: `uncommitted_files_present`

The Self-state block currently surfaces tool-call share, plan windows,
cost rate. Add a line:

```
- uncommitted in /mimir-home: <count> file(s) — <topN paths>
```

Implementation:
- New `mimir.health.git_status_summary()` that runs
  `git -C /mimir-home status --porcelain` and parses.
- Called from the same place `## Self-state` is rendered.
- Suppressed when count == 0.
- `topN` = first 3 paths sorted by lex order; truncated with "…+N"
  suffix if >3.

This catches the case where a commit failed (secret-scan refused, push
failure during a multi-turn outage, manual operator intervention left
the tree dirty). The agent sees the line and can self-correct — read
the staging dir, identify the offender, either fix it or escalate.

## `mimir setup` flow

Auth model: HTTPS push URL embeds `GITHUB_TOKEN` at clone time so the
working copy can push without further config. `MIMIR_STATE_REPO` is
the canonical (token-free) URL for diffing/sharing; we rewrite it
into the in-container remote URL by injecting the token. Token is
sourced from `.env`, never written to disk in plaintext outside the
container's `.git/config`.

First-container-start:

```bash
# In docker/post-create.sh or equivalent entrypoint init.
set -euo pipefail

apply_git_identity() {
  # Locked answer #3 (revised 2026-05-06 msg 1501603018377007295):
  # mimir@muninnai.ai with no GitHub-account association — commits will
  # show in log without avatar attribution, which is the desired shape
  # for a non-human committer identity.
  git -C /mimir-home config user.name  "mimir"
  git -C /mimir-home config user.email "mimir@muninnai.ai"
}

inject_token_into_url() {
  # MIMIR_STATE_REPO is canonical (no creds). Rewrite to embed the PAT.
  python3 -c "
import os, urllib.parse
url = os.environ['MIMIR_STATE_REPO']
tok = os.environ['GITHUB_TOKEN']
p = urllib.parse.urlparse(url)
print(p._replace(netloc=f'{tok}@{p.hostname}').geturl())
"
}

if [ ! -d /mimir-home/.git ]; then
  if [ -n "${MIMIR_STATE_REPO:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
    # Operator has a remote — clone with token-embedded URL.
    PUSH_URL=$(inject_token_into_url)
    git clone "$PUSH_URL" /mimir-home
    apply_git_identity
    # Ensure the gitignore + hook are present even if the cloned repo
    # was empty at first commit.
    cp -n /workspace/mimir/docker/templates/.gitignore /mimir-home/ || true
    cp /workspace/mimir/docker/templates/pre-commit /mimir-home/.git/hooks/
    chmod +x /mimir-home/.git/hooks/pre-commit
  else
    # Bootstrap fresh — operator will set the remote later (or this is
    # PR 4a's pre-activation state).
    git -C /mimir-home init
    apply_git_identity
    cp /workspace/mimir/docker/templates/.gitignore /mimir-home/
    cp /workspace/mimir/docker/templates/pre-commit /mimir-home/.git/hooks/
    chmod +x /mimir-home/.git/hooks/pre-commit
    git -C /mimir-home add -A
    git -C /mimir-home commit -m "initial mimir-home bootstrap"
    if [ -n "${MIMIR_STATE_REPO:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
      PUSH_URL=$(inject_token_into_url)
      git -C /mimir-home remote add origin "$PUSH_URL"
      git -C /mimir-home push -u origin HEAD
    fi
  fi
fi
```

Subsequent-container-start:

```bash
# Volume is preserved; git repo is intact. Pull to catch any operator-
# side rebases. Never auto-resolve conflicts.
git -C /mimir-home fetch --all --tags
if ! git -C /mimir-home pull --ff-only; then
  # Conflict path: log algedonic, leave the tree as-is, let the agent
  # surface it on the first turn.
  log_event "git_pull_blocked" --reason "non-fast-forward"
fi
```

Conflict resolution policy: **operator-side rebase wins**. If the
container's local commits diverge from the remote, the agent surfaces
`git_pull_blocked` and stops auto-committing until the operator
resolves. We never `git reset --hard` from inside the container — the
agent's local commits represent real work the operator may want to
keep.

Token-rotation note: when `GITHUB_TOKEN` rotates in `.env`, the
container's `.git/config` still has the old one. `mimir setup`
re-injects on every container start (idempotent — `git remote
set-url origin "$PUSH_URL"`), so a container restart picks up the
new token. Mid-container rotation is out of scope for v1.

## Migration path from current bind-mount

This runbook executes when PR 4c lands. PRs 4a + 4b have already
shipped on the bind-mount layout, so the bind-mount source already
contains a working `.git/` repo with commits and a configured remote.
The migration is "swap the transport, copy the state, point at the
volume."

Pre-migration state (after 4a + 4b):
- `<mimirbot>/state/home/.git/` exists, has the bootstrap commit and
  whatever per-turn commits accumulated.
- `MIMIR_STATE_REPO=https://github.com/jasoncarreira/mimirbot-state.git`
  set in `.env`.
- `GITHUB_TOKEN` already in `.env`, scoped to both
  `jasoncarreira/mimirbot` and `jasoncarreira/mimirbot-state`.
- Both repos exist on GitHub (private, pre-created by operator).
  `mimirbot-state` is mimir's push target; `mimirbot` is the
  operator-side compose/README repo.

One-shot operator script:

1. **Snapshot host-side state.** `tar czf
   ~/mimirbot-state-backup-$(date +%Y%m%d).tar.gz state/home/`
   on the host. Don't ship this; just keep a local rollback.
2. **Push the latest commits.** Inside the running container, run
   `git -C /mimir-home push` to make sure GitHub has everything.
3. **Stop the container.** `docker compose down`.
4. **Create the new volume.** Edit `docker-compose.yml`: replace the
   `/mimir-home` bind-mount with a named volume (`mimir_home_data`).
5. **Seed the volume from the GitHub repo.** Cleanest path is to
   clone fresh (the volume is empty, the remote is authoritative
   after step 2). The `mimir setup` flow handles this on first
   container start (sees empty `/mimir-home`, sees `MIMIR_STATE_REPO`
   set, runs `git clone`).
   - Alternative for offline migration: `docker run --rm -v
     mimir_home_data:/dst -v $PWD/state/home:/src alpine cp -a
     /src/. /dst/` to copy from the host snapshot directly. Either
     way works; clone is simpler.
6. **First-start in new layout.** `docker compose up -d`. Container
   runs `mimir setup`; setup sees `.git` doesn't exist (or sees the
   cloned repo, depending on path 5), proceeds normally.
7. **Verify the bind-mount surface area dropped.** `cat
   /proc/self/mountinfo | grep mimir-home` should show overlay2, not
   virtiofs. PR #23's health probe should auto-disable on
   non-VirtioFS mounts (it's gated to VirtioFS today).
8. **Verify auto-commit/push works.** Trigger a memory write (e.g.
   ask the agent to update a wiki page); observe a commit on the
   container and a push to GitHub within 60s.
9. **Decommission the host bind-mount.** Once a few days of stable
   operation pass, delete `state/home/` on the host (we have the
   snapshot tarball + the GitHub remote).

## Failure modes

| # | Failure | Detection | Response |
|---|---------|-----------|----------|
| 1 | GitHub auth expired (`GITHUB_TOKEN` revoked or rotated to a value not yet picked up by the container) | `git_push_failed` algedonic with `403`/`401` from GitHub | Surface to operator alert channel. Continue committing locally; commits accumulate until auth is restored. Operator updates `.env` and restarts the container; `mimir setup` re-injects the token into the remote URL and the next debounced push catches up. |
| 2 | Two parallel turns on different channels race a commit | Internally atomic; both commits land in order | No-op. Documented as v2 concern (see §"Lock coordination"). |
| 3 | Repo bloat over time (turn-per-commit, ~50 commits/day) | Manual `du -sh /mimir-home/.git` audit | v1: monitor only. v2: monthly squash policy or shallow-clone-then-rebase. Defer until repo is >100MB. |
| 4 | Corrupted `.git` directory | `git fsck` fails on commit | Algedonic `git_repo_corrupted`; agent stops auto-commit, surfaces to operator. Recovery: re-clone from remote into a side dir, swap. |
| 5 | Network outage during pull-on-start | `git fetch` fails on container start | `mimir setup` proceeds without pull (volume is authoritative locally); first successful turn-end push reconciles. |
| 6 | Network outage during commit-on-turn | `git push` times out (30s) | Logged `git_push_failed`. Local commit stands; next successful push catches up. |
| 7 | Pre-commit hook false-positive (legitimate content matches a secret pattern) | `git_commit_secret_scan_blocked` algedonic | Agent reads the matched pattern from the event, surfaces to operator. Operator either tunes the pattern or commits manually with `--no-verify` after audit. |
| 8 | Volume corruption (overlay2 disk-full, ext4 fsck failure) | Volume mount fails on container start | Out of scope for git layer; same recovery story as today (recreate volume, restore from host snapshot). |
| 9 | Operator-side force-push rewrites history | `git pull --ff-only` rejects on next start | `git_pull_blocked` algedonic. Operator manually resolves: either reset the container's local branch to match remote, or push container's commits. |
| 10 | Auto-commit produces noise (1000 trivial commits/day) | Repo growth + log review | v1: accept the noise as audit value. v2: introduce empty-diff suppression already (`git status --porcelain` empty-check), add commit-coalescing if >5 commits within 60s. |

## Locked answers

Operator decisions on the six open questions from the v1 draft.
Initial answers in msg 1501603003994476787; revisions to #1, #2, #3
in msg 1501603018377007295. Carried through into the rest of this
spec.

1. **GitHub repos — two of them, separate concerns:**
   - `jasoncarreira/mimirbot` — operator-side repo for the
     `docker-compose.yml`, README, host-side scripts, the
     `.env.example`. Operator-curated; mimir does not push here.
     Distinct from the host-side `~/projects/odin/mimirbot/`
     directory — different thing, don't conflate.
   - `jasoncarreira/mimirbot-state` — **mimir's push target.** The
     remote for `/mimir-home`'s `.git/`. Container env:
     `MIMIR_STATE_REPO=https://github.com/jasoncarreira/mimirbot-state.git`.
   Both private. Operator creates both before PR 4c lands.
2. **Auth:** existing PAT (`GITHUB_TOKEN` in `.env`), expanded by
   operator to cover both new repos. Reuse what's already wired into
   the container; no new secret to manage. Mimir's auto-push only
   targets `mimirbot-state`, but the same token grants access to
   `mimirbot` for whatever operator-side workflows need it. Push URL
   embeds the token at clone-time:
   `https://${GITHUB_TOKEN}@github.com/jasoncarreira/mimirbot-state.git`.
3. **Committer identity:** `user.name = "mimir"`, `user.email =
   "mimir@muninnai.ai"`. The email has no associated GitHub account
   — that's intentional; commits will show in the log without avatar
   attribution, which is the desired shape for a non-human committer.
4. **state/INDEX.md:** tracked. Diff value > regen noise.
5. **Push cadence:** debounce-then-push at 60s, **not** per-turn-push.
   Skip both the commit and the push when `git status --porcelain` is
   empty (most turns don't write to `memory/`). See §"Post-turn commit
   hook contract" for the implementation.
6. **Migration timing:** PR 4a + PR 4b land first **on the current
   bind-mount layout** (no docker change needed for either; both are
   compatible with the bind-mount). PR 4c contains the docker swap as
   a runbook the operator executes manually when convenient (~30 min
   block). PR #23's health probe remains the safety net throughout.

## Test plan

Unit tests (new `tests/test_git_tracking.py`):
- `commit_turn_changes` is a no-op when `git status --porcelain` is
  empty — no commit, no push scheduled.
- `commit_turn_changes` commits when changes present and schedules a
  debounced push.
- `commit_turn_changes` swallows `git_push_failed` without raising.
- `_debounced_push` waits ~60s before invoking `git push`.
- Debounce coalescing: 5 commits within 30s produce 5 commits and
  exactly 1 push (the prior 4 push tasks are cancelled before
  firing).
- Debounce reset: a commit at t=0 schedules push at t=60; a second
  commit at t=30 cancels the t=60 push and schedules a new one for
  t=90.
- `_debounced_push` emits `git_push_failed` on `asyncio.TimeoutError`
  and on `GitError`.
- Pre-commit hook integration: stage a file containing `Bearer
  abcdef...20chars`, attempt commit, assert exit 1 + matching
  algedonic event + no push scheduled.
- Pre-commit hook integration: stage `oauth_credentials.json`, assert
  refusal on filename pattern.
- Allowlist `.gitignore`: verify `atoms.db` placed under tracked dirs
  is still excluded from `git add -A`.
- `inject_token_into_url`: `MIMIR_STATE_REPO` + `GITHUB_TOKEN`
  produces `https://${TOKEN}@github.com/jasoncarreira/mimirbot-state.git`
  (token is URL-encoded if it contains special chars).
- `mimir.health.git_status_summary()` returns `(count, paths[:3])`,
  with truncation suffix when `count > 3`.

Integration tests (new `tests/integration/test_git_setup.py`):
- Fresh-container setup path: empty volume → `mimir setup` →
  `.git` exists, gitignore present, hook executable, initial commit
  exists, remote set.
- Restart-container path: existing `.git`, `mimir setup` → `git pull
  --ff-only` runs, no-op when up-to-date.
- Conflict path: container has unpushed commits, remote has divergent
  commits → `git_pull_blocked` event emitted, agent surfaces.

End-to-end smoke (manual, gated on operator):
- Start container in new layout, run a few user turns, observe
  per-turn commits land in the GitHub repo.
- Force a push failure (block GitHub via firewall), observe
  `git_push_failed` algedonic, observe agent surface it on the next
  turn, observe local commits accumulate and reconcile when
  connectivity returns.
- Trigger the pre-commit secret scanner with a planted token in a
  memory write, observe refusal + algedonic + agent self-correction
  on next turn.

## Sequencing within the larger arc

1. **PR #23 (bind-mount health probe)** — current mitigation, in
   review. Safety net throughout the arc below.
2. **This spec** — operator-locked answers above; ready for
   implementation.
3. **PR 4a — `mimir/git_tracking.py` module + post-turn hook +
   algedonic events.** Compatible with the current bind-mount layout
   (the bind-mount becomes a git repo via `mimir setup`). Behavior is
   gated on `MIMIR_GIT_TRACKING_ENABLED=true` so the module can land
   inert ahead of the gitignore + secret hook. Includes:
   - `commit_turn_changes` + `_schedule_debounced_push` /
     `_debounced_push` (60s debounce) + `_git()` wrapper
   - Post-message phase wiring in `agent.run_turn`
   - `git_commit_failed`, `git_push_failed`, `git_pull_blocked`
     algedonic events
   - `mimir.health.git_status_summary()` for the Self-state line
   - `tests/test_git_tracking.py` (debounce, commit/push, error
     paths, no-op fast path)
4. **PR 4b — Allowlist `.gitignore` + pre-commit secret-scan hook +
   activation.** Drops the gitignore and pre-commit hook into
   `/mimir-home`, flips `MIMIR_GIT_TRACKING_ENABLED=true` in the
   container env, ships the `mimir setup` flow that initializes the
   repo (clones from `MIMIR_STATE_REPO` if set, otherwise `git init`
   + bootstrap commit). Compatible with the current bind-mount —
   `.git/` lands in the bind-mount source. Includes:
   - `docker/templates/.gitignore` + `docker/templates/pre-commit`
   - `docker/post-create.sh` setup logic for clone-or-init
   - HTTPS-token push URL wiring (`MIMIR_STATE_REPO` env →
     `https://${GITHUB_TOKEN}@github.com/...`)
   - Pre-commit hook integration tests
   - Self-state `uncommitted in /mimir-home: ...` line wiring
5. **PR 4c — Docker layout swap (volume replaces bind-mount) +
   migration runbook.** Operator executes manually (~30 min) when
   convenient. Includes:
   - `docker-compose.yml` change: bind-mount → named volume
     (`mimir_home_data`, overlay2)
   - One-shot operator migration script (snapshot host-side state,
     copy into volume, swap, verify)
   - End-to-end smoke test in the new layout
6. **Decommission PR #23 health probe gate** — once 4c is stable on
   the volume layout, the probe's firing rate on `/mimir-home` should
   drop to zero. Probe stays in for `/workspace/mimir` and
   `/benchmark` coverage (those remain VirtioFS bind-mounts); narrow
   the gate to "any VirtioFS mount that is not `/mimir-home`".

Each of 4a / 4b / 4c is independently shippable and reviewable. PR 4a
lands inert (gated env flag); PR 4b activates it on the current
bind-mount; PR 4c upgrades the underlying transport without touching
the application layer.

## References

- `BIND_MOUNT_HEALTH_PROBE.md` — current mitigation spec (PR #23).
- `memory/shared/virtiofs-stale-inode.md` — incident notes from both
  corruption events.
- `memory/shared/filesystem-persistence.md` — current container path
  persistence model.
- `SPEC.md` §<TBD> — once this spec lands, cross-reference the
  storage layout section.
