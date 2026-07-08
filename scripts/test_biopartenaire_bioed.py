"""
Quick feasibility test: can we get Biopartenaire and BioED member data
via plain `requests` (fast, dynamic, no browser needed) — or do we need
Playwright like we did for Bord Bia (JS-rendered content)?

Run this locally and check the printed output:
  - If company names/domains show up in the raw HTML length check below,
    plain requests works and we can build a live "API-style" checker.
  - If the counts are 0 / names are missing, the content is JS-rendered
    and we'd need Playwright instead.

Usage:
    pip install requests beautifulsoup4 --break-system-packages
    python test_biopartenaire_bioed.py
"""

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}

# Known real members, to check if their name/domain actually appears
# in the raw HTML we get back.
KNOWN_BIOPARTENAIRE_MEMBERS = ["danival.fr", "belledonne.bio", "kaoka.fr"]
KNOWN_BIOED_MEMBERS = ["Danival", "Pain de Belledonne", "Kaoka"]


def test_biopartenaire():
    url = "https://www.biopartenaire.com/fr/qui-sommes-nous/"
    print(f"\n{'='*60}\nTesting Biopartenaire: {url}\n{'='*60}")

    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"Status code: {resp.status_code}")
    print(f"Raw HTML length: {len(resp.text)} characters")

    soup = BeautifulSoup(resp.text, "html.parser")
    all_links = [a.get("href", "") for a in soup.find_all("a")]
    print(f"Total <a> tags found: {len(all_links)}")

    for domain in KNOWN_BIOPARTENAIRE_MEMBERS:
        found = any(domain in href for href in all_links)
        print(f"  '{domain}' found in raw HTML links: {found}")


def test_bioed():
    url = "https://bioed.fr/la-communaute/"
    print(f"\n{'='*60}\nTesting BioED: {url}\n{'='*60}")

    resp = requests.get(url, headers=HEADERS, timeout=20)
    print(f"Status code: {resp.status_code}")
    print(f"Raw HTML length: {len(resp.text)} characters")

    for name in KNOWN_BIOED_MEMBERS:
        found = name in resp.text
        print(f"  '{name}' found in raw HTML text: {found}")

    # Also print a snippet of the page around the member list, to help
    # figure out the right CSS selector for a real parser afterward.
    soup = BeautifulSoup(resp.text, "html.parser")
    # crude guess at a likely container class — adjust after inspecting
    # actual output below
    candidates = soup.find_all(["li", "div", "a"], string=lambda s: s and "Danival" in s)
    print(f"\nElements containing 'Danival': {len(candidates)}")
    for c in candidates[:3]:
        print(f"  tag={c.name}, class={c.get('class')}, parent={c.parent.name if c.parent else None}")


if __name__ == "__main__":
    test_biopartenaire()
    test_bioed()
