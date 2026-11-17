# Google Drive Folder Scraper Sequence Diagrams

This document illustrates the execution lifecycles and interactions across the components in [`folder_scraper.py`](file:///media/New%20Volume/projects/Python/Scraper/folder_scraper.py).

---

## 1. Top-Level Entry & Engine Dispatch Flow

When a caller invokes `scrape()` or `scrape_classified()`, `_scrape_source()` routes execution to either the **Google Drive v3 REST API**, the **Async Playwright Multi-Tab Crawler**, or the **Sync Playwright Sequential Crawler**.

```mermaid
sequenceDiagram
    autonumber
    actor Caller
    participant Module as folder_scraper
    participant Scraper as DriveFolderScraper

    Caller->>Module: scrape(source) or scrape_classified(source)
    activate Module

    alt source is str (URL)
        Module->>Module: validate_drive_folder_url(source)
        Module->>Module: should_use_drive_api()

        alt USE_GOOGLE_DRIVE_API is True and GOOGLE_API_KEY is set
            Module->>Module: extract_folder_id(source)
            Module->>Module: _scrape_by_api(folder_id, api_key)
            Note over Module: Runs REST API flow (see Diagram 3)
        else Playwright Browser Crawling
            Module->>Module: FolderScrapeSettings.from_config()
            Module->>Module: _run_blocking(_crawl_with_browser(url, settings))
            Note over Module: Runs Async Browser flow (see Diagram 2)
        end

    else source is sync Page
        Module->>Scraper: DriveFolderScraper(source)
        activate Scraper
        Module->>Scraper: scrape()
        Note over Scraper: Runs Sync Page flow (see Diagram 4)
        Scraper-->>Module: discovered items
        deactivate Scraper
    end

    Module-->>Caller: discovered_file_urls or discovered_file_links
    deactivate Module
```

---

## 2. Playwright Async Multi-Tab Concurrent Crawl (URL Mode)

When crawling a folder URL with Playwright, a single `AsyncBrowser` and shared `AsyncBrowserContext` are spawned. The queue is drained concurrently using an `asyncio.Semaphore`, reading each folder in a short-lived tab with asset route blocking.

```mermaid
sequenceDiagram
    autonumber
    participant Engine as _crawl_with_browser
    participant Scraper as DriveFolderScraper
    participant BrowserMgr as Chromium and SharedContext
    participant Reader as PlaywrightFolderElementReader
    participant AsyncTab as AsyncPage
    participant Classifiers as classifiers

    Engine->>Scraper: get_instance()
    Engine->>BrowserMgr: launch_chromium(headless)
    activate BrowserMgr
    BrowserMgr-->>Engine: browser
    Engine->>BrowserMgr: _open_shared_context(browser)
    BrowserMgr-->>Engine: shared context with route blocking

    Engine->>Reader: PlaywrightFolderElementReader(browser, settings, context)
    Engine->>Scraper: crawl(root_url, reader, settings)
    activate Scraper

    Scraper->>Scraper: _start_session(settings)
    Scraper->>Scraper: enqueue root_url at depth 0

    loop While queue has items or tasks are in-flight
        Scraper->>Scraper: slots.acquire() via asyncio.Semaphore
        Scraper->>Scraper: pop (folder_url, depth) from queue

        Scraper->>Reader: _visit_folder calls read(folder_url)
        activate Reader

        loop Retry attempts up to max_retries + 1
            Reader->>AsyncTab: open new tab via _open_tab()
            activate AsyncTab
            Reader->>AsyncTab: goto(folder_url, wait_until commit)

            alt HTTP status 400 or higher
                Reader-->>Reader: raise _InaccessibleFolderError
                Note over Reader: Inaccessible folder, skip without retrying
            else HTTP status OK
                Reader->>AsyncTab: wait_for_selector(ITEMS_SELECTOR)
                Reader->>AsyncTab: evaluate(_FILE_ELEMENTS_JS)
                AsyncTab-->>Reader: raw Drive item dicts
            end

            Reader->>AsyncTab: _close_quietly(page)
            deactivate AsyncTab

            opt Transient error or timeout
                Reader->>Reader: sleep(_retry_backoff_seconds(attempt))
            end
        end

        Reader-->>Scraper: list of DriveElement or None
        deactivate Reader

        Scraper->>Scraper: _ingest_elements(elements, depth)
        loop For each DriveElement
            Scraper->>Classifiers: classify_element(element)
            Classifiers-->>Scraper: DocumentType

            alt DocumentType is FOLDER
                opt depth + 1 <= max_depth and folder not visited
                    Scraper->>Scraper: _enqueue_folder(folder_id, depth + 1)
                end
            else DocumentType is Supported File
                opt file_id not in seen_file_ids
                    Scraper->>Classifiers: generate_classified_url(label, file_id)
                    Classifiers-->>Scraper: ClassifiedLink
                    Scraper->>Scraper: record file URL and ClassifiedLink
                end
            else DocumentType is UNSUPPORTED_FORMAT
                Scraper->>Scraper: log_unsupported_element(file_id, tabindex)
            end
        end

        Scraper->>Scraper: slots.release()
    end

    Scraper-->>Engine: discovered_file_urls
    deactivate Scraper

    Engine->>BrowserMgr: close shared context and browser
    deactivate BrowserMgr
```

---

## 3. Google Drive REST API v3 Scraping Flow (`_scrape_by_api`)

When the REST API backend is active, folder exploration executes as a concurrent, rate-limited level-order BFS across folders at each depth level.

```mermaid
sequenceDiagram
    autonumber
    participant ApiRunner as _scrape_by_api
    participant Scraper as DriveFolderScraper
    participant Pool as ThreadPoolExecutor
    participant Limiter as RateLimiter
    participant DriveAPI as Google Drive API v3
    participant Classifiers as classifiers

    ApiRunner->>Scraper: reset()
    ApiRunner->>ApiRunner: create requests.Session with HTTPAdapter pool and gzip
    ApiRunner->>Scraper: visited_folder_ids.add(root_folder_id)

    loop While current_level is not empty
        ApiRunner->>Pool: map(_api_list_children, current_level)
        activate Pool

        loop Pagination while nextPageToken exists
            Pool->>Limiter: acquire()
            activate Limiter
            Note over Limiter: Sliding-window check, blocks if quota full
            Limiter-->>Pool: grant request slot
            deactivate Limiter

            Pool->>DriveAPI: GET drive v3 files with parent query and pageSize 1000
            DriveAPI-->>Pool: JSON files list and nextPageToken
        end

        Pool-->>ApiRunner: list of child items per parent folder
        deactivate Pool

        loop For each folder item batch
            ApiRunner->>Scraper: _record_api_items(items, depth)
            activate Scraper
            loop For each file item
                opt Shortcut mimeType
                    Scraper->>Scraper: resolve targetId and targetMimeType
                end
                Scraper->>Classifiers: classify_mime_type(mime_type, name)
                Classifiers-->>Scraper: DocumentType

                alt DocumentType is FOLDER
                    opt target_id not visited and depth + 1 <= max_depth
                        Scraper->>Scraper: append target_id to next_level
                    end
                else Supported File
                    Scraper->>Scraper: _record_file(label, target_id)
                else Unsupported Format
                    Scraper->>Scraper: log_warning for unsupported file
                end
            end
            Scraper-->>ApiRunner: next_level child folder IDs
            deactivate Scraper
        end

        ApiRunner->>ApiRunner: advance current_level = next_level and depth + 1
    end

    ApiRunner->>ApiRunner: session.close()
    ApiRunner-->>ApiRunner: return scraper
```

---

## 4. Playwright Synchronous In-Place Crawl (Page Mode)

When an existing `playwright.sync_api.Page` is passed, the scraper executes sequentially in-place within that page without modifying the browser lifecycle.

```mermaid
sequenceDiagram
    autonumber
    participant Caller
    participant Scraper as DriveFolderScraper
    participant SyncPage as Caller Sync Page

    Caller->>Scraper: scrape()
    activate Scraper
    Scraper->>Scraper: _start_session()

    Scraper->>SyncPage: _wait_for_folder_items_sync(page, timeout)
    Note over Scraper: Catches timeout with warning if initial page is slow

    Scraper->>SyncPage: evaluate(_FILE_ELEMENTS_JS)
    SyncPage-->>Scraper: root elements
    Scraper->>Scraper: _extract_and_queue(current_depth=0)

    loop While queue has items
        Scraper->>Scraper: (folder_url, depth) = queue.popleft()

        loop Retry attempts up to max_retries + 1
            Scraper->>SyncPage: goto(folder_url, wait_until commit)
            alt HTTP status 400 or higher
                Note over Scraper: Inaccessible folder, log error and skip
            else HTTP status OK
                Scraper->>SyncPage: _wait_for_folder_items_sync(page, timeout)
                Scraper->>SyncPage: evaluate(_FILE_ELEMENTS_JS)
                SyncPage-->>Scraper: folder elements
                Scraper->>Scraper: _extract_and_queue(depth)
            end

            opt Retryable failure
                Scraper->>Scraper: time.sleep(_retry_backoff_seconds(attempt))
            end
        end
    end

    Scraper-->>Caller: discovered_file_urls
    deactivate Scraper
```
