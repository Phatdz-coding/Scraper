from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import sys
from typing import Any


def _load_dotenv(env_path: str | None = None) -> None:
    """Load variables from a .env file into os.environ without overriding existing env vars."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            else:
                comment_idx = value.find(" #")
                if comment_idx != -1:
                    value = value[:comment_idx].strip()
            os.environ.setdefault(key, value)


_load_dotenv()

# ---------------------------------------------------------------------------
# General Scraping Settings
# ---------------------------------------------------------------------------
OUTPUT_DIR = "output"  # relative path
USER_DATA_DIR = ".browser_profile"  # persistent Chromium user data directory
CONCURRENT_SCRAPER = 5  # maximum number of concurrent document scrapers sharing one browser
MAX_FOLDER_DEPTH = 2
MAX_CONCURRENT_IMG_API = 20  # maximum number of viewer image requests per batch
SCRAPE_TYPE = "d"  # "s": screenshot, "d": try to download directly
VALID_SCRAPE_TYPES: frozenset[str] = frozenset({"d", "s"})
MAX_CONCURENT_FOLDER_NODE_VISIT=10 # determine the maximum number of folder to be visited and scraped in DriveFolderScraper.queue
FOLDER_TIMEOUT = 10 # max time to wait when open a folder. Should be >= MAX_CONCURENT_FOLDER_NODE_VISIT, measured in second
SCRAPE_RETRY_COUNT = 2

# Google Drive API (folder scraping). Values come from process environment
# variables or the project's .env file.
# The folder scraper uses the Drive API only when USE_GOOGLE_DRIVE_API is true
# AND GOOGLE_API_KEY is non-empty; otherwise it falls back to Playwright.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
USE_GOOGLE_DRIVE_API = os.environ.get("USE_GOOGLE_DRIVE_API", "false").strip().lower() in {"1", "true", "yes", "on"}
# Client-side rate limit: at most N requests per sliding window of P seconds.
GOOGLE_DRIVE_API_RATE_LIMIT = 12000
GOOGLE_DRIVE_API_RATE_PERIOD_SECONDS = 60

# ---------------------------------------------------------------------------
# Browser & Anti-Bot Stealth Settings
# ---------------------------------------------------------------------------
BROWSER_HEADLESS: bool = True
BROWSER_CHANNEL: str | None = "chromium"
BROWSER_ACCEPT_DOWNLOADS: bool = True
BROWSER_LOCALE: str = "en-US"
BROWSER_VIEWPORT: dict[str, int] = {"width": 1920, "height": 1080}
BROWSER_WAIT_UNTIL: str = "domcontentloaded"
VALID_WAIT_UNTIL_STATES: frozenset[str] = frozenset(
    {"load", "domcontentloaded", "networkidle", "commit"}
)

ANTI_BOT_CHROMIUM_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--headless=new",
)

ANTI_BOT_IGNORE_DEFAULT_ARGS: tuple[str, ...] = ("--enable-automation",)

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT: str = """
(() => {
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });

    if (!window.chrome) {
        Object.defineProperty(window, 'chrome', {
            value: {
                runtime: {},
                loadTimes: () => ({}),
                csi: () => ({}),
                app: {}
            },
            configurable: true
        });
    }

    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
        configurable: true
    });

    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' }
        ],
        configurable: true
    });

    if (navigator.permissions && navigator.permissions.query) {
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
})();
"""


# ---------------------------------------------------------------------------
# Configuration Helpers & Immutable Settings Models
# ---------------------------------------------------------------------------
def normalize_scrape_type(scrape_type: str) -> str:
    """Normalize and validate a scrape type ('d' or 's')."""
    normalized = str(scrape_type).strip().lower()
    if normalized not in VALID_SCRAPE_TYPES:
        raise ValueError(
            f"Invalid scrape_type {scrape_type!r}. Must be 'd' or 's'."
        )
    return normalized


@dataclass(frozen=True)
class BrowserConfig:
    """Immutable Playwright Chromium launch and anti-bot stealth configuration."""

    user_data_dir: Path = field(default_factory=lambda: Path(USER_DATA_DIR))
    headless: bool = field(default_factory=lambda: bool(BROWSER_HEADLESS))
    channel: str | None = field(default_factory=lambda: BROWSER_CHANNEL)
    accept_downloads: bool = field(
        default_factory=lambda: bool(BROWSER_ACCEPT_DOWNLOADS)
    )
    args: tuple[str, ...] = field(default_factory=lambda: tuple(ANTI_BOT_CHROMIUM_ARGS))
    ignore_default_args: tuple[str, ...] = field(
        default_factory=lambda: tuple(ANTI_BOT_IGNORE_DEFAULT_ARGS)
    )
    user_agent: str = field(default_factory=lambda: str(DEFAULT_USER_AGENT))
    viewport_width: int = field(
        default_factory=lambda: int(BROWSER_VIEWPORT["width"])
    )
    viewport_height: int = field(
        default_factory=lambda: int(BROWSER_VIEWPORT["height"])
    )
    locale: str = field(default_factory=lambda: str(BROWSER_LOCALE))
    stealth_init_script: str = field(default_factory=lambda: str(STEALTH_INIT_SCRIPT))
    wait_until: str = field(default_factory=lambda: str(BROWSER_WAIT_UNTIL))

    def __post_init__(self) -> None:
        if isinstance(self.user_data_dir, str):
            if not self.user_data_dir.strip():
                raise ValueError("user_data_dir must be a non-empty path")
            object.__setattr__(self, "user_data_dir", Path(self.user_data_dir))
        elif not isinstance(self.user_data_dir, Path):
            object.__setattr__(self, "user_data_dir", Path(self.user_data_dir))

        object.__setattr__(self, "headless", bool(self.headless))
        if self.channel is not None:
            channel = str(self.channel).strip()
            if not channel:
                raise ValueError("channel must be a non-empty string or None")
            object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "accept_downloads", bool(self.accept_downloads))

        if isinstance(self.args, str):
            raise TypeError("args must be a sequence of strings, not str")
        object.__setattr__(self, "args", tuple(str(arg) for arg in self.args))

        if isinstance(self.ignore_default_args, str):
            raise TypeError(
                "ignore_default_args must be a sequence of strings, not str"
            )
        object.__setattr__(
            self,
            "ignore_default_args",
            tuple(str(arg) for arg in self.ignore_default_args),
        )

        if isinstance(self.viewport_width, bool) or isinstance(
            self.viewport_height, bool
        ):
            raise ValueError("viewport_width and viewport_height must be integers")
        width = int(self.viewport_width)
        height = int(self.viewport_height)
        if width < 1 or height < 1:
            raise ValueError("viewport_width and viewport_height must be at least 1")
        object.__setattr__(self, "viewport_width", width)
        object.__setattr__(self, "viewport_height", height)

        user_agent = str(self.user_agent).strip()
        if not user_agent:
            raise ValueError("user_agent must be a non-empty string")
        object.__setattr__(self, "user_agent", user_agent)

        locale = str(self.locale).strip()
        if not locale:
            raise ValueError("locale must be a non-empty string")
        object.__setattr__(self, "locale", locale)

        wait_until = str(self.wait_until).strip()
        if wait_until not in VALID_WAIT_UNTIL_STATES:
            raise ValueError(
                f"Invalid wait_until {self.wait_until!r}. "
                f"Must be one of {sorted(VALID_WAIT_UNTIL_STATES)}."
            )
        object.__setattr__(self, "wait_until", wait_until)
        object.__setattr__(self, "stealth_init_script", str(self.stealth_init_script))

    @property
    def viewport(self) -> dict[str, int]:
        """Return a fresh viewport dimensions dict for Playwright."""
        return {"width": self.viewport_width, "height": self.viewport_height}

    def with_overrides(
        self,
        *,
        headless: bool | None = None,
        channel: str | None = None,
        user_data_dir: Path | str | None = None,
        accept_downloads: bool | None = None,
    ) -> BrowserConfig:
        """Return a copy of this BrowserConfig with any non-None overrides applied."""
        changes: dict[str, Any] = {}
        if headless is not None:
            changes["headless"] = bool(headless)
        if channel is not None:
            changes["channel"] = channel
        if user_data_dir is not None:
            if isinstance(user_data_dir, str) and not user_data_dir.strip():
                raise ValueError("user_data_dir must be a non-empty path")
            changes["user_data_dir"] = Path(user_data_dir)
        if accept_downloads is not None:
            changes["accept_downloads"] = bool(accept_downloads)
        if not changes:
            return self
        return replace(self, **changes)

    def to_persistent_context_kwargs(self) -> dict[str, Any]:
        """Build keyword arguments for Playwright's launch_persistent_context()."""
        if self.headless:
            args = list(self.args)
            if not any(
                arg == "--headless" or arg.startswith("--headless=") for arg in args
            ):
                args.append("--headless=new")
        else:
            args = [
                arg
                for arg in self.args
                if arg != "--headless" and not arg.startswith("--headless=")
            ]

        kwargs: dict[str, Any] = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.headless,
            "accept_downloads": self.accept_downloads,
            "args": args,
            "ignore_default_args": list(self.ignore_default_args),
            "user_agent": self.user_agent,
            "viewport": self.viewport,
            "locale": self.locale,
        }
        if self.channel is not None:
            kwargs["channel"] = self.channel
        return kwargs

    @classmethod
    def from_config(
        cls,
        *,
        headless: bool | None = None,
        channel: str | None = None,
        user_data_dir: Path | str | None = None,
        accept_downloads: bool | None = None,
    ) -> BrowserConfig:
        """Construct a BrowserConfig from module defaults with optional overrides."""
        return cls().with_overrides(
            headless=headless,
            channel=channel,
            user_data_dir=user_data_dir,
            accept_downloads=accept_downloads,
        )


