import sys
from threading import Lock
from rich.live import Live
from rich.text import Text


class ScrapeProgress:
    """Thread-safe counter for pages completed by the PDF scraper."""

    def __init__(self) -> None:
        self._count = 0
        self._lock = Lock()
        self._live: "Live | None" = None  # lazy-initialized Rich Live display

    def _get_live(self) -> "Live | None":
        """Return a shared Rich Live instance, or None if Rich is unavailable."""
        if self._live is None:
            try:
                # from rich.live import Live

                self._live = Live("", refresh_per_second=10)
                self._live.start()
            except ImportError:
                pass
        return self._live

    def increment(self, total_pages: int) -> None:
        if total_pages <= 0:
            raise ValueError("total_pages must be greater than zero")

        with self._lock:
            self._count += 1
            percentage = self._count / total_pages * 100

            live = self._get_live()
            if live is not None:
                # from rich.text import Text

                text = Text()
                text.append("Scraped pages: ", style="bold yellow")
                text.append(f"{self._count}/{total_pages} | {percentage:.1f}%")
                live.update(text)
            else:
                # Plain-text fallback: \r returns to line start, \033[K clears
                sys.stdout.write(
                    f"\rScraped pages: {self._count}/{total_pages} | {percentage:.1f}%"
                )
                sys.stdout.flush()

    def finish(self) -> None:
        """Stop the live display (call once scraping is done)."""
        with self._lock:
            if self._live is not None:
                self._live.stop()
                self._live = None
            else:
                # Print a final newline so the terminal prompt isn't on the same line
                sys.stdout.write("\n")
                sys.stdout.flush()

    def get_count(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


scrape_progress = ScrapeProgress()


def get_scraped_page_count() -> int:
    """Return the number of PDF pages scraped so far."""
    return scrape_progress.get_count()
