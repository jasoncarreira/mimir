# Changelog fragments

For each change, add one `changelog.d/<name>.md` file. Use the Chainlink issue ID
when available (for example, `changelog.d/1897.md`), or a short slug such as
`changelog.d/mcp-2x.md`. Write the Markdown bullet(s) exactly as they should
appear in the released changelog; start the file with a bullet. For example:

```markdown
- Fix the example behavior (#1897).
```

Do not add entries to `CHANGELOG.md`'s `[Unreleased]` section. During release
preparation, run `python scripts/changelog_collect.py X.Y.Z --date YYYY-MM-DD`
(omit `--date` to use today). The collector sorts fragments by filename, includes
any older entries still in `[Unreleased]`, and removes the collected fragments.
`README.md` is never collected.