@dataclass(frozen=True, init=False)
class BatchConfig:
    """Immutable configuration for a batch scraping run."""

    output_dir: Path
    concurrency: int
    scrape_type: str
    headless: bool
    browser: BrowserConfig

    def __init__(
        self,
        output_dir: Path | str | None = None,
        concurrency: int | None = None,
        scrape_type: str | None = None,
        headless: bool | None = None,
        browser: BrowserConfig | None = None,
    ) -> None:
        raw_output = OUTPUT_DIR if output_dir is None else output_dir
        if isinstance(raw_output, str) and not raw_output.strip():
            raise ValueError("output_dir must be a non-empty path")
        resolved_output = Path(raw_output)

        raw_concurrency = CONCURRENT_SCRAPER if concurrency is None else concurrency
        if isinstance(raw_concurrency, bool) or int(raw_concurrency) < 1:
            raise ValueError("concurrency must be at least 1")
        resolved_concurrency = int(raw_concurrency)

        resolved_scrape_type = normalize_scrape_type(
            SCRAPE_TYPE if scrape_type is None else scrape_type
        )

        resolved_browser = (
            browser if browser is not None else BrowserConfig.from_config()
        )
        resolved_headless = (
            resolved_browser.headless if headless is None else bool(headless)
        )
        if resolved_browser.headless != resolved_headless:
            resolved_browser = resolved_browser.with_overrides(
                headless=resolved_headless
            )

        object.__setattr__(self, "output_dir", resolved_output)
        object.__setattr__(self, "concurrency", resolved_concurrency)
        object.__setattr__(self, "scrape_type", resolved_scrape_type)
        object.__setattr__(self, "headless", resolved_headless)
        object.__setattr__(self, "browser", resolved_browser)

    @classmethod
    def from_config(
        cls,
        *,
        output_dir: Path | str | None = None,
        concurrency: int | None = None,
        scrape_type: str | None = None,
        headless: bool | None = None,
        browser: BrowserConfig | None = None,
    ) -> BatchConfig:
        """Construct a BatchConfig with optional overrides on top of config.py defaults."""
        return cls(
            output_dir=output_dir,
            concurrency=concurrency,
            scrape_type=scrape_type,
            headless=headless,
            browser=browser,
        )


