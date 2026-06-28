# Maintenance

Core memory blocks (`memory/core/`) that get too large create noise that
buries other important information. It's crucial that blocks stay dense.

## Pruning Core Blocks

If a block is too large, consider replacing chunks of block text with a file
reference. Move detail to `memory/<topic>.md` and leave a one-line pointer in
the core block. The core block stays dense; the detail stays accessible via
`Read` or `mcp__mimir__file_search`.

If renumbering is needed (gaps from many inserts at one position), `mv` the
files — `00-identity.md`, `15-foo.md`, `20-style.md` → `00-identity.md`,
`10-foo.md`, `20-style.md`. The first line of each file should be
`<!-- desc: short description -->` so the auto-generated `memory/INDEX.md`
gets a clean entry; otherwise it falls back to the first sentence and
prefixes the entry with `[auto]`.

## File Frequency Report

A simple ad-hoc check — find which files you read most often:

```bash
grep -h '"name": *"Read"' logs/events.jsonl \
  | python3 -c "import json,sys,collections; \
c = collections.Counter(); \
for line in sys.stdin: \
    try: e = json.loads(line)
    except: continue
    p = (e.get('args') or {}).get('file_path')
    if p: c[p] += 1
print('\n'.join(f'{n:>4}  {p}' for p, n in c.most_common(20)))"
```

For heavily accessed files, consider promoting important fields into a core
block (saves a `Read` tool call per turn). If a heavily accessed file is also
large, consider breaking it into smaller files so each `Read` brings in less
unrelated content.

## Index Health

`memory/INDEX.md` regenerates at end-of-turn (debounced). If you see entries
prefixed with `[auto]`, those files are missing the `<!-- desc: -->` first
line — add one with a single `Edit` call to give the index entry a clean
description.
