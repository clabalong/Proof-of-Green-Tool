"""
Diagnostic: prints the raw HTML surrounding 'Danival' on the BioED
members page, so we can see the actual tag/class structure and write
a correct BeautifulSoup selector (the previous crude attempt used
string= exact match, which is too strict for nested/whitespace-padded text).

Usage:
    python inspect_bioed_html.py
"""

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
}

url = "https://bioed.fr/la-communaute/"
resp = requests.get(url, headers=HEADERS, timeout=20)
html = resp.text

# Find every occurrence of "Danival" and print ~300 chars of context
# around each, so we can see the surrounding tags/classes.
search_term = "Danival"
start = 0
occurrence = 0
while True:
    idx = html.find(search_term, start)
    if idx == -1:
        break
    occurrence += 1
    snippet_start = max(0, idx - 200)
    snippet_end = min(len(html), idx + 200)
    print(f"\n{'='*60}\nOccurrence #{occurrence} (char {idx})\n{'='*60}")
    print(html[snippet_start:snippet_end])
    start = idx + len(search_term)

print(f"\n\nTotal occurrences of '{search_term}': {occurrence}")
