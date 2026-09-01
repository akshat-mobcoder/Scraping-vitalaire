import csv
import json
import logging
import os
import time
from collections import deque
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging setup — writes to console AND output/scraper.log
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("vitalaire")
logger.setLevel(logging.DEBUG)

_fmt = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler — INFO and above so the terminal stays readable
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(_fmt)

# File handler — DEBUG and above for full detail
_file = logging.FileHandler(os.path.join(LOG_DIR, "scraper.log"), encoding="utf-8")
_file.setLevel(logging.DEBUG)
_file.setFormatter(_fmt)

logger.addHandler(_console)
logger.addHandler(_file)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://sa.vitalaire.com"
OUTPUT_DIR = LOG_DIR

SEED_URLS = [
    "https://sa.vitalaire.com/",
    "https://sa.vitalaire.com/diabetes",
    "https://sa.vitalaire.com/sleep-apnea",
    "https://sa.vitalaire.com/sleep-apnea/sleep-apnea/do-i-have-sleep-apnea",
    "https://sa.vitalaire.com/sleep-apnea/sleep-apnea/what-treatment-sleep-apnea",
    "https://sa.vitalaire.com/sleep-apnea/sleep-apnea/our-products-and-services",
    "https://sa.vitalaire.com/oxygen-therapy",
    "https://sa.vitalaire.com/ventilation",
    "https://sa.vitalaire.com/home-health-care",
    "https://sa.vitalaire.com/contact-us",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_PAGES = 100
HEADING_TAGS = {"h1", "h2", "h3"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean(tag) -> str:
    return " ".join(tag.get_text(separator=" ").split()) if tag else ""


def normalize(href: str, base: str) -> str | None:
    parsed = urlparse(href)
    if parsed.scheme in ("mailto", "tel", "javascript"):
        return None
    if not href.strip() or href.startswith("#"):
        return None
    full = urljoin(base, href).split("#")[0].rstrip("/")
    if not full.startswith("https://sa.vitalaire.com"):
        return None
    return full

# ---------------------------------------------------------------------------
# Page extraction
# ---------------------------------------------------------------------------

def extract_page(url: str, session: requests.Session) -> dict | None:
    logger.debug("Requesting: %s", url)
    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        logger.debug(
            "Response: %s %s  (%.1f KB)",
            resp.status_code,
            resp.reason,
            len(resp.content) / 1024,
        )
    except requests.HTTPError as exc:
        logger.warning("HTTP error for %s — %s", url, exc)
        return None
    except requests.ConnectionError as exc:
        logger.error("Connection error for %s — %s", url, exc)
        return None
    except requests.Timeout:
        logger.error("Timeout while fetching %s", url)
        return None
    except requests.RequestException as exc:
        logger.error("Unexpected request error for %s — %s", url, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")

    title_tag = soup.find("title")
    meta_tag = soup.find("meta", attrs={"name": "description"})

    # Walk tags in document order, grouping content under the nearest heading
    sections = []
    current_heading = None
    current_level = None
    content_parts: list[str] = []
    pre_heading_parts: list[str] = []

    for tag in soup.find_all(["h1", "h2", "h3", "p", "ul", "ol"]):
        if tag.name in HEADING_TAGS:
            heading_text = clean(tag)
            if not heading_text:
                logger.debug("Empty heading tag <%s> skipped", tag.name)
                continue

            if current_heading is not None:
                sections.append({
                    "heading": current_heading,
                    "heading_level": current_level,
                    "content": "\n\n".join(content_parts),
                })
                logger.debug(
                    "  Section flushed: [%s] %r  (%d content part(s))",
                    current_level,
                    current_heading[:60],
                    len(content_parts),
                )
            else:
                pre_heading_parts = content_parts[:]

            current_heading = heading_text
            current_level = tag.name
            content_parts = []

        elif tag.name == "p":
            text = clean(tag)
            if text:
                content_parts.append(text)

        elif tag.name in ("ul", "ol"):
            items = [f"• {clean(li)}" for li in tag.find_all("li", recursive=False) if clean(li)]
            if not items:
                items = [f"• {clean(li)}" for li in tag.find_all("li") if clean(li)]
            if items:
                content_parts.append("\n".join(items))

    # Flush the last open section
    if current_heading:
        sections.append({
            "heading": current_heading,
            "heading_level": current_level,
            "content": "\n\n".join(content_parts),
        })
        logger.debug(
            "  Section flushed (final): [%s] %r  (%d content part(s))",
            current_level,
            current_heading[:60],
            len(content_parts),
        )

    # Collect internal links
    internal_links = []
    for a in soup.find_all("a", href=True):
        norm = normalize(a["href"], url)
        if norm and norm not in internal_links:
            internal_links.append(norm)

    logger.info(
        "Extracted %d section(s), %d internal link(s) from: %s",
        len(sections),
        len(internal_links),
        url,
    )

    return {
        "url": url,
        "title": clean(title_tag),
        "meta_description": (
            meta_tag["content"].strip() if meta_tag and meta_tag.get("content") else ""
        ),
        "page_summary": "\n\n".join(pre_heading_parts),
        "sections": sections,
        "internal_links": internal_links,
    }

# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

def crawl() -> list[dict]:
    logger.info("=" * 60)
    logger.info("Crawl started  |  base: %s  |  seed pages: %d", BASE_URL, len(SEED_URLS))
    logger.info("=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)

    visited: set[str] = set()
    queue: deque[str] = deque()

    for url in SEED_URLS:
        norm = url.rstrip("/")
        if norm not in visited:
            visited.add(norm)
            queue.append(norm)

    logger.debug("Initial queue: %d URL(s)", len(queue))

    results = []
    counter = 0

    while queue and counter < MAX_PAGES:
        url = queue.popleft()
        counter += 1
        logger.info("[%d/%d] Fetching: %s", counter, min(len(visited), MAX_PAGES), url)

        data = extract_page(url, session)
        if data:
            results.append(data)
            new_links = 0
            for link in data["internal_links"]:
                norm = link.rstrip("/")
                if norm not in visited:
                    visited.add(norm)
                    queue.append(norm)
                    new_links += 1
            if new_links:
                logger.debug("  Queued %d new link(s)  (queue depth: %d)", new_links, len(queue))
        else:
            logger.warning("No data returned for: %s", url)

        time.sleep(1)

    logger.info("=" * 60)
    logger.info(
        "Crawl complete  |  pages fetched: %d  |  pages skipped: %d",
        len(results),
        counter - len(results),
    )
    logger.info("=" * 60)
    return results

# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def save_json(data: list[dict], path: str) -> None:
    logger.info("Writing JSON → %s", path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("JSON saved successfully  (%d page(s))", len(data))
    except OSError as exc:
        logger.error("Failed to write JSON: %s", exc)
        raise


def save_csv(data: list[dict], path: str) -> None:
    logger.info("Writing CSV  → %s", path)
    fieldnames = ["url", "page_title", "heading_level", "heading", "content"]
    total_rows = 0
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for page in data:
                for section in page.get("sections", []):
                    writer.writerow({
                        "url": page["url"],
                        "page_title": page["title"],
                        "heading_level": section["heading_level"],
                        "heading": section["heading"],
                        "content": section["content"],
                    })
                    total_rows += 1
        logger.info("CSV saved successfully  (%d row(s))", total_rows)
    except OSError as exc:
        logger.error("Failed to write CSV: %s", exc)
        raise

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    data = crawl()

    json_path = os.path.join(OUTPUT_DIR, "vitalaire_data_new.json")
    csv_path = os.path.join(OUTPUT_DIR, "vitalaire_data_new.csv")

    save_json(data, json_path)
    save_csv(data, csv_path)

    logger.info("Done. Output files:")
    logger.info("  %s", json_path)
    logger.info("  %s", csv_path)
    logger.info("  %s", os.path.join(OUTPUT_DIR, "scraper.log"))


if __name__ == "__main__":
    main()
