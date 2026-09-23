"""ocr: a standalone, portable CLI. `ocr convert book.pdf` produces book.txt
and book.md, wherever the caller says to put them. No project-specific path
assumptions -- this must work against any PDF, anywhere.
"""

from __future__ import annotations

import os

# Must be set before torch initializes any CUDA context (the QARI-OCR
# escalation engine's model load fails on this machine's tight 4GB VRAM
# without it -- see engines/qari_engine.py). Setting it here, at the true
# process entry point, guarantees it's in place before any import in the
# pipeline (surya, torch, etc.) can touch CUDA first.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import json
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from bookocr.config import load_config
from bookocr.core.state import StateStore
from bookocr.pipeline import BookPipeline

console = Console()


def _coerce(value: str):
    """--set values arrive as raw CLI strings; config fields (thresholds,
    dpi, booleans...) are typed. Coerce the obvious cases rather than making
    every caller quote --set ocr.primary.device=cpu but also
    --set quality.thresholds.high=80 silently comparing str >= float and
    crashing deep in the scorer.
    """
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _parse_sets(pairs: tuple[str, ...]) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = _coerce(value)
    return out


def _default_output_dir(source_pdf: str) -> Path:
    return Path.cwd() / Path(source_pdf).stem


@click.group()
@click.option("--config", "config_path", default=None, help="Override config YAML, deep-merged over config/default.yaml")
@click.option("--set", "sets", multiple=True, help="Dotted-key override, e.g. --set ocr.primary.device=cpu")
@click.pass_context
def cli(ctx, config_path, sets):
    """ocr: standalone book OCR. Converts a scanned PDF into a high-fidelity,
    page-preserved book.txt and book.md. Independent of any other project --
    works against any PDF path, output goes wherever you say.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path, _parse_sets(sets))


@cli.command()
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False))
@click.option("--output", "output_dir", default=None, type=click.Path(), help="Output directory for book.txt/book.md (default: ./<pdf-stem>/)")
@click.option("--force", is_flag=True, help="Reprocess every page, ignoring any prior run's state")
@click.pass_context
def convert(ctx, pdf, output_dir, force):
    """Convert a single PDF into book.txt + book.md.

    \b
    Example:
        ocr convert "/path/to/book.pdf"
        ocr convert "/path/to/book.pdf" --output "/path/to/output"
    """
    cfg = ctx.obj["config"]
    output_dir = output_dir or _default_output_dir(pdf)
    pipeline = BookPipeline(cfg)

    console.print(f"[bold]Converting[/bold] {pdf}")
    console.print(f"  -> output: {output_dir}")
    t0 = time.time()
    pipeline.convert(pdf, str(output_dir), force=force)
    console.print(f"[green]Done[/green] in {time.time() - t0:.1f}s")
    console.print(f"  book.txt: {Path(output_dir) / 'book.txt'}")
    console.print(f"  book.md:  {Path(output_dir) / 'book.md'}")


@cli.command()
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.pass_context
def status(ctx, output_dir):
    """Show per-status page counts for a book, given its output directory."""
    cfg = ctx.obj["config"]
    internal_dir = Path(output_dir) / cfg.output.internal_dirname
    state = StateStore(internal_dir / "state.db")
    progress = state.book_progress("book")

    table = Table(title=f"Status: {output_dir}")
    table.add_column("Status")
    table.add_column("Pages", justify="right")
    for status_name, count in sorted(progress["by_status"].items()):
        table.add_row(status_name, str(count))
    console.print(table)
    console.print(f"Total pages: {progress['total_pages']}")

    failed = state.failed_pages("book")
    if failed:
        console.print(f"[red]{len(failed)} failed page(s):[/red]")
        for f in failed[:10]:
            console.print(f"  page {f['page']}: {f['error']} (retries: {f['retry_count']})")


@cli.command()
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--page", type=int, default=None, help="Show a single internal page record instead of the qc_report summary")
@click.pass_context
def inspect(ctx, output_dir, page):
    """Inspect a book's internal qc_report, or one page's raw OCR record.
    (Debugging/QC only -- not a product output.)
    """
    cfg = ctx.obj["config"]
    internal_dir = Path(output_dir) / cfg.output.internal_dirname

    if page is not None:
        jsonl_path = internal_dir / "pages.jsonl"
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                if record["page"] == page:
                    console.print_json(data=record)
                    return
        console.print(f"[yellow]Page {page} not found in {jsonl_path}[/yellow]")
        return

    qc_path = internal_dir / "qc_report.json"
    if not qc_path.exists():
        console.print(f"[yellow]No qc_report.json yet at {qc_path} (book not finalized?)[/yellow]")
        return
    with open(qc_path, encoding="utf-8") as f:
        console.print_json(data=json.load(f))


@cli.command()
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--limit", type=int, default=8, help="How many books to sample for the benchmark")
@click.option("--pages-per-book", type=int, default=5, help="Pages sampled per book")
@click.pass_context
def benchmark(ctx, directory, limit, pages_per_book):
    """Developer/QC tool: sample a handful of PDFs under a directory, process
    a few pages from each, and report throughput/quality stats. Not part of
    the product surface -- produces console output only, no files.
    """
    import pymupdf

    cfg = ctx.obj["config"]
    pipeline = BookPipeline(cfg)

    pdfs = sorted(Path(directory).rglob("*.pdf"))[:limit]
    table = Table(title="Benchmark")
    for col in ["Book", "Pages", "Sampled", "Avg s/page", "Mean confidence", "Tier spread"]:
        table.add_column(col)

    import tempfile

    for pdf in pdfs:
        try:
            with pymupdf.open(str(pdf)) as doc:
                total_pages = doc.page_count
        except Exception as e:
            console.print(f"[red]Skipping unreadable {pdf}: {e}[/red]")
            continue

        sample_pages = sorted(set(min(total_pages, p) for p in range(20, 20 + pages_per_book * 40, 40)))[:pages_per_book]

        durations, confidences, tiers = [], [], {}
        with tempfile.TemporaryDirectory(prefix="bookocr_bench_") as work_dir:
            for pno in sample_pages:
                t0 = time.time()
                page_img = pipeline.rasterizer.render_page(str(pdf), pno, cfg.rasterize.dpi, work_dir)
                analysis = pipeline.preprocessor.analyze(page_img)
                if analysis.get("blank"):
                    continue
                page_img = pipeline.preprocessor.apply(page_img, analysis)
                result = pipeline.engine.recognize(page_img)
                report = pipeline.evaluator.score(result, page_img)
                durations.append(time.time() - t0)
                confidences.append(report.score)
                tiers[report.tier.value] = tiers.get(report.tier.value, 0) + 1

        avg_dt = sum(durations) / len(durations) if durations else 0
        avg_conf = sum(confidences) / len(confidences) if confidences else 0
        table.add_row(
            pdf.name, str(total_pages), str(len(durations)), f"{avg_dt:.1f}",
            f"{avg_conf:.1f}", ", ".join(f"{k}:{v}" for k, v in tiers.items()),
        )

    console.print(table)


if __name__ == "__main__":
    cli()