def _validate_optional_int(
    value: int | None,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    """Validate an optional integer override against inclusive bounds."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, not bool")
    resolved = int(value)
    if resolved < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {resolved}")
    if maximum is not None and resolved > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {resolved}")
    return resolved


@dataclass(frozen=True)
class RuntimeConfigOverrides:
    """Immutable bundle of CLI/runtime overrides applied to module-level config settings."""

    headless_disable: bool = False
    use_google_drive_api: bool = False
    google_api_key: str | None = None
    concurrent_folder: int | None = None
    folder_timeout: int | None = None
    max_folder_depth: int | None = None
    max_retry: int | None = None
    max_concurrent_img_api: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headless_disable", bool(self.headless_disable))
        object.__setattr__(
            self, "use_google_drive_api", bool(self.use_google_drive_api)
        )

        normalized_key: str | None = None
        if self.google_api_key is not None:
            stripped = str(self.google_api_key).strip()
            normalized_key = stripped or None
        object.__setattr__(self, "google_api_key", normalized_key)

        object.__setattr__(
            self,
            "concurrent_folder",
            _validate_optional_int(
                self.concurrent_folder, "concurrent_folder", minimum=1
            ),
        )
        object.__setattr__(
            self,
            "folder_timeout",
            _validate_optional_int(self.folder_timeout, "folder_timeout", minimum=1),
        )
        object.__setattr__(
            self,
            "max_folder_depth",
            _validate_optional_int(
                self.max_folder_depth, "max_folder_depth", minimum=1, maximum=100
            ),
        )
        object.__setattr__(
            self,
            "max_retry",
            _validate_optional_int(self.max_retry, "max_retry", minimum=0, maximum=10),
        )
        object.__setattr__(
            self,
            "max_concurrent_img_api",
            _validate_optional_int(
                self.max_concurrent_img_api,
                "max_concurrent_img_api",
                minimum=1,
                maximum=30,
            ),
        )

    def to_config_updates(self) -> dict[str, Any]:
        """Return the mapping of config module attribute names to overridden values."""
        updates: dict[str, Any] = {}
        if self.headless_disable:
            updates["BROWSER_HEADLESS"] = False
        if self.use_google_drive_api:
            updates["USE_GOOGLE_DRIVE_API"] = True
            if self.google_api_key is not None:
                updates["GOOGLE_API_KEY"] = self.google_api_key
        if self.concurrent_folder is not None:
            updates["MAX_CONCURENT_FOLDER_NODE_VISIT"] = self.concurrent_folder
        if self.folder_timeout is not None:
            updates["FOLDER_TIMEOUT"] = self.folder_timeout
        if self.max_folder_depth is not None:
            updates["MAX_FOLDER_DEPTH"] = self.max_folder_depth
        if self.max_retry is not None:
            updates["SCRAPE_RETRY_COUNT"] = self.max_retry
        if self.max_concurrent_img_api is not None:
            updates["MAX_CONCURRENT_IMG_API"] = self.max_concurrent_img_api
        return updates

    def apply(self, target: Any | None = None) -> None:
        """Apply all active overrides to ``target`` (defaults to this config module)."""
        resolved_target = sys.modules[__name__] if target is None else target
        for attr, value in self.to_config_updates().items():
            setattr(resolved_target, attr, value)

    @contextmanager
    def scoped(self, target: Any | None = None) -> Iterator[None]:
        """Temporarily apply overrides to ``target`` and restore original values on exit."""
        resolved_target = sys.modules[__name__] if target is None else target
        updates = self.to_config_updates()
        previous = {attr: getattr(resolved_target, attr) for attr in updates}
        for attr, value in updates.items():
            setattr(resolved_target, attr, value)
        try:
            yield
        finally:
            for attr, old_value in previous.items():
                setattr(resolved_target, attr, old_value)


# ---------------------------------------------------------------------------
# Google Drive DOM Icon Vectors & Elements
# ---------------------------------------------------------------------------

folder_icon_vector="""M20,6h-8l-2-2H4C2.9,4,2.01,4.9,2.01,6L2,18c0,1.1,0.9,2,2,2h16c1.1,0,2-0.9,2-2V8C22,6.9,21.1,6,20,6z M15,9 c1.1,0,2,0.9,2,2c0,1.1-0.9,2-2,2s-2-0.9-2-2C13,9.9,13.9,9,15,9z M19,17h-8v-0.57c0-0.81,0.48-1.53,1.22-1.85 C13.07,14.21,14.01,14,15,14c0.99,0,1.93,0.21,2.78,0.58C18.52,14.9,19,15.62,19,16.43V17z"""
folder_icon_vector2="""M4 20C3.45 20 2.97917 19.8042 2.5875 19.4125C2.19583 19.0208 2 18.55 2 18V6C2 5.45 2.19583 4.97917 2.5875 4.5875C2.97917 4.19583 3.45 4 4 4H10L12 6H20C20.55 6 21.0208 6.19583 21.4125 6.5875C21.8042 6.97917 22 7.45 22 8V18C22 18.55 21.8042 19.0208 21.4125 19.4125C21.0208 19.8042 20.55 20 20 20H4ZM12 17C13.1 17 14.0417 16.6083 14.825 15.825C15.6083 15.0417 16 14.1 16 13C16 11.9 15.6083 10.9583 14.825 10.175C14.0417 9.39167 13.1 9 12 9C10.9 9 9.95833 9.39167 9.175 10.175C8.39167 10.9583 8 11.9 8 13C8 14.1 8.39167 15.0417 9.175 15.825C9.95833 16.6083 10.9 17 12 17ZM10.85 10.8C11.0333 10.7167 11.2167 10.6458 11.4 10.5875C11.5833 10.5292 11.7833 10.5 12 10.5C12.7 10.5 13.2917 10.7417 13.775 11.225C14.2583 11.7083 14.5 12.3 14.5 13C14.5 13.2167 14.4708 13.4167 14.4125 13.6C14.3542 13.7833 14.2833 13.9667 14.2 14.15L10.85 10.8ZM12 15.5C11.3 15.5 10.7083 15.2583 10.225 14.775C9.74167 14.2917 9.5 13.7 9.5 13C9.5 12.7833 9.52917 12.5833 9.5875 12.4C9.64583 12.2167 9.71667 12.0333 9.8 11.85L13.15 15.2C12.9667 15.2833 12.7833 15.3542 12.6 15.4125C12.4167 15.4708 12.2167 15.5 12 15.5Z"""
folder_icon_vector3="""M10,4H4C2.9,4,2.01,4.9,2.01,6L2,18c0,1.1,0.9,2,2,2h16c1.1,0,2-0.9,2-2V8c0-1.1-0.9-2-2-2h-8L10,4z"""
pdf_icon_vector = """M5.81045 15.0882C5.59205 15.0882 5.40305 15.0084 5.24345 14.8488C5.08665 14.6892 5.00825 14.4974 5.00825 14.2734V9.8844C5.00825 9.638 5.09085 9.4266 5.25605 9.2502C5.42405 9.0738 5.62285 8.9856 5.85245 8.9856H7.29725C7.91045 8.9856 8.39765 9.1634 8.75885 9.519C9.12285 9.8746 9.30485 10.3898 9.30485 11.0646V11.0898C9.30485 11.7814 9.11305 12.3162 8.72945 12.6942C8.34865 13.0722 7.85165 13.2612 7.23845 13.2612H5.91965V11.955H7.04105C7.24265 11.955 7.39945 11.8948 7.51145 11.7744C7.62625 11.6512 7.68365 11.4524 7.68365 11.178V11.115C7.68365 10.8434 7.62485 10.653 7.50725 10.5438C7.39245 10.4346 7.23565 10.38 7.03685 10.38H6.61685V14.2734C6.61685 14.4974 6.53705 14.6892 6.37745 14.8488C6.22065 15.0084 6.03165 15.0882 5.81045 15.0882ZM11.1055 15C10.8647 15 10.6575 14.9132 10.4839 14.7396C10.3103 14.566 10.2235 14.3588 10.2235 14.118V9.8676C10.2235 9.6268 10.3103 9.4196 10.4839 9.246C10.6575 9.0724 10.8647 8.9856 11.1055 8.9856H12.3025C13.1257 8.9856 13.7613 9.2474 14.2093 9.771C14.6601 10.2946 14.8855 11.0226 14.8855 11.955V12.0222C14.8855 12.9238 14.6503 13.6462 14.1799 14.1894C13.7095 14.7298 13.0837 15 12.3025 15H11.1055ZM11.8321 13.551H12.2521C12.5909 13.551 12.8415 13.4292 13.0039 13.1856C13.1691 12.942 13.2517 12.5766 13.2517 12.0894V11.892C13.2517 11.4272 13.1677 11.0702 12.9997 10.821C12.8317 10.5718 12.5839 10.4472 12.2563 10.4472H11.8321V13.551ZM16.6881 15.0882C16.4669 15.0882 16.2765 15.0084 16.1169 14.8488C15.9601 14.6892 15.8817 14.4974 15.8817 14.2734V9.855C15.8817 9.617 15.9657 9.4126 16.1337 9.2418C16.3045 9.071 16.5075 8.9856 16.7427 8.9856H18.6915C18.8931 8.9856 19.0653 9.057 19.2081 9.1998C19.3537 9.3426 19.4265 9.5134 19.4265 9.7122C19.4265 9.911 19.3537 10.0818 19.2081 10.2246C19.0653 10.3674 18.8931 10.4388 18.6915 10.4388H17.4903V14.2734C17.4903 14.4974 17.4105 14.6892 17.2509 14.8488C17.0941 15.0084 16.9065 15.0882 16.6881 15.0882ZM16.8309 12.732V11.4216H18.4101C18.5921 11.4216 18.7475 11.486 18.8763 11.6148C19.0079 11.7436 19.0737 11.8976 19.0737 12.0768C19.0737 12.256 19.0079 12.41 18.8763 12.5388C18.7475 12.6676 18.5921 12.732 18.4101 12.732H16.8309Z"""
google_doc_icon_element="""<g><rect x="3" y="3" width="18" height="18" rx="3" fill="#3186FF"></rect><rect x="7" y="15" width="6" height="2" rx="1" fill="white"></rect><rect x="7" y="11" width="10" height="2" rx="1" fill="white"></rect></g>"""
microsoft_word_icon_vector="""m6.507 10.483-.832 3.862H4.68l-.491-2.317-.515 2.317H2.671l-.799-3.862h.82l.494 2.549.49-2.55H4.68l.513 2.55.48-2.55z"""
png_icon_vector="""M7.71462 14.4812C8.10668 13.9771 8.86431 13.9646 9.27272 14.4556L10.9778 16.505L13.6741 13.0345C14.0787 12.5136 14.868 12.5203 15.2637 13.048L18.3778 17.2C18.625 17.5296 18.3898 18 17.9778 18H6.00007C5.584 18 5.34995 17.5215 5.6054 17.193L7.71462 14.4812Z"""
xlsx_icon_element="""<g><rect x="21" y="21" width="18" height="18" rx="3" transform="rotate(-180 21 21)" fill="#00AF57"></rect><rect x="18" y="10" width="13" height="2" rx="1" transform="rotate(-180 18 10)" fill="white"></rect><rect x="8" y="18" width="13" height="2" rx="1" transform="rotate(-90 8 18)" fill="white"></rect></g>"""
pptx_icon_element="""<g><path d="M.552 2.681a1.655 1.655 0 0 1 1.655-1.655H12.69A3.31 3.31 0 0 1 16 4.336v7.173a1.655 1.655 0 0 1-1.655 1.655H3.862a3.31 3.31 0 0 1-3.31-3.31z" fill="url(#ucc-4-a)"></path><use href="#ucc-4-p1" fill="url(#ucc-4-b)"></use><use href="#ucc-4-p1" fill="url(#ucc-4-c)"></use><use href="#ucc-4-p2" fill="url(#ucc-4-d)"></use><use href="#ucc-4-p2" fill="url(#ucc-4-e)"></use><use fill-opacity=".5" href="#ucc-4-p2" fill="url(#ucc-4-f)"></use><use href="#ucc-4-p3" fill="url(#ucc-4-g)"></use><use href="#ucc-4-p3" fill="url(#ucc-4-h)"></use><use href="#ucc-4-p4" fill="url(#ucc-4-i)"></use><use fill-opacity=".3" href="#ucc-4-p4" fill="url(#ucc-4-j)"></use><path fill="#fff" d="M3.32 12.458v1.258h-.87V9.853h1.344q.722 0 1.1.316.38.314.38.934 0 .638-.427.997-.423.357-1.136.358zm0-1.937v1.269h.364q.324 0 .499-.17t.174-.487q0-.294-.172-.452-.17-.16-.487-.16H3.32"></path><defs><path d="M1.103 2.681a1.103 1.103 0 0 1 1.104-1.103H12.69a2.76 2.76 0 0 1 2.758 2.758v7.173a1.104 1.104 0 0 1-1.103 1.103H3.862a2.76 2.76 0 0 1-2.759-2.759z" id="ucc-4-p1"></path><path d="M8.276 3.233a3.862 3.862 0 1 1-.552 7.685V9.853a2.207 2.207 0 0 0-2.207-2.206H4.454a3.862 3.862 0 0 1 3.822-4.414" id="ucc-4-p2"></path><path d="M8.276 3.233a3.862 3.862 0 0 1 3.338 5.802l.062-.134a1.282 1.282 0 0 0-1.169-1.809H8.964a.69.69 0 0 1-.69-.692V4.865c0-.932-.96-1.553-1.81-1.17l-.114.053a3.84 3.84 0 0 1 1.926-.515" id="ucc-4-p3"></path><rect y="8.198" width="7.172" height="7.172" rx="1.655" id="ucc-4-p4"></rect><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(7.57089 8.73564 -11.3563 3.27587 5.686 4.451)" gradientUnits="userSpaceOnUse" id="ucc-4-c"><stop offset=".177" stop-color="#fff"></stop><stop offset="1" stop-color="#efefef"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(-5.60029 5.48347 -9.42601 -9.22938 9.753 5.474)" gradientUnits="userSpaceOnUse" id="ucc-4-e"><stop offset=".152" stop-color="#aa1d2d"></stop><stop offset=".381" stop-color="#d12b18" stop-opacity=".44"></stop><stop offset=".602" stop-color="#ff3c00" stop-opacity="0"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(6.78855 -3.13962 5.80907 12.042 2.872 10.235)" gradientUnits="userSpaceOnUse" id="ucc-4-f"><stop offset=".407" stop-color="#ff66fb"></stop><stop offset="1" stop-color="#ea3d01" stop-opacity="0"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(3.52665 -4.03888 3.76397 3.13421 7.85 7.622)" gradientUnits="userSpaceOnUse" id="ucc-4-g"><stop stop-color="#fd6ef9"></stop><stop offset=".637" stop-color="#f94"></stop><stop offset=".852" stop-color="#fcc479"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(-.39836 2.70021 -6.10667 -.85913 7.5 3.743)" gradientUnits="userSpaceOnUse" id="ucc-4-h"><stop offset=".144" stop-color="#ff8d13"></stop><stop offset=".537" stop-color="#ff7f29" stop-opacity="0"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="rotate(45 -9.896 4.1)scale(10.1434)" gradientUnits="userSpaceOnUse" id="ucc-4-i"><stop stop-color="#f8193e"></stop><stop offset=".939" stop-color="#920616"></stop></radialGradient><radialGradient cx="0" cy="0" r="1" gradientTransform="matrix(0 5.0207 -5.71555 0 3.585 12.502)" gradientUnits="userSpaceOnUse" id="ucc-4-j"><stop offset=".576" stop-color="#ffb055" stop-opacity="0"></stop><stop offset=".974" stop-color="#fff2be"></stop></radialGradient><linearGradient x1="5.54" x2="6.833" y1="1.026" y2="13.366" gradientUnits="userSpaceOnUse" id="ucc-4-a"><stop stop-color="#bbb"></stop><stop offset="1" stop-color="#888"></stop></linearGradient><linearGradient x1="5.088" x2="6.169" y1="1.578" y2="12.815" gradientUnits="userSpaceOnUse" id="ucc-4-b"><stop stop-color="#fff"></stop><stop offset="1" stop-color="#ebebeb"></stop></linearGradient><linearGradient x1="7.794" x2="3.058" y1="2.987" y2="7.613" gradientUnits="userSpaceOnUse" id="ucc-4-d"><stop offset=".058" stop-color="#ff7f48"></stop><stop offset="1" stop-color="#e5495b"></stop></linearGradient></defs></g>"""
zip_folder_icon_element="""<g><rect x="3" y="3" width="18" height="18" rx="3" fill="#5F6368"></rect><path d="M9 7C9 6.44772 9.44772 6 10 6H12V8H10C9.44772 8 9 7.55228 9 7Z" fill="white"></path><path d="M9 11C9 10.4477 9.44772 10 10 10H12V12H10C9.44772 12 9 11.5523 9 11Z" fill="white"></path><path d="M9 15C9 14.4477 9.44772 14 10 14H12V16H10C9.44772 16 9 15.5523 9 15Z" fill="white"></path><path d="M12 8H14C14.5523 8 15 8.44772 15 9C15 9.55228 14.5523 10 14 10H12V8Z" fill="white"></path><path d="M12 12H14C14.5523 12 15 12.4477 15 13C15 13.5523 14.5523 14 14 14H12V12Z" fill="white"></path><path d="M12 16H14C14.5523 16 15 16.4477 15 17C15 17.5523 14.5523 18 14 18H12V16Z" fill="white"></path></g>"""