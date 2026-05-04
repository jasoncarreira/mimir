from typing import Optional, List

def render_pdf(
    input_file: str,
    output_file: str = None,
    options: dict = None
) -> str:
    """Render Markdown file to PDF using Pandoc.

    Converts Markdown documents to high-quality PDFs with support for:
    - High-resolution image embedding
    - Table of contents generation
    - Custom page geometry and margins
    - PDF metadata and styling

    Args:
        input_file: Path to input Markdown file (relative to /workspace/muninn)
        output_file: Path to output PDF file (optional, defaults to input_file with .pdf extension)
        options: Optional dict with pandoc options:
            - dpi: Image resolution (default: 600)
            - toc: Include table of contents (default: False)
            - toc_depth: TOC depth level (default: 3)
            - geometry: Page geometry (default: "margin=1in")
            - pdf_engine: PDF engine to use (default: "pdflatex")
            - metadata: Dict of PDF metadata (title, author, date)
            - template: Custom pandoc template file

    Returns:
        Success message with output path, or error message

    Examples:
        # Basic PDF generation
        render_pdf("state/research/report.md")

        # With table of contents and custom DPI
        render_pdf(
            "state/research/report.md",
            "output/report-final.pdf",
            {"dpi": 300, "toc": True}
        )

        # With custom margins and metadata
        render_pdf(
            "state/research/competitive-analysis.md",
            options={
                "dpi": 600,
                "toc": True,
                "toc_depth": 2,
                "geometry": "margin=0.75in",
                "metadata": {
                    "title": "TPRM Competitive Landscape",
                    "author": "Muninn AI Assistant",
                    "date": "2026-01-15"
                }
            }
        )
    """
    # All imports inside function for Letta
    import os
    import shutil
    import subprocess
    from pathlib import Path

    try:
        # Determine base path
        base = Path("/workspace/muninn") if Path("/workspace/muninn").exists() else Path.cwd()

        # Resolve input file path
        if not input_file.startswith('/'):
            input_path = base / input_file
        else:
            input_path = Path(input_file)

        # Resolve input path and validate it stays within base
        input_path = input_path.resolve()
        try:
            input_path.relative_to(base)
        except ValueError:
            return f"❌ Error: Input file must be within {base}"

        # Validate input file exists
        if not input_path.exists():
            return f"❌ Error: Input file not found: {input_path}"

        if not input_path.is_file():
            return f"❌ Error: Input path is not a file: {input_path}"

        # Determine output file path
        if output_file is None:
            output_path = input_path.with_suffix('.pdf')
        elif not output_file.startswith('/'):
            output_path = base / output_file
        else:
            output_path = Path(output_file)

        # Resolve output path and validate it stays within base
        output_path = output_path.resolve()
        try:
            output_path.relative_to(base)
        except ValueError:
            return f"❌ Error: Output file must be within {base}"

        # Create output directory if needed
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if pandoc is installed
        if not shutil.which('pandoc'):
            return "❌ Error: pandoc is not installed. Install with: brew install pandoc (macOS) or apt-get install pandoc (Linux)"

        # Parse options
        opts = options or {}
        dpi = opts.get('dpi', 600)
        toc = opts.get('toc', False)
        toc_depth = opts.get('toc_depth', 3)
        geometry = opts.get('geometry', 'margin=1in')
        pdf_engine = opts.get('pdf_engine', 'pdflatex')
        metadata = opts.get('metadata', {})
        template = opts.get('template')

        # Build pandoc command
        cmd = [
            'pandoc',
            str(input_path),
            '-o', str(output_path),
            f'--dpi={dpi}',
            f'--pdf-engine={pdf_engine}'
        ]

        # Add table of contents
        if toc:
            cmd.append('--toc')
            cmd.append(f'--toc-depth={toc_depth}')

        # Add page geometry
        if geometry:
            cmd.append(f'-V')
            cmd.append(f'geometry:{geometry}')

        # Add metadata
        for key, value in metadata.items():
            cmd.append('-M')
            cmd.append(f'{key}={value}')

        # Track warnings to append to result
        warnings = []

        # Add custom template
        if template:
            template_path = base / template if not template.startswith('/') else Path(template)
            if template_path.exists():
                cmd.append(f'--template={template_path}')
            else:
                warnings.append(f"⚠️  Template file not found: {template_path}, proceeding without template")

        # Execute pandoc
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60  # 60 second timeout
        )

        # Check result
        if result.returncode == 0:
            # Get file size
            size_bytes = output_path.stat().st_size
            size_kb = size_bytes / 1024
            size_mb = size_kb / 1024

            if size_mb >= 1:
                size_str = f"{size_mb:.2f} MB"
            else:
                size_str = f"{size_kb:.1f} KB"

            success_msg = f"✅ PDF generated successfully!\n\nInput:  {input_path.relative_to(base)}\nOutput: {output_path.relative_to(base)}\nSize:   {size_str}\nDPI:    {dpi}\nTOC:    {'Yes' if toc else 'No'}"

            # Append warnings if any
            if warnings:
                success_msg += "\n\n" + "\n".join(warnings)

            return success_msg
        else:
            # Pandoc failed
            error_msg = result.stderr.strip()

            # Check for common issues
            if 'pdflatex not found' in error_msg or 'pdf-engine' in error_msg:
                return f"❌ Error: PDF engine '{pdf_engine}' not found. Install LaTeX: brew install basictex (macOS) or apt-get install texlive (Linux)"

            return f"❌ Pandoc conversion failed:\n{error_msg}"

    except subprocess.TimeoutExpired:
        return "❌ Error: Pandoc conversion timed out (>60 seconds). File may be too large or complex."
    except Exception as e:
        return f"❌ Error rendering PDF: {e}"
