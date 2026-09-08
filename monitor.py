import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://sproutandsoil.com"

MAX_PAGES = 5000
MAX_WORKERS = 8
TIMEOUT = 20

DATA_DIR = Path("data")
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"
DASHBOARD_FILE = Path("dashboard.html")


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WebsiteAudit/1.0; "
        "+https://github.com/)"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================
# URL CLEANING
# ============================================================

def clean_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return None

        base_host = urlparse(BASE_URL).netloc.lower()

        if parsed.netloc.lower() != base_host:
            return None

        path = parsed.path or "/"

        # Ignore obvious non-HTML assets
        ignored_extensions = (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".ico",
            ".pdf",
            ".zip",
            ".mp4",
            ".mp3",
            ".webm",
            ".css",
            ".js",
            ".xml",
            ".json",
            ".woff",
            ".woff2",
            ".ttf",
        )

        if path.lower().endswith(ignored_extensions):
            return None

        clean = f"{parsed.scheme}://{parsed.netloc}{path}"

        if path != "/" and clean.endswith("/"):
            clean = clean[:-1]

        return clean

    except Exception:
        return None


# ============================================================
# FETCH PAGE
# ============================================================

def fetch(url):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )

        status = response.status_code

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        final_url = response.url

        # Successful HTML page
        if status == 200 and "text/html" in content_type:
            return {
                "status": "success",
                "html": response.text,
                "http_status": status,
                "content_type": content_type,
                "final_url": final_url,
            }

        # Confirmed removed page
        if status in (404, 410):
            print(
                f"[REMOVED] {status} | {url}",
                flush=True
            )

            return {
                "status": "removed",
                "html": "",
                "http_status": status,
                "content_type": content_type,
                "final_url": final_url,
            }

        # Everything else
        print(
            f"[FAILED] {status} | "
            f"{content_type or 'unknown'} | "
            f"{url}",
            flush=True
        )

        return {
            "status": "failed",
            "html": "",
            "http_status": status,
            "content_type": content_type,
            "final_url": final_url,
        }

    except requests.Timeout:

        print(
            f"[ERROR] TIMEOUT | {url}",
            flush=True
        )

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "content_type": "",
            "error": "Timeout",
        }

    except requests.ConnectionError as error:

        print(
            f"[ERROR] CONNECTION | "
            f"{url} | {error}",
            flush=True
        )

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "content_type": "",
            "error": "ConnectionError",
        }

    except requests.RequestException as error:

        print(
            f"[ERROR] REQUEST | "
            f"{url} | {error}",
            flush=True
        )

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "content_type": "",
            "error": str(error),
        }

    except Exception as error:

        print(
            f"[ERROR] UNKNOWN | "
            f"{url} | {error}",
            flush=True
        )

        return {
            "status": "failed",
            "html": "",
            "http_status": None,
            "content_type": "",
            "error": str(error),
        }


# ============================================================
# EXTRACT SEO + CONTENT
# ============================================================

def extract(url, html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "svg",
        ]
    ):
        tag.decompose()

    # TITLE
    title = ""

    if soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

    # META DESCRIPTION
    description = ""

    description_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^description$",
                re.I
            )
        }
    )

    if description_tag:
        description = description_tag.get(
            "content",
            ""
        ).strip()

    # CANONICAL
    canonical = ""

    canonical_tag = soup.find(
        "link",
        attrs={
            "rel": lambda value:
            value and "canonical" in value
        }
    )

    if canonical_tag:

        href = canonical_tag.get(
            "href",
            ""
        ).strip()

        if href:
            canonical = urljoin(
                url,
                href
            )

    # H1
    h1 = [
        element.get_text(
            " ",
            strip=True
        )
        for element in soup.find_all("h1")
    ]

    # ROBOTS
    robots = ""

    robots_tag = soup.find(
        "meta",
        attrs={
            "name": re.compile(
                "^robots$",
                re.I
            )
        }
    )

    if robots_tag:

        robots = robots_tag.get(
            "content",
            ""
        ).strip()

    # MAIN CONTENT
    main = soup.find("main")

    if main:

        content = main.get_text(
            " ",
            strip=True
        )

    else:

        content = soup.get_text(
            " ",
            strip=True
        )

    content = re.sub(
        r"\s+",
        " ",
        content
    ).strip()

    return {
        "url": url,
        "title": title,
        "description": description,
        "canonical": canonical,
        "h1": h1,
        "robots": robots,
        "content": content,
        "content_length": len(content),
    }


