from __future__ import annotations

import asyncio
import contextvars
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

from rich.live import Live
from rich.text import Text


@runtime_checkable
class ProgressReporter(Protocol):
    """Port for reporting per-document page scraping progress."""

    def reset(self) -> None: ...

    def increment(self, total_pages: int) -> None: ...

    def finish(self) -> None: ...

    def get_count(self) -> int: ...


_live_progress_enabled_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "live_progress_enabled", default=True
)


def is_live_progress_enabled() -> bool:
    """Return True when single-document Rich Live / status spinners are allowed."""
    return _live_progress_enabled_var.get()


@contextmanager
def suppress_live_progress() -> Iterator[None]:
    """Context manager to disable single-document Rich Live displays during batch runs."""
    token = _live_progress_enabled_var.set(False)
    try:
        yield
    finally:
        _live_progress_enabled_var.reset(token)


class ScrapeProgress:
    """Thread-safe and task-isolated counter for pages completed by document scrapers."""

    def __init__(
        self,
        *,
        enable_live: bool = True,
        on_increment: Callable[[int, int], None] | None = None,
    ) -> None:
        self._count = 0
        self._lock = Lock()
        self._live: Live | None = None
        self._enable_live = enable_live
        self._on_increment = on_increment

    def _should_display_live(self) -> bool:
        return self._enable_live and is_live_progress_enabled()

    def _get_live(self) -> Live | None:
        """Return a Rich Live instance when live output is enabled."""
        if not self._should_display_live():
            return None
        if self._live is None:
            try:
                self._live = Live("", refresh_per_second=10)
                self._live.start()
            except Exception:
                self._live = None
        return self._live

    def increment(self, total_pages: int) -> None:
        if total_pages <= 0:
            raise ValueError("total_pages must be greater than zero")

        with self._lock:
            self._count += 1
            count = self._count

            if self._on_increment is not None:
                try:
                    self._on_increment(count, total_pages)
                except Exception:
                    pass

            if not self._should_display_live():
                return

            percentage = count / total_pages * 100
            live = self._get_live()
            if live is not None:
                text = Text()
                text.append("Scraped pages: ", style="bold yellow")
                text.append(f"{count}/{total_pages} | {percentage:.1f}%")
                live.update(text)
            else:
                sys.stdout.write(
                    f"\rScraped pages: {count}/{total_pages} | {percentage:.1f}%"
                )
                sys.stdout.flush()

    def finish(self) -> None:
        """Stop the live display (call once scraping is done)."""
        with self._lock:
            if self._live is not None:
                try:
                    self._live.stop()
                except Exception:
                    pass
                self._live = None
            elif self._should_display_live():
                sys.stdout.write("\n")
                sys.stdout.flush()

    def get_count(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


class NullScrapeProgress(ScrapeProgress):
    """Silent per-task progress tracker that counts pages without terminal output."""

    def __init__(
        self, on_increment: Callable[[int, int], None] | None = None
    ) -> None:
        super().__init__(enable_live=False, on_increment=on_increment)


class OutputFilenameResolver:
    """Async- and thread-safe output filename reservation and deduplication helper.

    Prevents concurrent tasks with identical document titles from overwriting or
    corrupting each other's files in ``OUTPUT_DIR``.
    """

    def __init__(self) -> None:
        self._thread_lock = Lock()
        self._async_lock = asyncio.Lock()
        self._reserved: set[Path] = set()

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize a document filename to a safe single-segment basename."""
        cleaned = re.sub(r"[/\\]+", "_", filename.strip())
        return cleaned or "document"

    def _reserve_locked(
        self,
        output_dir: Path,
        filename: str,
        file_id: str | None = None,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = self.sanitize_filename(filename)
        base_path = Path(safe_name)
        stem = base_path.stem or "document"
        suffix = base_path.suffix

        candidate = output_dir / f"{stem}{suffix}"
        resolved_candidate = candidate.resolve()
        if resolved_candidate not in self._reserved and not candidate.exists():
            self._reserved.add(resolved_candidate)
            return candidate

        counter = 1
        while True:
            dedup_candidate = output_dir / f"{stem}_{counter}{suffix}"
            resolved_dedup = dedup_candidate.resolve()
            if resolved_dedup not in self._reserved and not dedup_candidate.exists():
                self._reserved.add(resolved_dedup)
                return dedup_candidate
            counter += 1

    async def reserve(
        self,
        output_dir: Path,
        filename: str,
        file_id: str | None = None,
    ) -> Path:
        """Reserve a unique output file path asynchronously under ``asyncio.Lock``."""
        async with self._async_lock:
            with self._thread_lock:
                path = self._reserve_locked(output_dir, filename, file_id=file_id)
                ctx = _active_task_context_var.get()
                if ctx is not None:
                    ctx.reserved_path = path
                return path

    def reserve_sync(
        self,
        output_dir: Path,
        filename: str,
        file_id: str | None = None,
    ) -> Path:
        """Reserve a unique output file path synchronously under ``threading.Lock``."""
        with self._thread_lock:
            path = self._reserve_locked(output_dir, filename, file_id=file_id)
            ctx = _active_task_context_var.get()
            if ctx is not None:
                ctx.reserved_path = path
            return path

    def release(self, path: Path | None) -> None:
        """Release a reservation if the file was not written (e.g., failed direct download)."""
        if path is None:
            return
        with self._thread_lock:
            if not path.exists():
                self._reserved.discard(path.resolve())
            ctx = _active_task_context_var.get()
            if ctx is not None and ctx.reserved_path == path:
                ctx.reserved_path = None

    def reset(self) -> None:
        """Clear all in-memory path reservations."""
        with self._thread_lock:
            self._reserved.clear()


@dataclass
class TaskExecutionContext:
    """Task-scoped execution context carrying batch overrides and isolated state."""

    output_dir: Path | None = None
    scrape_type: str | None = None
    progress: ProgressReporter | None = None
    filename_resolver: OutputFilenameResolver | None = None
    reserved_path: Path | None = None


_active_task_context_var: contextvars.ContextVar[TaskExecutionContext | None] = (
    contextvars.ContextVar("active_task_context", default=None)
)

_default_filename_resolver = OutputFilenameResolver()


@contextmanager
def task_execution_context(
    *,
    output_dir: Path | None = None,
    scrape_type: str | None = None,
    progress: ProgressReporter | None = None,
    filename_resolver: OutputFilenameResolver | None = None,
    enable_live: bool = False,
) -> Iterator[TaskExecutionContext]:
    """Bind per-task configuration and progress tracking for an async worker."""
    ctx = TaskExecutionContext(
        output_dir=output_dir,
        scrape_type=scrape_type,
        progress=progress or NullScrapeProgress(),
        filename_resolver=filename_resolver or _default_filename_resolver,
    )
    ctx_token = _active_task_context_var.set(ctx)
    live_token = _live_progress_enabled_var.set(enable_live)
    try:
        yield ctx
    finally:
        _live_progress_enabled_var.reset(live_token)
        _active_task_context_var.reset(ctx_token)


def get_active_output_dir() -> Path | None:
    """Return the task-scoped output directory override if active."""
    ctx = _active_task_context_var.get()
    return ctx.output_dir if ctx is not None else None


def get_active_scrape_type() -> str | None:
    """Return the task-scoped scrape_type override ('d' or 's') if active."""
    ctx = _active_task_context_var.get()
    return ctx.scrape_type if ctx is not None else None


def get_active_progress() -> ProgressReporter | None:
    """Return the task-scoped ProgressReporter if active."""
    ctx = _active_task_context_var.get()
    return ctx.progress if ctx is not None else None


def get_active_filename_resolver() -> OutputFilenameResolver:
    """Return the task-scoped OutputFilenameResolver, or the process-wide default."""
    ctx = _active_task_context_var.get()
    if ctx is not None and ctx.filename_resolver is not None:
        return ctx.filename_resolver
    return _default_filename_resolver


def resolve_download_filename(
    headers: object,
    fallback_name: str,
    *,
    prefer_header_name: bool = False,
    default_extension: str | None = None,
) -> str:
    """Pick a download filename from Content-Disposition, else fallback + inferred extension."""
    import mimetypes
    import re
    from urllib.parse import unquote

    def _get(key: str) -> str:
        getter = getattr(headers, "get", None)
        value = getter(key, "") if getter else ""
        return value or ""

    name: str | None = None
    if prefer_header_name:
        disposition = _get("Content-Disposition")
        match = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.I)
        if match:
            name = unquote(match.group(1).strip().strip('"'))
        else:
            match = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
            if match:
                name = match.group(1).strip()
        if name:
            name = Path(name.replace("\\", "/")).name or None

    chosen = name or fallback_name
    if not Path(chosen).suffix and prefer_header_name:
        content_type = _get("Content-Type").split(";", 1)[0].strip().lower()
        ext = default_extension or mimetypes.guess_extension(content_type) or ""
        if ext == ".jpe":
            ext = ".jpg"
        chosen += ext
    return chosen


async def reserve_output_path(
    output_dir: Path,
    filename: str,
    file_id: str | None = None,
) -> Path:
    """Reserve a deduplicated output path in ``output_dir`` using the active resolver."""
    resolver = get_active_filename_resolver()
    return await resolver.reserve(output_dir, filename, file_id=file_id)


def release_output_path(path: Path | None) -> None:
    """Release an unused output path reservation on the active resolver."""
    resolver = get_active_filename_resolver()
    resolver.release(path)


scrape_progress = ScrapeProgress()


def get_scraped_page_count() -> int:
    """Return the number of PDF pages scraped so far."""
    active = get_active_progress()
    if active is not None:
        return active.get_count()
    return scrape_progress.get_count()


class BatchErrorCollector:
    """Thread-safe collector for non-item errors and skipped-item warnings during a batch run."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._errors: list[str] = []

    def record(self, message: str) -> None:
        cleaned = str(message).strip()
        if not cleaned:
            return
        with self._lock:
            if cleaned not in self._errors:
                self._errors.append(cleaned)

    def get_errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)


_active_error_collector_var: contextvars.ContextVar[BatchErrorCollector | None] = (
    contextvars.ContextVar("active_error_collector", default=None)
)


@contextmanager
def batch_error_context(
    collector: BatchErrorCollector | None = None,
) -> Iterator[BatchErrorCollector]:
    """Bind a BatchErrorCollector to the current async/thread execution context."""
    active_collector = collector or BatchErrorCollector()
    token = _active_error_collector_var.set(active_collector)
    try:
        yield active_collector
    finally:
        _active_error_collector_var.reset(token)


def get_active_error_collector() -> BatchErrorCollector | None:
    """Return the active BatchErrorCollector if inside a batch run."""
    return _active_error_collector_var.get()


def record_batch_error(message: str) -> None:
    """Record an error message on the active BatchErrorCollector if one is bound."""
    collector = _active_error_collector_var.get()
    if collector is not None:
        collector.record(message)
