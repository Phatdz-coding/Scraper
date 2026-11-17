# Implement Multi-Stage Link & Document Type Classifiers

Design and implement a modular, extensible link classification system to classify URLs injected by users (via a `.txt` file or a Python `list[str]`) before and during scraping.

## Context & Existing Codebase

Currently, [`classifiers.py`](./classifiers.py) only contains DOM/SVG icon classifiers (`DriveElement`, `classify_element`, `generate_url`) designed for Google Drive folder scraping, along with the shared `DocumentType` and `WebsiteType` enums.

Additionally, [`template_info.py`](./template_info.py) contains reference logic in `Template.__check_document_type()` and `Template.__match_domain()` showing how Google Drive, Google Docs, and Scribd pages are identified in Playwright.

## Scope & Constraints

- **Do NOT** implement any website or document scrapers (`pdf_scraper`, `ggdoc_scraper`, `scribd_scraper`, etc.).
- **Do NOT** modify existing scrapers (`folder_scraper.py`, `pdf_scraper.py`, `ggdoc_scraper.py`, `scrape.py`) or `template_info.py`.
- **Do NOT** add tests unless explicitly requested.
- Refactor `classifiers.py` into a `classifiers/` package (removing the standalone `classifiers.py` file to prevent Python module/package shadowing).
- **Preserve 100% backward compatibility**: `classifiers/__init__.py` MUST re-export all existing public symbols (`DocumentType`, `WebsiteType`, `DriveElement`, `matches_vector`, `is_pdf`, `is_folder`, `is_microsoft_word`, `is_image`, `is_google_doc`, `is_xlsx`, `is_pptx`, `is_zip_folder`, `classify_element`, `generate_url`) so `folder_scraper.py`, `template_info.py`, and `scrape.py` continue to work unchanged.
- Use Python 3.12+ type annotations, `playwright.sync_api.Page`, `urllib.parse`, `dataclasses`, `abc`/`Protocol`, and `rich` (consistent with `folder_scraper.py`) for warning logs.

---

## Core Data Model

Keep the existing `WebsiteType` and `DocumentType` enums:
- `WebsiteType`: `GOOGLE_DRIVE`, `GOOGLE_DOC`, `SCRIBD`
- `DocumentType`: `FOLDER`, `PDF`, `GOOGLE_DOC`, `MICROSOFT_WORD`, `PPTX`, `IMAGE` (aliases: `JPG`, `PNG`, `JPEG`), `XLSX`, `ZIP_FOLDER`, `UNSUPPORTED_FORMAT`

> **Note:** Since `JPG`, `PNG`, `JPEG` are enum aliases of `IMAGE`, `.name` resolves to `"IMAGE"`.

Define a dataclass (e.g., `ClassifiedLink`):
- `url: str` — normalized URL string (without surrounding quotes)
- `website_type: WebsiteType`
- `document_type: DocumentType | None = None`

Serialization requirements (`to_tag()` / `__str__`):
- When `document_type is None`:
  `f"{self.url}.{self.website_type.name}"`
  Example: `https://drive.google.com/file/d/10pYur59yGoU42E1D13YU_rbYModCl9xl/view.GOOGLE_DRIVE`
- When `document_type is not None`:
  `f"{self.url}.{self.website_type.name}.{self.document_type.name}"`
  Example: `https://www.scribd.com/document/524804738/Math-With-Bad-Drawings.SCRIBD.PDF`
- Provide a clean method/helper to update `document_type` (or return an updated `ClassifiedLink`) while preserving the rest of the list.

---

## Classification Pipeline Requirements

### 1. Input Parsing & Root Classifier (Website Type)

**Responsibility:** Accept either a `.txt` file path (`Path | str`) or a `Sequence[str]` / `list[str]` of URLs, determine each link's `WebsiteType` from its domain/URL structure, and return `list[ClassifiedLink]` (with `document_type=None`).

#### `.txt` Comment & Line Parsing Rules
The parser must cleanly strip comments and extract URLs from injected `.txt` files:
1. **Multi-line / block comments:**
   - Ignore everything enclosed in `/* ... */` (single-line or spanning multiple lines).
   - Ignore everything enclosed in `""" ... """` (single-line or spanning multiple lines).
