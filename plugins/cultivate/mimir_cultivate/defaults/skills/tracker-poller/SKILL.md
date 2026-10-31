---
name: cultivate-tracker-poller
description: "Triage GitHub issues, Linear issues and Jira issues assigned to the bot into Worklink leaves or epics. Install mimir-cultivate, select MIMIR_PLUGINS=cultivate and run `mimir skills install cultivate:tracker-poller`, then configure the tracker: section of worklink.yaml and the tracker credentials."
env:
  optional:
    - name: GITHUB_TOKEN
      description: "GitHub token with issue read and comment access (github_issues adapter)."
      example: "ghp_..."
    - name: MIMIR_GITHUB_SELF_LOGIN
      description: "GitHub login the bot acts as (github_issues adapter). Required for that adapter."
      example: "your-bot-login"
    - name: MIMIR_GITHUB_TRUSTED_BOT_LOGINS
      description: "Comma-separated bot logins whose activity counts as trusted (github_issues adapter)."
      example: "dependabot[bot]"
    - name: LINEAR_API_KEY
      description: "Access token of the Linear OAuth app installed with actor=app and the app:assignable scope; the bot is set as the issue's delegate (linear adapter)."
      example: "<oauth access token>"
    - name: JIRA_SITE
      description: "Jira Cloud host, without scheme (jira adapter)."
      example: "example.atlassian.net"
    - name: JIRA_EMAIL
      description: "Email of the Jira service account (jira adapter)."
      example: "bot@example.com"
    - name: JIRA_API_TOKEN
      description: "API token of the Jira service account (jira adapter)."
      example: "ATATT..."
---

# Cultivate Tracker Poller - triage tracker issues assigned to the bot

This poller ships in `mimir-cultivate`, not core. When selected with
`MIMIR_PLUGINS=cultivate` it is installed from
`plugins/cultivate/mimir_cultivate/defaults/skills/tracker-poller/` into
`<home>/skills/cultivate-tracker-poller/` with `mimir skills install cultivate:tracker-poller`.
`mimir cultivate setup --tracker ...` does both steps and writes the configuration below.

## Configuration

The poller does nothing until `<home>/worklink.yaml` has a `tracker:` section with at least
one adapter. Unknown keys are rejected.

```yaml
tracker:
  close_on_merge: false        # close the tracker issue after the Worklink PR merges
  adapters:
    github_issues:
      repos: [owner/repo]      # defaults to the worklink repository
    linear:
      team_keys: [ENG]
      transitions: {filed: "In Progress", pr_opened: "In Review"}
      project_transitions: {ENG: {merged: "Done"}}
    jira:
      account_id: "<service account id>"
      projects: [PROJ]
      trusted_groups: [engineering]
      trusted_project_roles: [Developer]
      transitions: {filed: "In Progress"}
```

`transitions` and `project_transitions` map a lifecycle event (`filed`, `build_started`,
`pr_opened`, `merged`) to a workflow state name. GitHub issues have no workflow states.
Changes to `tracker:` made by hand are applied to a setup-managed home only by rerunning
`mimir cultivate setup` with `--force`.

## What the poller does each fire

1. Applies queued tracker transitions and backstop closures, and reports notices.
2. Lists the open issues assigned to the bot (at most 50 per adapter): the assignee on GitHub
   and Jira, the delegate on Linear.
3. For each issue whose update time changed, reads the full thread (at most 10 reads per
   fire) and decides whether it may reach the agent.
4. Emits at most 5 items per fire. One item is one issue; the agent gets only the tracker
   tools: `tracker_file_work`, `tracker_comment` and `tracker_set_state`.

Triage has four outcomes: no work needed (reply with `tracker_comment`), clarification
needed (ask with `tracker_comment`), one leaf, or one epic (`tracker_file_work`). An issue
whose work is already filed receives later activity as an `update`, which never files new
work. A triaged issue that gets no reply is offered to the agent once more after 24 hours.

When filing a leaf or epic, write acceptance criteria for the change and its tests,
never for publication. Worklink commits, pushes and opens the PR after its gate, and
adds the tracker closing line (`Closes #N` / `Fixes KEY`) itself. Put reviewer and
PR-description requests from the tracker issue in Worklink notes, or describe the
content to write to `.worklink-pr-body.md`; do not make them checklist items.

Worklink links the resulting pull request to the issue. A leaf pull request starts with the
tracker line: `Closes #N`, `Fixes ENG-123` or `Fixes PROJ-45`, and a Jira key also prefixes the
title. An epic pull request is written by the feature factory, and Worklink adds the same line
after the factory finishes; if that edit fails, the operator gets a notice. The tracker closes
the issue on a merge to the default branch; for work that targets another branch, set
`close_on_merge` so the backstop closes it.

## The trust rule

The agent only sees a thread whose latest activity by anyone other than the bot is a trusted
person's assignment or comment. Every change to the title, the body or a comment is checked
by who made it, so a later trusted edit never hides an earlier untrusted one. An untrusted
latest activity, an unattributable one, or a thread or edit history over 100 entries withholds
the issue. A Jira comment edited after the bot's assignment withholds the issue, because Jira
does not record who made each comment edit; a trusted comment or re-assignment afterwards
re-attests it. The host re-reads the issue and re-applies the rule before the agent turn starts.

## Notices

Withheld issues and transitions that could not be applied raise operator notices. Titles
appear only on a web chat operator alert channel (live, never stored); the dashboard and every
other channel receive title-free lines.

Known limitation: a notice reason the host does not recognize, such as `adapter_unavailable`
or `attempts_exhausted`, is reported as `failed`.

## Jira permissions

The Jira service account needs Browse Projects on each configured project, Add Comments, and
Transition Issues when transitions or `close_on_merge` are set. The adapter also calls the
user, group and project-role endpoints, `GET /rest/api/3/user`, `GET /rest/api/3/user/groups`
(for `trusted_groups`), `GET /rest/api/3/project/{key}/role` and
`GET /rest/api/3/project/{key}/role/{id}` (for `trusted_project_roles`), alongside
`POST /rest/api/3/search/jql`, `GET /rest/api/3/issue/{key}`,
`POST /rest/api/3/issue/{key}/comment` and `GET`/`POST /rest/api/3/issue/{key}/transitions`.
Grant the service account read access to the configured groups and roles (Jira may require the
"Browse users and groups" global permission or project administration) and verify that each
endpoint returns 200 for it. A 401 or 403 fails closed: no Jira issue is delivered, and the
poller writes the stderr code `adapter_unavailable:jira:http_403`.

## Tests

`uv run --all-packages --extra dev --extra bench pytest -q plugins/cultivate/mimir_cultivate/defaults/skills/tracker-poller/tests/`
