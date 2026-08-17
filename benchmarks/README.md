# Benchmarks

Run the durable-log redaction benchmark from the repository root:

```bash
uv run --extra dev python benchmarks/redaction_hot_path.py
```

It measures 400 iterations over five representative strings (plain tool output,
a JSON tool-call record, an error line, a 2 KB blob, and a 50-line record) and a
nested event payload. The report compares the optimized implementation with the
three colon scans it replaces and the measured 66.5 us pre-#1539 baseline.