2. **Line & inline comments:**
   - Ignore full-line comments starting with `#` or `//` (after stripping leading whitespace).
   - Strip trailing inline comments starting with `#` or `//` **only when preceded by whitespace** (or outside `http://` / `https://` scheme boundaries) so URL schemes like `https://` and valid URL `#` fragments (if any) are never corrupted.
3. **Whitespace & empty lines:**
   - Strip leading/trailing whitespace (and optional surrounding quotes if a line is just `"https://..."`) on each remaining line, and skip empty lines.
4. **URL validation:**
   - Validate that each extracted URL has an `http` or `https` scheme and a non-empty `netloc`.
   - If a non-empty entry is malformed or belongs to an unsupported domain, log an orange warning via `rich` and skip it (do not abort the batch).

#### Website Matching Rules
- `drive.google.com` → `WebsiteType.GOOGLE_DRIVE`
  - Examples:
    - `https://drive.google.com/file/d/{fileID}/...`
    - `https://drive.google.com/drive/folders/{folderID}`
    - `https://drive.google.com/open?id={fileID}`
- `docs.google.com` → `WebsiteType.GOOGLE_DOC`
  - Examples:
    - `https://docs.google.com/document/d/{fileID}/...`
    - `https://docs.google.com/spreadsheets/d/{fileID}/...`
    - `https://docs.google.com/presentation/d/{fileID}/...`
- `scribd.com` (including `www.scribd.com`) → `WebsiteType.SCRIBD`
  - Examples:
    - `https://www.scribd.com/document/{numericID}/...`
    - `https://www.scribd.com/doc/{numericID}/...`
  - Note: Unlike Google Drive/Docs alphanumeric string IDs, Scribd document IDs in the URL path are numeric (`\d+`).

### 2. Stage 2a: URL-Based Document Type Classifier (Pre-Browser Fast Path)

**Responsibility:** Determine `DocumentType` purely from the URL (without launching Playwright) whenever the URL unambiguously encodes the document format. Update matching `ClassifiedLink` entries in the list; leave ambiguous entries with `document_type=None`.

URL classification rules:
- `WebsiteType.SCRIBD` → `DocumentType.PDF` (always)
- `WebsiteType.GOOGLE_DOC`:
  - URL path contains `/spreadsheets/` → `DocumentType.XLSX`
  - URL path contains `/presentation/` → `DocumentType.PPTX`
  - URL path contains `/document/` → **leave `document_type=None`** (must be checked in Playwright to distinguish `GOOGLE_DOC` vs `MICROSOFT_WORD` `.docx`)
- `WebsiteType.GOOGLE_DRIVE`:
  - URL path contains `/folders/` → `DocumentType.FOLDER`
  - All other Google Drive file URLs (`/file/d/...`, `/open?id=...`) → **leave `document_type=None`** (must be checked in Playwright)

### 3. Stage 2b: Playwright Page-Based Document Type Classifier (Runtime)

**Responsibility:** Activated when scraping starts and a link (whose `document_type` is still `None`) has been opened in a Playwright `Page`. Receives the `Page` and the `ClassifiedLink` (or `WebsiteType`), determines the `DocumentType`, and updates the `ClassifiedLink` so its tagged representation becomes `f"{url}.{WEBSITE_TYPE}.{DOCUMENT_TYPE}"`.

Replicate the classification workflow and DOM/JS inspection mechanism of `Template.__check_document_type()` in `template_info.py`:

