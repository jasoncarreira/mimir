---
name: find-skills
description: Discover what bundled skills exist in this mimir install and what each one does. Use when you're not sure if a capability is already covered by a skill ("can I X?", "is there a skill for Y?", "what skills do I have?"). Reads SKILL.md frontmatter from each skill directory and surfaces names + descriptions.
success_criteria:
  # The skill's whole purpose is enumerating + filtering the skills
  # tree. A Bash call that touches the skills/ or .mimir_builtin_skills/
  # paths is the canonical signal of "discovery actually ran".
  any_of:
    - tool_call:
        name: Bash
        args:
          command_glob: "*skills/*/SKILL.md*"
    - tool_call:
        name: Bash
        args:
          command_glob: "*.mimir_builtin_skills*"
---

# Find Skills

mimir's bundled skills live under `skills/<name>/SKILL.md` (relative
to `MIMIR_HOME`). The system prompt's Skills section already lists them by
name; this skill is for *discovery* — when you want a one-line description of
what each skill is for, or you're searching for a capability and want to see
if it's already covered.

## List all skills with descriptions

```bash
for d in "$HOME"/skills/*/; do
  name=$(basename "$d")
  # Extract the description: line from the YAML frontmatter (between the two ---).
  desc=$(awk '/^---$/{n++; next} n==1 && /^description:/{sub(/^description:[[:space:]]*/, ""); print; exit}' "$d/SKILL.md")
  printf '%-22s — %s\n' "$name" "$desc"
done
```

(Replace `$HOME` with `$MIMIR_HOME` if `HOME` isn't set to it.)

## Find a skill by keyword

```bash
grep -l -i "<keyword>" "$HOME"/skills/*/SKILL.md
```

Or to search the descriptions specifically:

```bash
for d in "$HOME"/skills/*/; do
  name=$(basename "$d")
  desc=$(awk '/^---$/{n++; next} n==1 && /^description:/{sub(/^description:[[:space:]]*/, ""); print; exit}' "$d/SKILL.md")
  if echo "$desc" | grep -qi "<keyword>"; then
    echo "$name: $desc"
  fi
done
```

## Read a specific skill in full

```bash
cat "$HOME/skills/<name>/SKILL.md"
```

Skills with `references/` or `scripts/` subdirectories carry additional
material — `ls "$HOME/skills/<name>/"` to see what else is there.

## When you've decided a capability isn't covered

If no existing skill fits, consider:
1. Solving the task with built-in tools (Read/Write/Edit/Bash, the saga and
   channel tools) and proceeding.
2. Filing a "would benefit from a skill" note in your journal or in
   `state/agent-feedback.md` for the operator to triage.
3. The skill-creator skill walks through how to draft a new SKILL.md if
   you decide it's worth codifying the pattern.

This skill is descriptive, not prescriptive — it surfaces what exists, the
operator decides what to add.
