"""Typer CLI adapter for the 3-phase async batch scraper."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from rich.console import Console
import typer

from scrape import config
from scrape.batch.models import BatchScrapeResult
from scrape.batch.orchestrator import BatchScraper
from scrape.batch.reporters import RichBatchReporter
from scrape.config import BatchConfig, RuntimeConfigOverrides


class OptionalValueOptionCommand(typer.core.TyperCommand):
    """TyperCommand supporting options whose value argument is optional (e.g. --use-gg-api [<api_key>])."""

    optional_value_options: frozenset[str] = frozenset({"--use-gg-api", "-ugga"})

    def _collect_parser_metadata(
        self, ctx: typer.Context
    ) -> tuple[dict[str, int], int]:
        """Inspect registered params to find value-taking option arities and required positional count."""
        value_taking_arities: dict[str, int] = {}
        required_positionals = 0
        for param in self.get_params(ctx):
            if isinstance(param, typer.core.TyperArgument):
                if param.required:
                    required_positionals += max(1, param.nargs or 1)
                continue
            if not isinstance(param, typer.core.TyperOption):
                continue
            if param.is_flag or param.count:
                continue
            arity = max(1, param.nargs or 1)
            for opt in param.opts:
                if opt not in self.optional_value_options:
                    value_taking_arities[opt] = arity
        return value_taking_arities, required_positionals

    def _has_inline_optional_value(self, token: str) -> bool:
        return "=" in token and token.split("=", 1)[0] in self.optional_value_options

    @staticmethod
    def _can_take_following_value(args: Sequence[str], next_idx: int) -> bool:
        return (
            next_idx < len(args)
            and args[next_idx] != "--"
            and not args[next_idx].startswith("-")
        )

    @staticmethod
    def _apply_empty_insertions(
        args: Sequence[str], insert_empty_after: set[int]
    ) -> list[str]:
        if not insert_empty_after:
            return list(args)
        normalized: list[str] = []
        for idx, token in enumerate(args):
            normalized.append(token)
            if idx in insert_empty_after:
                normalized.append("")
        return normalized

    def _normalize_optional_value_args(
        self,
        args: Sequence[str],
        value_taking_arities: dict[str, int],
        required_positionals: int,
    ) -> list[str]:
        """Inject an empty sentinel value after bare optional-value options without stealing positional args."""
        insert_empty_after: set[int] = set()
        candidate_pairs: list[tuple[int, int]] = []
        guaranteed_positionals = 0

        idx = 0
        total = len(args)
        while idx < total:
            token = args[idx]
            if token == "--":
                guaranteed_positionals += total - idx - 1
                break

            if token in self.optional_value_options:
                if self._can_take_following_value(args, idx + 1):
                    candidate_pairs.append((idx, idx + 1))
                    idx += 2
                else:
                    insert_empty_after.add(idx)
                    idx += 1
                continue

            if self._has_inline_optional_value(token):
                idx += 1
                continue

            if token in value_taking_arities:
                idx += 1 + value_taking_arities[token]
                continue

            if token.startswith("-") and len(token) > 1:
                idx += 1
                continue

            guaranteed_positionals += 1
            idx += 1

        reserve_count = max(0, required_positionals - guaranteed_positionals)
        if reserve_count > 0 and candidate_pairs:
            # Preserve trailing candidate tokens for required positional arguments (e.g. `source`).
            for opt_idx, _ in candidate_pairs[-reserve_count:]:
                insert_empty_after.add(opt_idx)

        return self._apply_empty_insertions(args, insert_empty_after)

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:
        value_taking_arities, required_positionals = self._collect_parser_metadata(ctx)
        normalized_args = self._normalize_optional_value_args(
            args, value_taking_arities, required_positionals
        )
        return super().parse_args(ctx, normalized_args)


def _was_passed_on_cli(ctx: typer.Context, param_name: str) -> bool:
    """Return True when the option was explicitly supplied on the command line."""
    source = ctx.get_parameter_source(param_name)
    return getattr(source, "name", None) == "COMMANDLINE"


def resolve_cli_source(raw_source: str) -> Path | str:
    """Resolve a CLI source argument to either an HTTP(S) URL string or an existing file Path.

    Raises ValueError with a user-facing error message when the value is neither
    a valid HTTP(S) URL nor an existing file.
    """
    cleaned = raw_source.strip()
    if (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in {'"', "'"}
    ):
        cleaned = cleaned[1:-1].strip()

    if not cleaned:
        raise ValueError("Input source cannot be empty.")

    parsed = urlparse(cleaned)
    if parsed.scheme.lower() in {"http", "https"}:
        if not parsed.netloc:
            raise ValueError(f"Invalid URL (missing domain): {cleaned}")
        return cleaned

    source_path = Path(cleaned)
    if not source_path.exists() or not source_path.is_file():
        raise ValueError(f"Input file does not exist: {source_path}")
    return source_path


def _cli_override(
    ctx: typer.Context, param_name: str, value: int
) -> int | None:
    """Return value only when explicitly provided on the CLI, else None."""
    return value if _was_passed_on_cli(ctx, param_name) else None


app = typer.Typer(
    name="batch-scrape",
    help="Batch scrape documents from a .txt file or a single URL using a single shared Chromium browser.",
    add_completion=False,
)


@app.command(cls=OptionalValueOptionCommand)
def main(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help="Path to a .txt file of URLs, or a single document/folder URL to scrape.",
    ),
    concurrency: int = typer.Option(
        config.CONCURRENT_SCRAPER,
        "--concurrency",
        "-c",
        min=1,
        help="Maximum number of concurrent scraping workers sharing one browser.",
    ),
    output: Path = typer.Option(
        Path(config.OUTPUT_DIR),
        "--output",
        "-o",
        help="Output directory for scraped documents.",
    ),
    scrape_type: str = typer.Option(
        config.SCRAPE_TYPE,
        "--scrape-type",
        "-t",
        help="Scrape mode: 'd' (direct download first) or 's' (viewer screenshot/API).",
    ),
    headless_disable: bool = typer.Option(
        False,
        "--headless-disable",
        "-hd",
        is_flag=True,
        help="Disable headless mode and open a visible browser window for visualization.",
    ),
    use_gg_api: str | None = typer.Option(
        None,
        "--use-gg-api",
        "-ugga",
        metavar="[<api_key>]",
        show_default=False,
        help="Enable Google Drive API for folder scraping; optionally pass an API key to override the configured key.",
    ),
    concurrent_folder: int = typer.Option(
        config.MAX_CONCURENT_FOLDER_NODE_VISIT,
        "--concurrent-folder",
        "-cf",
        min=1,
        help="Maximum number of Google Drive folders to visit and scrape concurrently.",
    ),
    folder_timeout: int = typer.Option(
        config.FOLDER_TIMEOUT,
        "--folder-timeout",
        "-ft",
        min=1,
        help="Maximum time in seconds to wait when opening a Google Drive folder (recommended >= --concurrent-folder).",
    ),
    max_folder_depth: int = typer.Option(
        config.MAX_FOLDER_DEPTH,
        "--max-folder-depth",
        "-mfd",
        min=1,
        max=100,
        help="Maximum nested subfolder depth to traverse when expanding Google Drive folders.",
    ),
    max_retry: int = typer.Option(
        config.SCRAPE_RETRY_COUNT,
        "--max-retry",
        "-mr",
        min=0,
        max=10,
        help="Maximum number of retry attempts when a scrape operation fails.",
    ),
    max_concurrent_img_api: int = typer.Option(
        config.MAX_CONCURRENT_IMG_API,
        "--max-concurrent-img-api",
        "-mcia",
        min=1,
        max=30,
        help="Maximum number of concurrent viewer image API requests per batch.",
    ),
) -> None:
    """Run the 3-phase async batch scraper from the command line."""
    cli_console = Console()
    try:
        normalized_type = config.normalize_scrape_type(scrape_type)
    except ValueError:
        cli_console.print(
            f"[bold red]Invalid --scrape-type {scrape_type!r}. Must be 'd' or 's'.[/bold red]"
        )
        raise typer.Exit(code=1)

    try:
        resolved_source = resolve_cli_source(source)
    except ValueError as exc:
        cli_console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=1) from exc

    overrides = RuntimeConfigOverrides(
        headless_disable=headless_disable,
        use_google_drive_api=use_gg_api is not None,
        google_api_key=use_gg_api,
        concurrent_folder=_cli_override(ctx, "concurrent_folder", concurrent_folder),
        folder_timeout=_cli_override(ctx, "folder_timeout", folder_timeout),
        max_folder_depth=_cli_override(ctx, "max_folder_depth", max_folder_depth),
        max_retry=_cli_override(ctx, "max_retry", max_retry),
        max_concurrent_img_api=_cli_override(
            ctx, "max_concurrent_img_api", max_concurrent_img_api
        ),
    )
    overrides.apply()

    batch_config = BatchConfig.from_config(
        output_dir=output,
        concurrency=concurrency,
        scrape_type=normalized_type,
    )
    reporter = RichBatchReporter(cli_console)
    batch_scraper = BatchScraper(config=batch_config, reporter=reporter)

    try:
        result = asyncio.run(batch_scraper.run(resolved_source))
    except Exception as exc:
        reporter.on_batch_complete(
            BatchScrapeResult(
                raw_urls=[],
                classified_links=[],
                results=[],
                errors=[f"Batch scrape failed: {exc}"],
            )
        )
        raise typer.Exit(code=1) from exc

    if not reporter.summary_printed:
        reporter.print_summary(result)

    if not result.classified_links or result.failed_count > 0:
        raise typer.Exit(code=1)