- **`WebsiteType.GOOGLE_DRIVE`**:
  1. If `"folder"` (or `"/folders/"`) is in `page.url` → `DocumentType.FOLDER`.
  2. Evaluate `window.viewerData?.config?.title` via `page.evaluate(...)`; if unavailable, fall back to regex search for `window.viewerData...title` in `page.content()`.
  3. If `title` has a file extension (`title.rsplit(".", 1)[-1].lower()`), map:
     - `pdf` → `DocumentType.PDF`
     - `pptx`, `ppt` → `DocumentType.PPTX`
     - `doc`, `docx` → `DocumentType.MICROSOFT_WORD`
     - `xlsx`, `xls` → `DocumentType.XLSX`
     - `png`, `jpg`, `jpeg` → `DocumentType.IMAGE`
     - `zip` → `DocumentType.ZIP_FOLDER`
  4. Fallback logo check when `"/file/"` is in `urlparse(page.url).path`: check if the Google Docs blue SVG icon is attached in the DOM (`svg[height="24"][width="24"][viewBox="3 3 18 18"][fill="none"][xmlns="http://www.w3.org/2000/svg"]` containing `<g>` with `<rect x="3" y="3" width="18" height="18" rx="3" fill="#3186FF">`, `<rect x="7" y="15" width="6" height="2" rx="1" fill="white">`, and `<rect x="7" y="11" width="10" height="2" rx="1" fill="white">`, short timeout e.g. `1500ms`) → `DocumentType.GOOGLE_DOC`.
  5. If none match, return `DocumentType.UNSUPPORTED_FORMAT`.

- **`WebsiteType.GOOGLE_DOC`**:
  1. Inspect `urlparse(page.url).path.lower()`:
     - `"/spreadsheets/"` in path → `DocumentType.XLSX`
     - `"/presentation/"` in path → `DocumentType.PPTX`
     - `"/document/"` in path → parse `<title>` (from `page.title()` or `HTMLParser` on `page.content()`); if `".docx"` in `title.lower()`, return `DocumentType.MICROSOFT_WORD`, else return `DocumentType.GOOGLE_DOC`.
  2. Otherwise return `DocumentType.UNSUPPORTED_FORMAT`.

- **`WebsiteType.SCRIBD`**:
  - Always return `DocumentType.PDF`.

---

## Architecture & Design Principles (OOP & SOLID)

Organize the `classifiers/` package cleanly into focused modules (e.g., `models.py`, `drive_dom.py`, `input_parser.py`, `website_classifier.py`, `url_doc_classifier.py`, `page_doc_classifier.py`, `pipeline.py`, `__init__.py`):

- **Single Responsibility (SRP):** Separate `.txt`/URL parsing, website/domain classification, URL-based document classification, Playwright page-based document classification, and legacy DOM-icon classification into distinct classes/modules.
- **Open/Closed (OCP):** Use strategy interfaces (`ABC` or `Protocol`) and registry/mapping dispatch by `WebsiteType` so adding a new website (e.g., OneDrive, Dropbox) or document rule requires adding a new strategy class without modifying existing classifiers.
- **Liskov Substitution & Interface Segregation (LSP / ISP):** Keep URL-only and Page-based classifier interfaces small and distinct so callers never depend on Playwright when running URL-only pre-classification.
- **Dependency Inversion (DIP):** A coordinator/pipeline class (e.g., `LinkClassificationPipeline`) should depend on classifier abstractions, allowing custom strategies or parsers to be injected for testing or future expansion.

---

## Acceptance Criteria

1. Existing imports from `classifiers` in `folder_scraper.py`, `template_info.py`, and `scrape.py` work without any edits to those files.
2. Root classifier accepts both a `.txt` file path and a `list[str]`.
3. `.txt` parser strips `/* ... */` and `""" ... """` block comments as well as `#` and `//` full-line and inline comments without corrupting `http://` or `https://` URLs, skips blank lines, and warns on invalid/unsupported URLs.
4. `ClassifiedLink` serializes cleanly to `"{url}.{WEBSITE_TYPE}"` before document classification and `"{url}.{WEBSITE_TYPE}.{DOCUMENT_TYPE}"` after document classification.
5. URL-based Stage 2 classifier resolves Scribd (`PDF`), Google Sheets (`XLSX`), Google Slides (`PPTX`), and Google Drive folders (`FOLDER`), leaving ambiguous URLs (`drive.google.com/file/...`, `docs.google.com/document/...`) pending (`document_type=None`).
6. Page-based Stage 2 classifier accepts a Playwright `Page` and a `ClassifiedLink` (or `WebsiteType`), resolves the `DocumentType` using the `template_info.py` rules, and updates the `ClassifiedLink` entry.
7. No scrapers are implemented and no unrelated files are modified.