# ============================================================
# SITEMAP DISCOVERY
# ============================================================

def get_sitemap_urls():

    discovered = set()
    sitemap_success = False

    candidates = [
        f"{BASE_URL}/sitemap.xml",
        f"{BASE_URL}/sitemap_index.xml",
    ]

    for sitemap_url in candidates:

        print(
            f"[SITEMAP] Checking {sitemap_url}",
            flush=True
        )

        try:

            response = requests.get(
                sitemap_url,
                headers=HEADERS,
                timeout=TIMEOUT,
            )

            if response.status_code != 200:

                print(
                    f"[SITEMAP] "
                    f"{response.status_code} "
                    f"{sitemap_url}",
                    flush=True
                )

                continue

            sitemap_success = True

            soup = BeautifulSoup(
                response.text,
                "xml"
            )

            locations = soup.find_all(
                "loc"
            )

            for loc in locations:

                value = loc.get_text(
                    strip=True
                )

                # ------------------------------------------------
                # CHILD SITEMAP
                # ------------------------------------------------

                if value.lower().endswith(".xml"):

                    try:

                        child = requests.get(
                            value,
                            headers=HEADERS,
                            timeout=TIMEOUT,
                        )

                        if child.status_code != 200:
                            continue

                        child_soup = BeautifulSoup(
                            child.text,
                            "xml"
                        )

                        for item in child_soup.find_all(
                            "loc"
                        ):

                            page = clean_url(
                                item.get_text(
                                    strip=True
                                )
                            )

                            if page:
                                discovered.add(
                                    page
                                )

                    except requests.RequestException:

                        continue

                # ------------------------------------------------
                # NORMAL URL
                # ------------------------------------------------

                else:

                    page = clean_url(
                        value
                    )

                    if page:
                        discovered.add(
                            page
                        )

            if discovered:

                print(
                    f"[SITEMAP] "
                    f"{len(discovered)} HTML URLs found",
                    flush=True
                )

                return (
                    discovered,
                    sitemap_success
                )

        except requests.RequestException as error:

            print(
                f"[SITEMAP ERROR] "
                f"{error}",
                flush=True
            )

            continue

    print(
        "[SITEMAP] No usable sitemap found",
        flush=True
    )

    return (
        discovered,
        sitemap_success
    )


# ============================================================
# CRAWLER
# ============================================================

