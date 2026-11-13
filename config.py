OUTPUT_DIR = "output"  # relative path
FOLDER_MAX_DEPTH = 5
API_CONCURRENCY = 20  # maximum number of viewer image requests per batch
SCRAPE_TYPE = "d"  # "s": screenshot, "a": viewer API, "d": direct download then screenshot
SCREENSHOT_RESOLUTION = "3840x2160"  # Optional maximum capture size, e.g. "1920x1080"


def screenshot_viewport() -> dict[str, int] | None:
    """Return the configured Playwright viewport, or None for the default."""
    if not SCREENSHOT_RESOLUTION:
        return None

    try:
        width_text, height_text = SCREENSHOT_RESOLUTION.lower().split("x", 1)
        width = int(width_text.strip())
        height = int(height_text.strip())
    except ValueError as error:
        raise ValueError(
            "SCREENSHOT_RESOLUTION must use the WIDTHxHEIGHT format"
        ) from error

    if width <= 0 or height <= 0:
        raise ValueError("SCREENSHOT_RESOLUTION dimensions must be greater than zero")

    return {"width": width, "height": height}


def screenshot_width() -> int | None:
    viewport = screenshot_viewport()
    return viewport["width"] if viewport else None