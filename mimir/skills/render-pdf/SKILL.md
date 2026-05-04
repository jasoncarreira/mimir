---
name: render-pdf
description: Convert Markdown files to PDF via pandoc + pdflatex. Supports table of contents, custom margins, DPI, and metadata. Use when the user asks for a printable / shareable doc, or when generating a long-form report you want to attach via `<send-file kind="file">`. Container needs `pandoc` and `pdflatex` (texlive); if missing, the script reports a clear error and you should surface that to the user.
---

# Render PDF

Wraps pandoc to convert Markdown to PDF. The mimirbot container ships
`pandoc` and a minimal `texlive` install (provides `pdflatex`).

## Prerequisites

```bash
pandoc --version       # confirm pandoc is installed
pdflatex --version     # confirm a PDF engine is available
```

If either is missing in your container, the render call returns an error
message — surface it to the user rather than guessing. Install (Debian-
based image) is `apt-get install pandoc texlive-latex-base
texlive-latex-recommended texlive-fonts-recommended`.

## Quickstart

```bash
python3 .claude/skills/render-pdf/render_pdf.py \
    state/research/report.md
# Writes state/research/report.pdf
```

Or programmatically (the function returns a status string):

```python
import sys
sys.path.insert(0, ".claude/skills/render-pdf")
from render_pdf import render_pdf

result = render_pdf("state/research/report.md")
print(result)  # "PDF written to ..." or error
```

## Common options

```python
render_pdf(
    "state/research/report.md",
    "attachments/outbound/report.pdf",   # explicit output path
    {
        "dpi": 300,                       # image resolution (default 600)
        "toc": True,                      # add a table of contents
        "toc_depth": 2,                   # TOC depth level
        "geometry": "margin=0.75in",      # page margins
        "metadata": {
            "title": "Report Title",
            "author": "Mimir",
            "date": "2026-05-04",
        },
    },
)
```

Parameters: `input_file` (required), `output_file` (default = input with
`.pdf` extension), `options` (optional dict — see render_pdf.py for the
full list of supported keys).

## Workflow for sending a generated PDF

```bash
# 1. Author the markdown wherever you like (state/, scratch/, etc.).
# 2. Render into the outbound attachments dir so <send-file> can resolve it.
python3 .claude/skills/render-pdf/render_pdf.py \
    state/research/Q3-summary.md \
    "$MIMIR_HOME/attachments/outbound/Q3-summary.pdf"
```

Then in your `send_message` reply:

```
Here's the Q3 summary you asked for.

<actions>
  <send-file path="Q3-summary.pdf" kind="file" caption="Q3 summary" cleanup="true" />
</actions>
```

`cleanup="true"` deletes the rendered PDF after a successful send so
the outbound dir doesn't accumulate stale renders.

## When NOT to use

- Short replies that fit in a chat message — just answer in chat.
- The user explicitly asked for a different format (Google Doc, Notion,
  HTML); pandoc supports those too but this skill is the PDF path.
- One-off scratch notes — those go in `state/` as plain markdown.