def crawl(old_pages):

    start_time = time.time()

    sitemap_urls, sitemap_ok = (
        get_sitemap_urls()
    )

    homepage = clean_url(
        BASE_URL
    )

    if homepage:
        sitemap_urls.add(
            homepage
        )

    # If sitemap fails, safely reuse old URLs
    if not sitemap_ok and old_pages:

        urls = set(
            old_pages.keys()
        )

        print(
            "[WARNING] Sitemap unavailable. "
            "Using previous URLs for safe crawl.",
            flush=True
        )

    else:

        urls = set(
            sitemap_urls
        )

        urls.update(
            old_pages.keys()
        )

    # Final URL filtering
    urls = {
        url
        for url in urls
        if clean_url(url)
    }

    urls = sorted(
        urls
    )[:MAX_PAGES]

    print(
        f"[START] {len(urls)} HTML pages",
        flush=True
    )

    pages = {}
    failed = {}
    removed = set()

    # --------------------------------------------------------
    # PARALLEL CRAWL
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        jobs = {
            executor.submit(
                fetch,
                url
            ): url
            for url in urls
        }

        completed = 0

        for future in as_completed(
            jobs
        ):

            url = jobs[
                future
            ]

            try:

                result = future.result()

                status = result[
                    "status"
                ]

                if status == "success":

                    pages[url] = extract(
                        url,
                        result["html"]
                    )

                elif status == "removed":

                    removed.add(
                        url
                    )

                else:

                    failed[url] = {
                        "http_status":
                            result.get(
                                "http_status"
                            ),
                        "content_type":
                            result.get(
                                "content_type",
                                ""
                            ),
                        "error":
                            result.get(
                                "error",
                                ""
                            ),
                    }

            except Exception as error:

                failed[url] = {
                    "http_status": None,
                    "content_type": "",
                    "error": str(error),
                }

            completed += 1

            if (
                completed % 25 == 0
                or completed == len(urls)
            ):

                print(
                    f"[PROGRESS] "
                    f"{completed}/{len(urls)}",
                    flush=True
                )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    duration = round(
        time.time() - start_time,
        2
    )

    print(
        f"[DONE] "
        f"{len(pages)} pages collected",
        flush=True
    )

    print(
        f"[FAILED] "
        f"{len(failed)} pages",
        flush=True
    )

    print(
        f"[REMOVED] "
        f"{len(removed)} pages",
        flush=True
    )

    print(
        f"[DURATION] "
        f"{duration} seconds",
        flush=True
    )

    # --------------------------------------------------------
    # FAILURE SUMMARY
    # --------------------------------------------------------

    if failed:

        status_counts = {}

        for info in failed.values():

            status = info.get(
                "http_status"
            )

            if status is None:
                status = "ERROR"

            status_counts[status] = (
                status_counts.get(
                    status,
                    0
                ) + 1
            )

        print(
            "[FAILURE SUMMARY]",
            flush=True
        )

        for status, count in sorted(
            status_counts.items(),
            key=lambda x: str(x[0])
        ):

            print(
                f"  {status}: {count}",
                flush=True
            )

    return {
        "pages": pages,
        "failed": failed,
        "removed": removed,
        "duration": duration,
        "sitemap_ok": sitemap_ok,
        "attempted": len(urls),
    }


# ============================================================
# COMPARE
# ============================================================

def compare(
    old,
    new,
    failed,
    removed
):

    changes = []

    old_urls = set(old)
    new_urls = set(new)

    # --------------------------------------------------------
    # NEW PAGES
    # --------------------------------------------------------

    for url in sorted(
        new_urls - old_urls
    ):

        changes.append({
            "type": "new",
            "url": url,
            "field": "Page",
            "details": "New page",
            "priority": "medium",
        })

    # --------------------------------------------------------
    # REMOVED PAGES
    # --------------------------------------------------------

    for url in sorted(
        (old_urls - new_urls) & removed
    ):

        changes.append({
            "type": "removed",
            "url": url,
            "field": "Page",
            "details":
                "Page returned 404/410",
            "priority": "high",
        })

    # --------------------------------------------------------
    # FAILED PAGES
    # --------------------------------------------------------

    for url in sorted(
        failed
    ):

        info = failed[url]

        status = info.get(
            "http_status"
        )

        error = info.get(
            "error",
            ""
        )

        if status:

            details = (
                f"Crawl failed "
                f"(HTTP {status})"
            )

        elif error:

            details = (
                f"Crawl failed: "
                f"{error}"
            )

        else:

            details = (
                "Temporary crawl failure"
            )

        changes.append({
            "type": "failed",
            "url": url,
            "field": "Crawl",
            "details": details,
            "priority": "medium",
        })

    # --------------------------------------------------------
    # SEO FIELDS
    # --------------------------------------------------------

    fields = [
        ("title", "Title", "high"),
        (
            "description",
            "Description",
            "medium"
        ),
        (
            "canonical",
            "Canonical",
            "high"
        ),
        ("h1", "H1", "high"),
        ("robots", "Robots", "high"),
        ("content", "Content", "low"),
    ]

    for url in sorted(
        old_urls & new_urls
    ):

        before = old[url]
        after = new[url]

        for field, label, priority in fields:

            old_value = before.get(
                field,
                ""
            )

            new_value = after.get(
                field,
                ""
            )

            if old_value != new_value:

                changes.append({
                    "type": "changed",
                    "url": url,
                    "field": label,
                    "old": old_value,
                    "new": new_value,
                    "details":
                        f"{label} changed",
                    "priority": priority,
                })

    return changes


