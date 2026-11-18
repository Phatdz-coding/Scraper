import os


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

OUTPUT_DIR = "output"  # relative path
CONCURRENT_SCRAPER = 5  # maximum number of concurrent document scrapers sharing one browser
FOLDER_MAX_DEPTH = 2
API_CONCURRENCY = 20  # maximum number of viewer image requests per batch
SCRAPE_TYPE = "d"  # "s": screenshot, "d": try to download directly
MAX_CONCURENT_FOLDER_EXPLORER=5
MAX_CONCURENT_FOLDER_NODE_VISIT=10 # determine the maximum number of folder to be visited and scraped in DriveFolderScraper.queue
SCRAPE_TIMEOUT_SECONDS = 10
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