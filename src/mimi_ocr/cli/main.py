from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from mimi_ocr.config import load_config
from mimi_ocr.core.state import StateStore
from mimi_ocr.pipeline import BookPipeline
from mimi_ocr.rasterize.pymupdf_rasterizer import book_id_for

console = Console()


def _parse_sets(pairs: tuple[str, ...]) -> dict:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"--set expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


@click.group()
@click.option("--config", "config_path", default=None, help="Override config YAML, deep-merged over config/default.yaml")
@click.option("--set", "sets", multiple=True, help="Dotted-key override, e.g. --set ocr.primary.device=cpu")
@click.pass_context
def cli(ctx, config_path, sets):
    """mimi-ocr: permanent OCR infrastructure for the Mimi literary-analysis project."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path, _parse_sets(sets))


@cli.command()
@click.argument("target", type=click.Path(exists=True))
@click.option("--force", is_flag=True, help="Reprocess every page, ignoring existing state")
@click.pass_context
def process(ctx, target, force):
    """Process a single PDF, or every PDF under a directory (non-recursive-safe: recurses)."""
    cfg = ctx.obj["config"]
    pipeline = BookPipeline(cfg)

    targets = [target] if Path(target).is_file() else sorted(str(p) for p in Path(target).rglob("*.pdf"))
    if not targets:
        console.print(f"[yellow]No PDFs found under {target}[/yellow]")
        return

    for path in targets:
        console.print(f"[bold]Processing[/bold] {path}")
        t0 = time.time()
        try:
            book_id = pipeline.process_book(path, force=force)
        except Exception as e:
            console.print(f"[red]FAILED[/red] {path}: {e}")
            continue
        console.print(f"  -> book_id={book_id}  ({time.time() - t0:.1f}s)")


@cli.command()
@click.argument("target")
@click.pass_context
def resume(ctx, target):
    """Alias for `process`: pending/failed/low-confidence pages are always
    re-queued automatically unless --force was used on the prior run.
    """
    ctx.invoke(process, target=target, force=False)


@cli.command()
@click.argument("target")
@click.pass_context
def status(ctx, target):
    """Show per-status page counts for a book (pass a PDF path or a book_id)."""
    cfg = ctx.obj["config"]
    book_id = book_id_for(str(Path(target).resolve())) if Path(target).exists() else target
    state = StateStore(cfg.resolve_path(cfg.paths.state_db))
    progress = state.book_progress(book_id)

    table = Table(title=f"Status: {book_id}")
    table.add_column("Status")
    table.add_column("Pages", justify="right")
    for status_name, count in sorted(progress["by_status"].items()):
        table.add_row(status_name, str(count))
    console.print(table)
    console.print(f"Total pages: {progress['total_pages']}")

    failed = state.failed_pages(book_id)
    if failed:
        console.print(f"[red]{len(failed)} failed page(s):[/red]")
        for f in failed[:10]:
            console.print(f"  page {f['page']}: {f['error']} (retries: {f['retry_count']})")


@cli.command()
@click.argument("book_id")
@click.option("--page", type=int, default=None, help="Show a single page record instead of the qc_report summary")
@click.pass_context
def inspect(ctx, book_id, page):
    """Inspect a processed book's qc_report, or one page's full record."""
    cfg = ctx.obj["config"]
    book_dir = cfg.resolve_path(cfg.paths.processed_dir) / book_id

    if page is not None:
        jsonl_path = book_dir / "book.jsonl"
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                if record["page"] == page:
                    console.print_json(data=record)
                    return
        console.print(f"[yellow]Page {page} not found in {jsonl_path}[/yellow]")
        return

    qc_path = book_dir / "qc_report.json"
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
    """Phase 0 benchmark: sample a handful of books, process a few pages from
    each, and report throughput/quality stats without committing to a full run.
    """
    import pymupdf

    cfg = ctx.obj["config"]
    pipeline = BookPipeline(cfg)

    pdfs = sorted(Path(directory).rglob("*.pdf"))[:limit]
    table = Table(title="Phase 0 Benchmark")
    for col in ["Book", "Pages", "Sampled", "Avg s/page", "Mean confidence", "Tier spread"]:
        table.add_column(col)

    for pdf in pdfs:
        try:
            with pymupdf.open(str(pdf)) as doc:
                total_pages = doc.page_count
        except Exception as e:
            console.print(f"[red]Skipping unreadable {pdf}: {e}[/red]")
            continue

        sample_pages = sorted(set(min(total_pages, p) for p in range(20, 20 + pages_per_book * 40, 40)))[:pages_per_book]
        book_id = book_id_for(str(pdf.resolve()))
        work_dir = cfg.resolve_path(cfg.paths.work_dir) / f"bench_{book_id}"
        work_dir.mkdir(parents=True, exist_ok=True)

        durations, confidences, tiers = [], [], {}
        for pno in sample_pages:
            t0 = time.time()
            page_img = pipeline.rasterizer.render_page(str(pdf), pno, cfg.rasterize.dpi, str(work_dir))
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