# ============================================================
# HISTORY
# ============================================================

def save_history(
    pages,
    changes,
    duration,
    failed_count,
    removed_count
):

    history = []

    if HISTORY_FILE.exists():

        try:

            history = json.loads(
                HISTORY_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(
                history,
                list
            ):
                history = []

        except Exception:

            history = []

    now = datetime.now(
        timezone.utc
    )

    new_count = sum(
        c["type"] == "new"
        for c in changes
    )

    changed_count = sum(
        c["type"] == "changed"
        for c in changes
    )

    actual_removed_count = sum(
        c["type"] == "removed"
        for c in changes
    )

    history.append({

        "date":
            now.strftime(
                "%Y-%m-%d"
            ),

        "checked_at":
            now.isoformat(),

        "pages":
            len(pages),

        "new":
            new_count,

        "changed":
            changed_count,

        "removed":
            actual_removed_count,

        "failed":
            failed_count,

        "duration":
            duration,
    })

    history = history[-365:]

    HISTORY_FILE.write_text(
        json.dumps(
            history,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    return history


# ============================================================
# DASHBOARD
# ============================================================

def make_dashboard(
    pages,
    changes,
    history,
    duration,
    failed_count,
    first_run
):

    new_count = sum(
        c["type"] == "new"
        for c in changes
    )

    changed_count = sum(
        c["type"] == "changed"
        for c in changes
    )

    removed_count = sum(
        c["type"] == "removed"
        for c in changes
    )

    high_priority = sum(
        c.get("priority") == "high"
        for c in changes
    )

    title_changes = sum(
        c["field"] == "Title"
        and c["type"] == "changed"
        for c in changes
    )

    h1_changes = sum(
        c["field"] == "H1"
        and c["type"] == "changed"
        for c in changes
    )

    description_changes = sum(
        c["field"] == "Description"
        and c["type"] == "changed"
        for c in changes
    )

    canonical_changes = sum(
        c["field"] == "Canonical"
        and c["type"] == "changed"
        for c in changes
    )

    robots_changes = sum(
        c["field"] == "Robots"
        and c["type"] == "changed"
        for c in changes
    )

    # --------------------------------------------------------
    # HEALTH
    # --------------------------------------------------------

    if failed_count == 0:

        health_status = "Healthy"

    elif failed_count < 5:

        health_status = "Warning"

    else:

        health_status = "Needs Attention"

    # --------------------------------------------------------
    # CHANGE ROWS
    # --------------------------------------------------------

    rows = []

    for change in changes:

        url = escape(
            str(
                change.get(
                    "url",
                    ""
                )
            )
        )

        change_type = escape(
            str(
                change.get(
                    "type",
                    ""
                )
            ).title()
        )

        field = escape(
            str(
                change.get(
                    "field",
                    ""
                )
            )
        )

        priority = escape(
            str(
                change.get(
                    "priority",
                    ""
                )
            ).title()
        )

        details = escape(
            str(
                change.get(
                    "details",
                    ""
                )
            )
        )

        old_value = escape(
            str(
                change.get(
                    "old",
                    ""
                )
            )
        )

        new_value = escape(
            str(
                change.get(
                    "new",
                    ""
                )
            )
        )

        if change.get(
            "type"
        ) == "changed":

            rows.append(
                f"""
                <details
                    class="change-card"
                    data-type="changed"
                >

                    <summary>

                        <div class="summary-main">

                            <span class="badge">
                                {change_type}
                            </span>

                            <strong>
                                {field}
                            </strong>

                            <span class="url">
                                {url}
                            </span>

                        </div>

                        <span class="priority">
                            {priority}
                        </span>

                    </summary>

                    <div class="diff">

                        <div class="old">

                            <h4>
                                OLD
                            </h4>

                            <div>
                                {old_value}
                            </div>

                        </div>

                        <div class="new">

                            <h4>
                                NEW
                            </h4>

                            <div>
                                {new_value}
                            </div>

                        </div>

                    </div>

                </details>
                """
            )

        else:

            rows.append(
                f"""
                <div
                    class="change-card"
                    data-type="{change.get('type', '')}"
                >

                    <div class="summary-main">

                        <span class="badge">
                            {change_type}
                        </span>

                        <strong>
                            {field}
                        </strong>

                        <span class="url">
                            {url}
                        </span>

                    </div>

                    <span class="priority">
                        {priority}
                    </span>

                    <p>
                        {details}
                    </p>

                </div>
                """
            )

    if not rows:

        rows_html = """
        <div class="empty">
            No changes detected.
        </div>
        """

    else:

        rows_html = "\n".join(
            rows
        )

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------

    history_rows = []

    for item in reversed(
        history
    ):

        history_rows.append(
            f"""
            <tr>

                <td>
                    {escape(
                        str(
                            item.get(
                                "date",
                                ""
                            )
                        )
                    )}
                </td>

                <td>
                    {item.get(
                        "pages",
                        0
                    )}
                </td>

                <td>
                    {item.get(
                        "new",
                        0
                    )}
                </td>

                <td>
                    {item.get(
                        "changed",
                        0
                    )}
                </td>

                <td>
                    {item.get(
                        "removed",
                        0
                    )}
                </td>

                <td>
                    {item.get(
                        "failed",
                        0
                    )}
                </td>

                <td>
                    {item.get(
                        "duration",
                        0
                    )}s
                </td>

            </tr>
            """
        )

    history_html = "\n".join(
        history_rows
    )

    # --------------------------------------------------------
    # FIRST RUN
    # --------------------------------------------------------

    first_run_message = ""

    if first_run:

        first_run_message = """
        <div class="notice">
            First crawl completed.
            This run creates the baseline.
            Future crawls will compare against it.
        </div>
        """

    checked_at = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

    html = f"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width,
               initial-scale=1.0">

<title>
    Sprout & Soil Website Monitor
</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{
    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background: #f5f7fb;

    color: #172033;
}}

.container {{
    max-width: 1400px;

    margin: auto;

    padding: 30px;
}}

header {{
    background: white;

    border-radius: 18px;

    padding: 28px;

    margin-bottom: 22px;

    box-shadow:
        0 4px 18px
        rgba(0,0,0,0.06);
}}

header h1 {{
    margin:
        0 0 8px;

    font-size: 30px;
}}

header p {{
    margin: 0;

    color: #687386;
}}

.notice {{
    background: #fff7df;

    border:
        1px solid #ead28a;

    padding: 15px 18px;

    border-radius: 12px;

    margin-bottom: 20px;
}}

.cards {{
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(160px, 1fr)
        );

    gap: 15px;

    margin-bottom: 22px;
}}

.card {{
    background: white;

    border-radius: 15px;

    padding: 20px;

    box-shadow:
        0 4px 18px
        rgba(0,0,0,0.05);
}}

.card .number {{
    font-size: 30px;

    font-weight: bold;

    margin-bottom: 6px;
}}

.card .label {{
    color: #687386;

    font-size: 14px;
}}

.section {{
    background: white;

    border-radius: 18px;

    padding: 24px;

    margin-bottom: 22px;

    box-shadow:
        0 4px 18px
        rgba(0,0,0,0.05);
}}

.section h2 {{
    margin-top: 0;
}}

.health {{
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(180px, 1fr)
        );

    gap: 15px;
}}

