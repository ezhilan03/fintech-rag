# src/ingestion/fetcher.py
"""
Downloads and saves source documents from public authoritative sources.
Saves raw text locally so we can re-ingest without hitting the web every time.
Re-run this file whenever you want to refresh source documents.
"""

import httpx
import time
from pathlib import Path
from bs4 import BeautifulSoup

# Where we save raw fetched documents
RAW_DIR = Path("data/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)

# Sources — each has a name used as the filename and the URL to fetch
SOURCES = [
    {
        "name": "plaid_return_codes",
        "url": "https://plaid.com/resources/ach/ach-return/",
        "description": "Plaid ACH guide — correct timing, common codes explained",
    },
    {
        "name": "achq_developer_docs",
        "url": "https://developers.achq.com/docs/return-codes",
        "description": "ACHQ developer reference — structured table format",
    },
    {
        "name": "ramp_return_codes",
        "url": "https://ramp.com/blog/ach-return-codes",
        "description": "Ramp complete R01-R85 guide — updated July 2026",
    },
    {
        "name": "achforbusiness_full_list",
        "url": "https://achforbusiness.com/what-are-ach-return-codes/",
        "description": "Complete R01-R85 table including ENR and dishonored codes",
    },
]


def fetch_and_clean(url: str) -> str:
    """
    Fetches a URL and strips HTML down to clean readable text.

    Why httpx over requests?
    httpx is the modern Python HTTP client — supports async natively,
    which we'll need when we parallelize ingestion later.
    We're using it synchronously here to keep things simple for now.

    Why BeautifulSoup?
    Raw HTML is full of nav bars, footers, ads, script tags.
    BeautifulSoup extracts just the meaningful text content.
    """
    headers = {
        # Identify ourselves as a browser — some sites block default httpx UA
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    response = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()  # raises exception if 4xx or 5xx

    soup = BeautifulSoup(response.text, "html.parser")

    # Remove noise — scripts, styles, nav, footer
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # get_text() extracts all remaining text
    # separator="\n" puts each element on its own line
    # strip=True removes leading/trailing whitespace from each piece
    text = soup.get_text(separator="\n", strip=True)

    # Collapse multiple blank lines into one — keeps the text clean
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text


def fetch_all_sources(force_refresh: bool = False) -> dict[str, Path]:
    """
    Downloads all sources and saves them to data/raw/.
    Skips already-downloaded files unless force_refresh=True.

    Returns a dict of {source_name: file_path} for the chunker to consume.

    Why save to disk?
    - Don't hit the web on every run during development
    - Reproducible — your chunks come from the same text every time
    - Auditable — you can inspect exactly what was ingested
    """
    saved = {}

    for source in SOURCES:
        out_path = RAW_DIR / f"{source['name']}.txt"

        if out_path.exists() and not force_refresh:
            print(f"  ✓ {source['name']} already exists, skipping")
            saved[source['name']] = out_path
            continue

        print(f"  ↓ Fetching {source['name']}...")
        try:
            text = fetch_and_clean(source['url'])
            out_path.write_text(text, encoding="utf-8")
            print(f"  ✓ Saved {len(text):,} characters → {out_path}")
            saved[source['name']] = out_path
            # Be polite — wait 1 second between requests
            time.sleep(1)
        except Exception as e:
            print(f"  ✗ Failed to fetch {source['name']}: {e}")

    return saved


if __name__ == "__main__":
    print("Fetching source documents...")
    results = fetch_all_sources(force_refresh=False)
    print(f"\nDone. {len(results)} sources saved to data/raw/")
    for name, path in results.items():
        size = path.stat().st_size
        print(f"  {name}: {size:,} bytes")