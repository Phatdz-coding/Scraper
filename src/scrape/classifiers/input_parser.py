"""Input parser for extracting normalized URLs from .txt files or string sequences."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path


class InputParser(ABC):
    """Abstract interface for parsing raw link inputs into candidate URL strings."""

    @abstractmethod
    def parse(self, source: Path | str | Sequence[str]) -> list[str]:
        """Parse a .txt file path, raw text, or sequence of strings into candidate URLs."""


class LinkInputParser(InputParser):
    """Parses .txt files or sequences of URL strings while stripping block and line comments.

    Supported comment formats:
    - Multi-line or inline block comments: ``/* ... */`` and ``\"\"\" ... \"\"\"``
    - Full-line comments starting with ``#`` or ``//`` (ignoring leading whitespace)
    - Inline comments starting with ``#`` or ``//`` when preceded by whitespace
    """

    def parse(self, source: Path | str | Sequence[str]) -> list[str]:
        """Extract candidate URLs from a file path, raw text string, or sequence of strings."""
        if isinstance(source, Path):
            text = source.read_text(encoding="utf-8")
            return self.parse_text(text)

        if isinstance(source, str):
            stripped = source.strip()
            path_candidate = Path(stripped)
            if stripped and "\n" not in stripped and "\r" not in stripped:
                try:
                    if path_candidate.is_file():
                        return self.parse_text(
                            path_candidate.read_text(encoding="utf-8")
                        )
                except OSError:
                    pass
                if stripped.lower().endswith(".txt") and not stripped.lower().startswith(
                    ("http://", "https://")
                ):
                    return self.parse_text(path_candidate.read_text(encoding="utf-8"))
            return self.parse_text(source)

        if isinstance(source, Sequence):
            combined_text = "\n".join(str(item) for item in source)
            return self.parse_text(combined_text)

        raise TypeError(
            f"Unsupported input source type: {type(source).__name__}. "
            "Expected Path, str, or Sequence[str]."
        )

    def parse_file(self, file_path: Path | str) -> list[str]:
        """Read and parse candidate URLs from a .txt file."""
        path = Path(file_path)
        return self.parse_text(path.read_text(encoding="utf-8"))

    def parse_text(self, text: str) -> list[str]:
        """Strip block and line comments from text and return non-empty normalized lines."""
        uncommented = self._strip_comments(text)
        urls: list[str] = []
        for raw_line in uncommented.splitlines():
            cleaned = self._clean_line(raw_line)
            if cleaned:
                urls.append(cleaned)
        return urls

    @staticmethod
    def _strip_comments(text: str) -> str:
        """Single-pass scanner that strips /* ... */, \"\"\" ... \"\"\", and # / // comments."""
        result: list[str] = []
        i = 0
        n = len(text)
        line_has_non_ws = False

        while i < n:
            # 1. Multi-line / inline /* ... */ block comment
            if text.startswith("/*", i):
                end_idx = text.find("*/", i + 2)
                if end_idx == -1:
                    break
                segment = text[i + 2 : end_idx]
                if "\n" in segment:
                    result.append("\n")
                    line_has_non_ws = False
                else:
                    result.append(" ")
                i = end_idx + 2
                continue

            # 2. Multi-line / inline """ ... """ block comment
            if text.startswith('"""', i):
                end_idx = text.find('"""', i + 3)
                if end_idx == -1:
                    break
                segment = text[i + 3 : end_idx]
                if "\n" in segment:
                    result.append("\n")
                    line_has_non_ws = False
                else:
                    result.append(" ")
                i = end_idx + 3
                continue

            ch = text[i]
            prev_ch = text[i - 1] if i > 0 else "\n"
            preceded_by_ws_or_bol = (not line_has_non_ws) or (
                prev_ch in {" ", "\t", "\r", "\n"}
            )

            # 3. Line or inline comment starting with # or //
            is_hash_comment = ch == "#" and preceded_by_ws_or_bol
            is_slash_comment = text.startswith("//", i) and (
                preceded_by_ws_or_bol or prev_ch != ":"
            )
            if is_hash_comment or is_slash_comment:
                newline_idx = text.find("\n", i)
                if newline_idx == -1:
                    break
                result.append("\n")
                line_has_non_ws = False
                i = newline_idx + 1
                continue

            result.append(ch)
            if ch == "\n":
                line_has_non_ws = False
            elif not ch.isspace():
                line_has_non_ws = True
            i += 1

        return "".join(result)

    @staticmethod
    def _clean_line(line: str) -> str:
        """Strip whitespace and optional surrounding quotes from a single line."""
        cleaned = line.strip()
        if not cleaned:
            return ""
        if (
            len(cleaned) >= 2
            and cleaned[0] == cleaned[-1]
            and cleaned[0] in {'"', "'"}
        ):
            cleaned = cleaned[1:-1].strip()
        return cleaned


def parse_links_input(
    source: Path | str | Sequence[str],
    parser: InputParser | None = None,
) -> list[str]:
    """Convenience function to parse a .txt file path or sequence of URLs."""
    active_parser = parser or LinkInputParser()
    return active_parser.parse(source)
