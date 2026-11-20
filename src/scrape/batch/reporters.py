"""Batch progress observers and Rich terminal reporting adapters."""

from __future__ import annotations

from collections.abc import Sequence

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from scrape.batch.models import BatchScrapeResult, ScrapeItemResult
from scrape.batch.ports import BatchReporter
from scrape.classifiers import ClassifiedLink

console = Console()


class _ProgressCounter:
    """Shared Phase 3 completion counter spanning all scrape passes."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.completed = 0

    def advance(self) -> int:
        self.completed += 1
        return self.completed


class NullBatchReporter(BatchReporter):
    """No-op reporter (Null Object pattern) for silent programmatic or unit-test runs."""

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None:
        pass

    def on_phase2_start(self, folder_count: int) -> None:
        pass

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None:
        pass

    def on_phase3_start(self, total: int) -> None:
        pass

    def on_item_start(self, link: ClassifiedLink) -> None:
        pass

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None:
        pass

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None:
        pass


class RichBatchReporter(BatchReporter):
    """Rich batch-level progress bar and final error / failure summary observer."""

    _FAILURE_SEPARATOR = "-" * 28

    def __init__(
        self,
        rich_console: Console | None = None,
        *,
        enable_live_bar: bool = True,
    ) -> None:
        self.console = rich_console or console
        self._enable_live_bar = enable_live_bar
        self._progress: Progress | None = None
        self._task_id: int | None = None
        self._active_urls: list[str] = []
        self.summary_printed: bool = False

    def _format_active(self) -> str:
        if not self._active_urls:
            return "Idle"
        latest = self._active_urls[-1]
        short = latest if len(latest) <= 48 else f"{latest[:45]}..."
        if len(self._active_urls) == 1:
            return f"Active: {short}"
        return f"Active ({len(self._active_urls)}): {short}"

    def on_phase1_complete(
        self,
        raw_urls: Sequence[str],
        classified_links: Sequence[ClassifiedLink],
    ) -> None:
        self.console.print(
            f"[bold cyan][Phase 1][/bold cyan] Parsed {len(raw_urls)} raw URL(s) -> "
            f"{len(classified_links)} valid classified link(s)."
        )

    def on_phase2_start(self, folder_count: int) -> None:
        if folder_count <= 0:
            return
        self.console.print(
            f"[bold cyan][Phase 2][/bold cyan] Expanding {folder_count} Google Drive folder(s)..."
        )

    def on_phase2_complete(
        self, expanded_links: Sequence[ClassifiedLink]
    ) -> None:
        self.console.print(
            f"[bold cyan][Phase 2][/bold cyan] Ready to scrape {len(expanded_links)} unique document link(s)."
        )

    def on_phase3_start(self, total: int) -> None:
        self._active_urls.clear()
        if not self._enable_live_bar or total <= 0:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TextColumn("[dim]{task.fields[active_info]}"),
            console=self.console,
            transient=False,
        )
        self._progress.start()
        self._task_id = self._progress.add_task(
            "Scraping batch", total=total, active_info="Starting..."
        )

    def on_item_start(self, link: ClassifiedLink) -> None:
        self._active_urls.append(link.url)
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(
            self._task_id,
            active_info=self._format_active(),
        )

    def on_item_complete(
        self, result: ScrapeItemResult, completed: int, total: int
    ) -> None:
        if result.link.url in self._active_urls:
            self._active_urls.remove(result.link.url)
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(
            self._task_id,
            completed=completed,
            active_info=self._format_active(),
        )

    def _stop_progress(self) -> None:
        if self._progress is None:
            return
        try:
            self._progress.stop()
        except Exception:
            pass
        self._progress = None
        self._task_id = None

    def on_batch_complete(self, batch_result: BatchScrapeResult) -> None:
        self._stop_progress()
        self.print_summary(batch_result)
        self.summary_printed = True

    def _print_errors(self, errors: Sequence[str]) -> None:
        self.console.print("[bold red]#Errors:[/bold red]")
        if not errors:
            self.console.print("None", style="dim")
            return
        for error in errors:
            self.console.print(error, markup=False, highlight=False, soft_wrap=True)

    @staticmethod
    def _format_type_label(link: ClassifiedLink) -> str:
        doc_type = (
            link.document_type.name
            if link.document_type is not None
            else "UNKNOWN"
        )
        return f"{link.website_type.name}.{doc_type}"

    def _print_failure_block(self, index: int, item: ScrapeItemResult) -> None:
        type_label = self._format_type_label(item.link)
        detail = item.error or "Unknown error"
        self.console.print(f"[bold]No.{index}[/bold]")
        self.console.print(
            f"[bold]URL:[/bold] {escape(item.link.url)}",
            highlight=False,
            soft_wrap=True,
        )
        self.console.print(
            f"[bold]Website/DocumentType:[/bold] {escape(type_label)}",
            highlight=False,
            soft_wrap=True,
        )
        self.console.print(
            f"[bold]Log:[/bold] {escape(detail)}",
            highlight=False,
            soft_wrap=True,
        )
        self.console.print(self._FAILURE_SEPARATOR, style="dim")

    def _print_failures(self, results: Sequence[ScrapeItemResult]) -> None:
        self.console.print("[bold red]#Failures:[/bold red]")
        failed_items = [item for item in results if not item.succeeded]
        if not failed_items:
            self.console.print("None", style="dim")
            return
        self.console.print(self._FAILURE_SEPARATOR, style="dim")
        for idx, item in enumerate(failed_items, start=1):
            self._print_failure_block(idx, item)

    def _print_totals(self, batch_result: BatchScrapeResult) -> None:
        self.console.print(
            f"[bold]Total:[/bold] {len(batch_result.results)} | "
            f"[bold red]Fail:[/bold red] {batch_result.failed_count}"
        )

    def print_summary(self, batch_result: BatchScrapeResult) -> None:
        """Render batch errors and failed items only in plain block format."""
        self._print_errors(batch_result.errors)
        self._print_failures(batch_result.results)
        self._print_totals(batch_result)
