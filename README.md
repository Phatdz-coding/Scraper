# Document Scraper

A powerful CLI web scraping tool that supports various types of documents — including PDF, PPTX, DOCX, Google Docs, images, ZIP archives, and Google Drive folders — as well as scraping documents from the Scribd platform.  
*No log in required*
## Installation

### Linux & macOS

Run the following command in your terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/Phatdz-coding/Scraper/release/install.sh | bash
```

### Windows

Press `Win + R`, type `powershell` to launch PowerShell, and run:

```powershell
irm https://raw.githubusercontent.com/Phatdz-coding/Scraper/release/install.ps1 | iex
```

### Verify Installation

In your terminal, run:

```bash
scrape --help
```

## How to Use

```bash
scrape [OPTIONS] <source>
```

Where `<source>` (required) is either a path to a `.txt` file containing URLs, or a single document/folder URL to scrape.

### Examples

#### 1. Scrape a Single File

```bash
scrape "https://drive.google.com/file/d/0B1HXnM1lBuoqMzVhZjcwNTAtZWI5OS00ZDg3LWEyMzktNzZmYWY2Y2NhNWQx/view"
```

or a Google Doc:

```bash
scrape "https://docs.google.com/document/d/{fileID}/view"
```

#### 2. Scrape a Google Drive Folder

```bash
scrape "https://drive.google.com/drive/folders/{folderID}"
```

#### 3. Scrape a List of Links (`links.txt`)

Suppose you have a `links.txt` file containing a list of Google Drive and Scribd links:

```bash
scrape links.txt
```

**`links.txt` Format & Constraints:**
- Must be a UTF-8 encoded `.txt` file with **one URL per line**.
- Blank lines are ignored, and optional surrounding single (`'...'`) or double (`"..."`) quotes on a line are stripped automatically.
- Supports comments:
  - Line or inline comments starting with `#` or `//` (at the start of a line or preceded by whitespace).
  - Multi-line or inline block comments using `/* ... */` or `""" ... """`.

Example `links.txt`:

```text
https://drive.google.com/file/d/{fileID}/view
#https://drive.google.com/drive/u/1/folders/{folderID}/*
//https://docs.google.com/document/d/{fileID}/view
#https://www.scribd.com/document/{fileID}/*
```

## Configurations

You can customize the scraper's behavior using the following CLI options:

### `--concurrency`, `-c`
- **Type / Range:** Integer (`x >= 1`)
- **Default:** `5`
- **Explanation:** Sets the maximum number of concurrent scraping workers that share a single available browser instance. Increasing this value speeds up large batch jobs, while lowering it reduces CPU/memory usage and helps avoid rate limits.
- **Example:**
  ```bash
  scrape links.txt -c 8
  ```

### `--output`, `-o`
- **Type:** Path
- **Default:** `output`
- **Explanation:** Specifies the destination directory where all scraped documents and folder structures will be saved. If the directory does not exist, it will be created automatically.
- **Example:**
  ```bash
  scrape links.txt -o ./downloads
  ```

### `--scrape-type`, `-t`
- **Type:** String (`d` or `s`)
- **Default:** `d`
- **Explanation:** Controls the scraping strategy used for documents:
  - `d` (**direct download first**): Attempts to download the original file directly (e.g., PDF/DOCX/PPTX) and falls back to viewer scraping if direct download is unavailable.
  - `s` (**viewer screenshot/API**): Forces scraping through the document viewer (capturing/fetching rendered page images and assembling them).
- **Example:**
  ```bash
  scrape "https://drive.google.com/file/d/{fileID}/view" -t s
  ```

### `--headless-disable`, `-hd`
- **Type:** Boolean flag
- **Default:** Disabled (`false` — runs headless by default)
- **Explanation:** Disables headless browser mode and opens a visible window of the available browser. Useful for debugging, observing scraping progress visually, or inspecting pages that behave differently in headless mode.
- **Example:**
  ```bash
  scrape "https://drive.google.com/file/d/{fileID}/view" -hd
  ```

### `--use-gg-api`, `-ugga`
- **Type:** Optional string (`[<api_key>]`)
- **Default:** Disabled
- **Explanation:** Enables the Google Drive API for faster and more reliable Google Drive folder scraping instead of relying solely on browser DOM traversal. You can pass `-ugga` / `--use-gg-api` by itself to use the configured API key, or supply an `<api_key>` value to override it at runtime.
- **Example:**
  ```bash
  # Use the pre-configured Google Drive API key
  scrape "https://drive.google.com/drive/folders/{folderID}" -ugga

  # Override with a specific API key
  scrape "https://drive.google.com/drive/folders/{folderID}" --use-gg-api YOUR_GOOGLE_API_KEY
  ```

### `--concurrent-folder`, `-cf`
- **Type / Range:** Integer (`x >= 1`)
- **Default:** `10`
- **Explanation:** Sets the maximum number of Google Drive folders to visit and expand concurrently. Higher values speed up traversing trees with many subfolders, while lower values reduce browser tab pressure.
- **Example:**
  ```bash
  scrape "https://drive.google.com/drive/folders/{folderID}" -cf 5
  ```

### `--folder-timeout`, `-ft`
- **Type / Range:** Integer in seconds (`x >= 1`)
- **Default:** `10`
- **Explanation:** Maximum time (in seconds) to wait when opening a Google Drive folder before timing out. It is recommended to keep `--folder-timeout` greater than or equal to `--concurrent-folder` so folders have enough time to load under load.
- **Example:**
  ```bash
  scrape "https://drive.google.com/drive/folders/{folderID}" -cf 10 -ft 20
  ```

### `--max-folder-depth`, `-mfd`
- **Type / Range:** Integer (`1 <= x <= 100`)
- **Default:** `2`
- **Explanation:** Controls how many levels of nested subfolders the scraper will traverse when expanding a Google Drive folder. Increase this if your target folder has deeply nested subdirectories.
- **Example:**
  ```bash
  scrape "https://drive.google.com/drive/folders/{folderID}" -mfd 5
  ```

### `--max-retry`, `-mr`
- **Type / Range:** Integer (`0 <= x <= 10`)
- **Default:** `2`
- **Explanation:** Maximum number of retry attempts when a scrape operation fails due to transient network errors, timeouts, or page load issues. Set to `0` to disable retries.
- **Example:**
  ```bash
  scrape links.txt -mr 4
  ```

### `--max-concurrent-img-api`, `-mcia`
- **Type / Range:** Integer (`1 <= x <= 30`)
- **Default:** `20`
- **Explanation:** Sets the maximum number of concurrent viewer image API requests sent per batch when scraping documents page-by-page (such as in viewer/screenshot mode). Lowering this can help prevent rate limiting on large documents.
- **Example:**
  ```bash
  scrape "https://drive.google.com/file/d/{fileID}/view" -t s -mcia 10
  ```

## Credit

**[Phat-coding](https://github.com/Phatdz-coding)**