.health-item {{
    padding: 16px;

    border-radius: 12px;

    background: #f7f8fb;
}}

.health-item strong {{
    display: block;

    margin-bottom: 6px;
}}

.health-status {{
    font-weight: bold;
}}

.controls {{
    display: flex;

    gap: 10px;

    flex-wrap: wrap;

    margin-bottom: 18px;
}}

input,
select {{
    padding:
        11px 13px;

    border:
        1px solid #d8dce5;

    border-radius: 10px;

    font-size: 14px;
}}

.change-card {{
    border:
        1px solid #e2e6ee;

    border-radius: 13px;

    padding: 15px;

    margin-bottom: 12px;

    background: white;
}}

.change-card summary {{
    cursor: pointer;

    display: flex;

    justify-content:
        space-between;

    gap: 15px;

    align-items:
        center;
}}

.summary-main {{
    display: flex;

    gap: 10px;

    align-items:
        center;

    flex-wrap: wrap;
}}

.badge {{
    padding:
        5px 9px;

    border-radius: 7px;

    background: #eef1f6;

    font-size: 12px;

    font-weight: bold;
}}

.priority {{
    font-size: 12px;

    font-weight: bold;
}}

.url {{
    overflow-wrap:
        anywhere;
}}

.change-card p {{
    color: #687386;
}}

