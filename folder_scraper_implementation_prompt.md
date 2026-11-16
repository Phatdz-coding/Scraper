# Implement a Google Drive Folder Scraper Module

Modify the scraper module in [`folder_scraper.py`](./folder_scraper.py) according to the requirements below.

## Scope

Implement only the Google Drive folder scraping functionality described here. Follow the existing project structure and configuration conventions.

Do not add tests unless explicitly requested. Do not modify unrelated functionality.

Use:

- Python
- Playwright synchronous API
- `playwright.sync_api.Page`
- `typer`
- `rich`

The implementation must be designed as a reusable module and should follow OOP and SOLID principles where appropriate.

## Public API

The module must expose a single `scrape()` function that supports both a Google Drive URL and an existing Playwright page.

Use one function with a union-style input rather than attempting to define two Python functions with the same name:

```python
def scrape(source: str | Page) -> list[str]:
    ...
```

### URL input

When `scrape()` receives a `str`:

1. Validate that the URL is a Google Drive folder URL.
2. The URL must contain the `folders` path segment.
3. If the initial URL is invalid, raise an appropriate error.
4. Initialize a Playwright browser and page through a separate external browser-initialization function.
5. Navigate to the supplied folder URL.
6. Call the page-based scraping logic using the initialized `Page`.
7. Process all queued folders until traversal is complete.
8. Move to the next URL/folder after a folder succeeds, times out, or is inaccessible.
9. Clean up the browser resources after the complete URL-based scrape finishes or fails.

The browser-initialization logic must be separated into its own function so it can be reused or replaced by callers.

### Page input

When `scrape()` receives a `Page`:

1. Use the existing page directly.
2. Do not initialize or close the browser.
3. Perform the core scraping logic on the current page.
4. Return the discovered file URLs as `list[str]`.

The page-based implementation must be the core implementation. URL mode should primarily handle browser setup, navigation, cleanup, and delegation to the page-based logic.

## Configuration

Use the existing values in [`config.py`](./config.py) wherever applicable.

Add global configuration values to `config.py` for:

```python
SCRAPE_TIMEOUT_SECONDS = 15
SCRAPE_RETRY_COUNT = 2
```

The values must be configurable without changing the scraper implementation.

Use the existing `FOLDER_MAX_DEPTH` configuration to limit recursive traversal.

Define depth consistently:

- The initial folder has depth `0`.
- A direct child folder has depth `1`.
- Do not process child folders whose depth exceeds `FOLDER_MAX_DEPTH`.
- When the maximum depth is reached, stop traversing deeper folders.
- Files discovered in folders that are processed before the limit must still be returned.

## Folder traversal

Traverse subfolders recursively using breadth-first search.

Requirements:

1. Start with the initial folder URL.
2. Read the current folder’s elements.
3. Identify files and subfolders.
4. Add discovered subfolders to a FIFO queue.
5. Process queued folders in BFS order.
6. Continue until the queue is empty or `FOLDER_MAX_DEPTH` is reached.
7. Return file URLs only.
8. Do not include folder URLs in the returned result.
9. Ignore duplicate files while preserving their first-discovery order.
10. Avoid duplicate folders where necessary to prevent repeated traversal.

If a child folder is inaccessible:

- Log the error.
- Skip that folder.
- Continue processing the remaining queued folders.

An inaccessible child folder must not abort the entire scrape.

## Extracting Drive elements

Create a function equivalent to:

```python
def get_file_elements(page: Page) -> list[...]:
    ...
```

It must execute the following JavaScript logic in the browser page:

```javascript
document.querySelectorAll('[data-target=doc]')
```

The returned DOM elements must be converted into a structure suitable for later processing. Do not pass an unprocessed browser `NodeList` through the rest of the application.

Each element is expected to contain:

- `data-id`: the Google Drive file or folder ID
- `tabindex`: used when logging unsupported elements
- SVG/icon information used for format detection

The sample DOM structure is available in [`sample.txt`](../../../../home/phatlinux/Downloads/sample.txt). Use it as the reference for the expected folder and icon markup.

Do not assume malformed DOM elements exist.

## Element classification

Create a classification module or equivalent logic that assigns exactly one of these seven labels to each element:

1. `pdf`
2. `google_doc`
3. `microsoft_word`
4. `image`
5. `xlsx`
6. `folder`
7. `unknown`

The classification logic should be organized into separate functions. Each detector should accept one extracted element and return a boolean result.

Use the following detection rules.

### PDF

A PDF element contains exactly one `<path>` element whose `d` attribute matches `config.pdf_icon_vector`.

### Folder

A folder element contains an SVG `<path>` whose `d` attribute matches `config.folder_icon_vector`.

The folder icon vector in `config.py` is based on the structure shown in [`sample.txt`](../../../../home/phatlinux/Downloads/sample.txt).

### Microsoft Word

A Microsoft Word element contains an SVG `<path>` whose `d` attribute matches `config.microsoft_word_icon_vector`.

### Image