.diff {{
    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 15px;

    margin-top: 18px;
}}

.old,
.new {{
    padding: 15px;

    border-radius: 10px;

    overflow-wrap:
        anywhere;
}}

.old {{
    background: #fff0f0;
}}

.new {{
    background: #effaf2;
}}

.old h4,
.new h4 {{
    margin-top: 0;
}}

.empty {{
    padding: 30px;

    text-align: center;

    color: #687386;
}}

table {{
    width: 100%;

    border-collapse:
        collapse;
}}

th,
td {{
    text-align: left;

    padding: 12px;

    border-bottom:
        1px solid #edf0f4;
}}

th {{
    font-size: 13px;

    color: #687386;
}}

@media(max-width:700px) {{

    .container {{
        padding: 15px;
    }}

    .diff {{
        grid-template-columns:
            1fr;
    }}

    .summary-main {{
        align-items:
            flex-start;
    }}

}}

</style>

</head>

<body>

<div class="container">


<header>

<h1>
    Sprout & Soil Website Monitor
</h1>

<p>
    Automated SEO and website change monitoring
</p>

<p style="margin-top:8px;">
    Last checked:
    {checked_at}
</p>

</header>


{first_run_message}


<!-- OVERVIEW -->

<div class="cards">


<div class="card">

    <div class="number">
        {len(pages)}
    </div>

    <div class="label">
        Pages
    </div>

</div>


<div class="card">

    <div class="number">
        {new_count}
    </div>

    <div class="label">
        New
    </div>

</div>


<div class="card">

    <div class="number">
        {changed_count}
    </div>

    <div class="label">
        Changed
    </div>

</div>


<div class="card">

    <div class="number">
        {removed_count}
    </div>

    <div class="label">
        Removed
    </div>

</div>


<div class="card">

    <div class="number">
        {high_priority}
    </div>

    <div class="label">
        High Priority
    </div>

</div>

</div>


<!-- SEO -->

<div class="section">

<h2>
    SEO Change Summary
</h2>

<div class="cards">


<div class="card">

    <div class="number">
        {title_changes}
    </div>

    <div class="label">
        Title Changes
    </div>

</div>


<div class="card">

    <div class="number">
        {h1_changes}
    </div>

    <div class="label">
        H1 Changes
    </div>

</div>


<div class="card">

    <div class="number">
        {description_changes}
    </div>

    <div class="label">
        Description Changes
    </div>

</div>


<div class="card">

    <div class="number">
        {canonical_changes}
    </div>

    <div class="label">
        Canonical Changes
    </div>

</div>


<div class="card">

    <div class="number">
        {robots_changes}
    </div>

    <div class="label">
        Robots Changes
    </div>

</div>

</div>

</div>


<!-- HEALTH -->

<div class="section">

<h2>
    Crawl Health
</h2>

<div class="health">


<div class="health-item">

<strong>
    Status
</strong>

<div class="health-status">
    {health_status}
</div>

</div>


<div class="health-item">

<strong>
    Pages Scanned
</strong>

{len(pages)}

</div>


<div class="health-item">

<strong>
    Failed
</strong>

{failed_count}

</div>


<div class="health-item">

<strong>
    Duration
</strong>

{duration}s

</div>


<div class="health-item">

<strong>
    Change Records
</strong>

{len(changes)}

</div>

</div>

</div>


<!-- CHANGES -->

<div class="section">

<h2>
    Current Changes
</h2>


<div class="controls">

<input
    id="search"
    type="text"
    placeholder="Search URL or change..."
    onkeyup="filterChanges()"
>


<select
    id="type"
    onchange="filterChanges()"
>

<option value="all">
    All Types
</option>

<option value="changed">
    Changed
</option>

<option value="new">
    New
</option>

<option value="removed">
    Removed
</option>

<option value="failed">
    Failed
</option>

</select>

</div>


<div id="changes">

{rows_html}

</div>

</div>


<!-- HISTORY -->

<div class="section">

<h2>
    Change History
</h2>


<div style="overflow-x:auto;">

<table>

<thead>

<tr>

<th>
    Date
</th>

<th>
    Pages
</th>

<th>
    New
</th>

<th>
    Changed
</th>

<th>
    Removed
</th>

<th>
    Failed
</th>

<th>
    Duration
</th>

</tr>

</thead>


<tbody>

{history_html}

</tbody>

</table>

</div>

</div>


</div>


<script>

function filterChanges() {{

    const search =
        document
        .getElementById("search")
        .value
        .toLowerCase();

    const type =
        document
        .getElementById("type")
        .value;

    const cards =
        document.querySelectorAll(
            "#changes .change-card"
        );

    cards.forEach(card => {{

        const text =
            card.innerText
            .toLowerCase();

        const cardType =
            card.dataset.type;

        const matchesSearch =
            text.includes(search);

        const matchesType =
            type === "all"
            ||
            cardType === type;

        card.style.display =
            matchesSearch
            && matchesType
            ? ""
            : "none";

    }});

}}

</script>

</body>

</html>
"""

    DASHBOARD_FILE.write_text(
        html,
        encoding="utf-8"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    DATA_DIR.mkdir(
        exist_ok=True
    )

    old_pages = {}

    first_run = (
        not SNAPSHOT_FILE.exists()
    )

    # --------------------------------------------------------
    # LOAD PREVIOUS BASELINE
    # --------------------------------------------------------

    if SNAPSHOT_FILE.exists():

        try:

            data = json.loads(
                SNAPSHOT_FILE.read_text(
                    encoding="utf-8"
                )
            )

            old_pages = data.get(
                "pages",
                {}
            )

        except Exception:

            old_pages = {}

    print(
        f"[BASELINE] "
        f"{len(old_pages)} pages",
        flush=True
    )

    # --------------------------------------------------------
    # CRAWL
    # --------------------------------------------------------

    crawl_result = crawl(
        old_pages
    )

    pages = crawl_result[
        "pages"
    ]

    failed = crawl_result[
        "failed"
    ]

    removed = crawl_result[
        "removed"
    ]

    duration = crawl_result[
        "duration"
    ]

    # --------------------------------------------------------
    # COMPARE
    # --------------------------------------------------------

    changes = compare(
        old_pages,
        pages,
        failed,
        removed
    )

    # --------------------------------------------------------
    # SNAPSHOT
    # --------------------------------------------------------

    snapshot = {

        "site":
            BASE_URL,

        "checked_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "pages":
            pages,

        "changes":
            changes,

        "failed":
            failed,
    }

    SNAPSHOT_FILE.write_text(
        json.dumps(
            snapshot,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # --------------------------------------------------------
    # HISTORY
    # --------------------------------------------------------

    history = save_history(
        pages=pages,
        changes=changes,
        duration=duration,
        failed_count=len(failed),
        removed_count=len(removed),
    )

    # --------------------------------------------------------
    # DASHBOARD
    # --------------------------------------------------------

    make_dashboard(
        pages=pages,
        changes=changes,
        history=history,
        duration=duration,
        failed_count=len(failed),
        first_run=first_run,
    )

    # --------------------------------------------------------
    # FINAL LOG
    # --------------------------------------------------------

    print(
        f"[RESULT] "
        f"{len(changes)} changes",
        flush=True
    )

    print(
        f"[PAGES] "
        f"{len(pages)} pages",
        flush=True
    )

    print(
        f"[FAILED] "
        f"{len(failed)} pages",
        flush=True
    )

    print(
        f"[COMPLETE] "
        f"Duration: {duration}s",
        flush=True
    )


if __name__ == "__main__":
    main()