An image element contains an SVG `<path>` whose `d` attribute matches `config.png_icon_vector`.

The image classification must support:

- PNG
- JPG
- JPEG
- WEBP

Use the available icon structure and configuration values consistently. Do not classify unrelated formats as images.

### Google Docs

A Google Docs element is recognized through pattern matching against the configured Google Docs icon structure:

```python
config.google_doc_icon_element
```

### XLSX

An XLSX element is recognized through pattern matching against:

```python
config.xlsx_icon_element
```

### Unknown

If no supported detector matches, classify the element as `unknown`.

The detectors should be evaluated in a deterministic order, and classification should return only one final label per element.

## URL generation

Before generating a URL, extract the element’s `data-id`.

Generate URLs according to the element label:

| Label | URL format |
|---|---|
| `pdf` | `https://drive.google.com/file/d/{file_id}/view` |
| `google_doc` | `https://drive.google.com/file/d/{file_id}/view` |
| `microsoft_word` | `https://drive.google.com/file/d/{file_id}/view` |
| `image` | `https://drive.google.com/file/d/{file_id}/view` |
| `xlsx` | `https://docs.google.com/spreadsheets/d/{file_id}/view` |
| `folder` | `https://drive.google.com/drive/folders/{file_id}` |
| `unknown` | No URL |

The final public result must contain file URLs only. Folder URLs may be generated internally for traversal but must not be returned from `scrape()`.

## Duplicate handling

The final result must be:

```python
list[str]
```

It must contain URLs only.

When a file is discovered more than once:

- Ignore subsequent duplicates.
- Keep the URL from its first discovery.
- Preserve discovery order for unique URLs.

Use file IDs or normalized URLs as the deduplication key.

Duplicate files must not interrupt traversal or classification.

## Unknown elements and logging

Unknown elements must:

1. Be excluded from the returned URL list.
2. Produce an orange warning.
3. Not stop the scrape.
4. Allow processing to continue with the next element.

Log unsupported elements using a dedicated function that receives the file ID and `tabindex`.

The warning format must be:

```text
File ID not supported: {fileID} | Tab Index: {tabindex}
```

Use `typer` and `rich` directly for terminal output.

Use orange output for unknown elements.

## Error handling and retries

### Initial URL

The initial input URL must be validated before navigation.

If it is not a valid Google Drive folder URL, raise an appropriate error. At minimum, the URL must contain the `folders` path segment.

### Folder accessibility

If a folder cannot be accessed:

- Log the error in red or orange.
- Skip that folder.
- Continue with the next queued folder.
- Do not return a folder URL as a result.

### Timeouts

Use the configurable timeout from `config.py`, with a default of 15 seconds.

If navigation or folder processing times out:

- Log the timeout error.
- Retry according to `SCRAPE_RETRY_COUNT`.
- After all retries are exhausted, skip the current folder and continue with the next queued folder.

### Retry behavior

The default retry count is 2.

Retry only the failed folder/navigation operation. Do not restart the entire traversal unnecessarily.

Errors must be visible through `typer`/`rich`; do not silently swallow exceptions or return success-shaped fallback values.

## Browser lifecycle

Provide a separate browser initialization function.

URL mode owns the browser lifecycle:

- Initialize the browser and page.
- Process the initial folder and all queued folders.
- Continue to the next URL after each folder succeeds, times out, or is skipped.
- Close browser resources after all queued URLs have been processed or after an unrecoverable initial-input failure.

Page mode does not own the browser lifecycle:

- Reuse the supplied `Page`.
- Do not close the page or browser.
- Return the discovered file URLs.

## Code quality requirements

- Keep scraping, browser initialization, DOM extraction, classification, URL generation, traversal, logging, and retry handling separated into focused functions or classes.
- Reuse existing configuration constants.
- Avoid global mutable result state unless it is necessary for compatibility with the existing module design.
- Prefer explicit types and clear return values.
- Preserve first-discovery ordering.
- Do not add unrelated dependencies.
- Do not add tests for this task.
- Do not implement behavior outside the requirements in this prompt.

## Acceptance criteria

The implementation is complete when:

1. `scrape(url: str)` initializes a browser and delegates core processing to page-based logic.
2. `scrape(page: Page)` works with an existing Playwright page without owning browser cleanup.
3. Folder traversal uses BFS.
4. Traversal stops at `config.FOLDER_MAX_DEPTH`.
5. Only file URLs are returned.
6. Duplicate files are removed while preserving first-discovery order.
7. All seven labels are supported.
8. PNG, JPG, JPEG, and WEBP images are recognized.
9. Unsupported elements are logged in orange and ignored.
10. Invalid initial URLs raise errors.
11. Inaccessible or timed-out child folders are logged, retried when applicable, skipped, and do not abort the remaining traversal.
12. Timeout and retry values are configurable through [`config.py`](./config.py).
13. No browser resources are leaked in URL mode.
14. No browser resources are closed unexpectedly in page mode.
15. The public return type remains `list[str]`.
