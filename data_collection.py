"""
================================================================
 GREENLENS — STAGE 1: WEB SCRAPER  (v3 — single-site entry point)
================================================================
 This is the v3 scraper engine (junk-page blocklist, precise
 path-segment matching, content verification) with the batch
 "run_data_collection.py" driver replaced by a SINGLE-URL entry
 point, for use inside the live tool where the user submits one
 website via the dashboard.

 CLI usage:
     python run_single_scrape.py https://glenisk.com "Glenisk"

     # company name is optional — derived from the domain if omitted
     python run_single_scrape.py https://glenisk.com

 Programmatic usage (e.g. from the dashboard backend):
     from run_single_scrape import run_single_scrape
     result, filepath = run_single_scrape(url, company_name)

 Output     :  scraped_data/<company_name>.json

 Author : [Your name] — Tech & Pipeline Lead
================================================================
"""

import argparse
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
import anthropic


# ================================================================
# LLM CLASSIFICATION CONFIG
# ================================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # TODO: same key you use in ecgt_classifier.py
LLM_MODEL = "claude-sonnet-4-6"
MAX_LINKS_TO_LLM = 60  # cap link list sent to the LLM per page, to bound tokens/cost


# ================================================================
# CONFIGURATION
# ================================================================

PAGE_KEYWORDS = {
    "About": [
        "about", "about-us", "our-story", "our story", "who-we-are", "who we are",
        "company", "story",
        "ueber-uns", "uber-uns", "unternehmen", "wer-wir-sind", "philosophie",
        "a-propos", "qui-sommes-nous", "notre-histoire", "entreprise", "histoire",
        "over-ons", "wie-we-zijn", "ons-verhaal", "bedrijf",
    ],
    "Sustainability": [
        "sustainability", "sustainable", "environment", "our-impact",
        "responsibility", "green", "planet", "values", "ethics",
        "nachhaltigkeit", "umwelt", "verantwortung", "werte",
        "durabilite", "developpement-durable", "environnement",
        "engagement", "responsabilite", "nos-valeurs",
        "duurzaamheid", "duurzaam", "milieu", "verantwoordelijkheid", "waarden",
    ],
    "Products": [
        "products", "shop", "range", "our-products", "store", "collection",
        "produkte", "sortiment",
        "produits", "boutique", "nos-produits", "gamme",
        "producten", "winkel", "assortiment", "onze-producten",
    ],
}

# Words that, if present in a URL, mean it is NOT a content page.
JUNK_URL_PARTS = [
    "privacy", "cookie", "cmplz", "terms", "legal", "gdpr",
    "login", "signin", "sign-in", "register", "account",
    "cart", "checkout", "basket", "wishlist",
    "search", "sitemap", "feed", "rss", "wp-admin", "wp-login",
    "contact", "faq", "returns", "shipping", "delivery",
    "newsletter", "subscribe", "disclaimer", "imprint", "impressum",
    "mentions-legales", "datenschutz", "agb",
]

# Words we EXPECT to find in the body text of each page type.
# Used to verify the content actually matches the claimed type.
CONTENT_SIGNALS = {
    "About": [
        "about", "founded", "our story", "we are", "family", "history",
        "began", "mission", "started", "journey", "since",
        "gegruendet", "unternehmen", "geschichte",
        "fonde", "histoire", "notre", "depuis",
        "opgericht", "verhaal", "sinds",
    ],
    "Sustainability": [
        "sustainab", "environment", "carbon", "responsib", "eco",
        "planet", "emission", "recycl", "organic", "ethical", "impact",
        "nachhaltig", "umwelt", "verantwort",
        "durable", "environnement", "responsab",
        "duurzaam", "milieu", "verantwoord",
    ],
    "Products": [
        "product", "range", "shop", "buy", "order", "price", "add to",
        "ingredient", "flavour", "flavor", "collection",
        "produkt", "sortiment", "kaufen",
        "produit", "gamme", "acheter",
        "product", "kopen", "assortiment",
    ],
}

COOKIE_BUTTON_TEXTS = [
    "Accept all", "Allow all", "Accept All Cookies", "Accept",
    "I agree", "Agree", "Got it", "OK", "Continue",
    "Allow cookies", "Accept cookies", "I accept",
    "Alle akzeptieren", "Akzeptieren", "Zustimmen", "Einverstanden",
    "Tout accepter", "Accepter", "J'accepte",
    "Alles accepteren", "Accepteren", "Akkoord",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MIN_CONTENT_LENGTH = 200
# A page must hit at least this many content signals to be accepted.
MIN_SIGNAL_MATCHES = 2
OUTPUT_FOLDER = "scraped_data"


# ================================================================
# TEXT CLEANING
# ================================================================

def clean_text(raw_text):
    if not raw_text:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    cleaned = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


# ================================================================
# URL HELPERS
# ================================================================

def build_url_variants(base_url):
    parsed = urlparse(base_url if "://" in base_url else "https://" + base_url)
    host = (parsed.netloc or parsed.path).rstrip("/")
    bare = host[4:] if host.startswith("www.") else host
    variants = [f"https://{bare}", f"https://www.{bare}",
                f"http://{bare}", f"http://www.{bare}"]
    seen, result = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def is_junk_url(url):
    """True if the URL points to a privacy/cookie/legal/etc page."""
    low = url.lower()
    return any(part in low for part in JUNK_URL_PARTS)


def keyword_match_score(url, keyword):
    """
    Returns a match score for how cleanly a keyword matches a URL.
      2 = keyword is a clean path segment   (/about/  or  /about)
      1 = keyword appears somewhere in path
      0 = no match
    Higher is better.
    """
    path = urlparse(url).path.lower()
    segments = [s for s in path.split("/") if s]
    if keyword in segments:
        return 2
    if keyword in path:
        return 1
    return 0


def _derive_company_name(url: str) -> str:
    """
    Falls back to a readable name derived from the domain when
    no company name is supplied, e.g.
    'https://glenisk.com' -> 'Glenisk'
    """
    netloc = urlparse(url).netloc or urlparse(url).path
    netloc = netloc.replace("www.", "")
    base = netloc.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


# ================================================================
# CONTENT VERIFICATION
# ================================================================

def verify_content(text, page_type):
    """
    Checks whether the scraped text actually reads like the
    claimed page type, by counting content-signal matches.
    Returns True if it passes, False if the content does not match.
    """
    low = text.lower()
    signals = CONTENT_SIGNALS.get(page_type, [])
    matches = sum(1 for s in signals if s in low)
    return matches >= MIN_SIGNAL_MATCHES


# ================================================================
# CORE PAGE SCRAPE
# ================================================================

def dismiss_cookie_banner(page):
    for button_text in COOKIE_BUTTON_TEXTS:
        try:
            button = page.get_by_role("button", name=button_text, exact=False)
            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=3000)
                time.sleep(1)
                return True
        except Exception:
            continue
    return False


def get_page_text(page, url):
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        dismiss_cookie_banner(page)
        return clean_text(page.inner_text("body"))
    except Exception:
        return ""


# ================================================================
# AUTO-DISCOVERY  (returns RANKED candidates per page type)
# ================================================================

def discover_links(page, home_url):
    """
    Reads homepage links and returns, for each page type, a list of
    candidate URLs ranked best-first. Junk URLs are excluded.
    """
    candidates = {pt: [] for pt in PAGE_KEYWORDS}

    try:
        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({
                href: e.href,
                text: (e.innerText || '').trim().toLowerCase()
            }))"""
        )
    except Exception:
        return candidates

    home_netloc = urlparse(home_url).netloc

    for page_type, keywords in PAGE_KEYWORDS.items():
        scored = []
        for link in links:
            href = link.get("href") or ""
            text = (link.get("text") or "").lower()

            # Skip external links and junk pages.
            if urlparse(href).netloc != home_netloc:
                continue
            if is_junk_url(href):
                continue

            # Score against every keyword; keep the best score.
            best = 0
            for kw in keywords:
                url_score = keyword_match_score(href, kw)
                text_score = 2 if text == kw else (1 if kw in text else 0)
                best = max(best, url_score, text_score)

            if best > 0:
                scored.append((best, href))

        # Sort best-first and de-duplicate.
        scored.sort(key=lambda x: x[0], reverse=True)
        seen = set()
        for _, href in scored:
            if href not in seen:
                seen.add(href)
                candidates[page_type].append(href)

    return candidates


# ================================================================
# LLM-BASED LINK CLASSIFICATION
# ================================================================

def extract_page_links(page, home_netloc, exclude_urls=None):
    """
    Grabs every internal <a href> on the CURRENT page, with its
    visible anchor text. No keyword filtering — junk pages (privacy,
    cookies, cart, login, etc.) are excluded, external domains are
    excluded, and anything already in exclude_urls is skipped.

    Returns a list of {"url": ..., "text": ...} dicts, deduplicated.
    """
    exclude_urls = exclude_urls or set()
    try:
        raw_links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({
                href: e.href,
                text: (e.innerText || '').trim()
            }))"""
        )
    except Exception:
        return []

    seen = set()
    links = []
    for link in raw_links:
        href = link.get("href") or ""
        text = (link.get("text") or "").strip()
        if not href or href in seen or href in exclude_urls:
            continue
        if urlparse(href).netloc != home_netloc:
            continue
        if is_junk_url(href):
            continue
        seen.add(href)
        links.append({"url": href, "text": text[:80]})  # cap text length
        if len(links) >= MAX_LINKS_TO_LLM:
            break
    return links


def classify_links_with_llm(links, company_name, categories, verbose=True):
    """
    Sends a list of {"url", "text"} links to Claude and asks it to
    pick the single best URL for each requested category.

    categories: list of category names to ask about, e.g.
                ["Sustainability", "Products"]

    Returns a dict: {category: url_or_None, ...}
    """
    empty_result = {c: None for c in categories}
    if not links:
        return empty_result

    link_list_str = "\n".join(
        f"- \"{l['text']}\" -> {l['url']}" for l in links
    )
    categories_str = ", ".join(categories)
    json_fields = ", ".join(f'"{c}": "<url or null>"' for c in categories)

    prompt = f"""You are helping identify specific pages on {company_name}'s food/beverage company website.

Below is a list of links found on one of their pages (anchor text -> URL). The anchor text may be in any language (English, French, German, Dutch, etc.) and may use branded/marketing phrasing rather than literal keywords.

{link_list_str}

From this list, pick the SINGLE best-matching URL for each of these categories: {categories_str}

Category meanings:
- "About": the company's about-us / our-story / who-we-are / company history page
- "Sustainability": a page specifically about environmental sustainability, ethics, responsible sourcing, packaging, organic/regenerative farming, carbon/climate action, or similar ESG/green topics — NOT general company values or news
- "Products": the main products / shop / range page

Respond with ONLY valid JSON, no other text, no markdown fences, in exactly this format:
{{{json_fields}}}

If nothing in the list clearly fits a category, use null for that category. Do not invent URLs that are not in the list above.
"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        # Validate: only accept URLs that were actually in our link list.
        valid_urls = {l["url"] for l in links}
        cleaned = {}
        for c in categories:
            picked = result.get(c)
            cleaned[c] = picked if picked in valid_urls else None

        if verbose:
            print(f"  LLM classification: {cleaned}")
        return cleaned

    except Exception as e:
        if verbose:
            print(f"  LLM classification failed: {type(e).__name__} — {e}")
        return empty_result


# ================================================================
# SCRAPE ONE WEBSITE
# ================================================================

def scrape_sme_website(base_url, company_name, verbose=True):
    if verbose:
        print(f"\n{'='*60}\nScraping: {company_name}\n{'='*60}")

    scraped_pages, scrape_status = {}, {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        # ── Find a working homepage ──
        working_home, home_text = None, ""
        for candidate in build_url_variants(base_url):
            if verbose:
                print(f"  Homepage try -> {candidate}")
            text = get_page_text(page, candidate)
            if len(text) >= MIN_CONTENT_LENGTH:
                working_home, home_text = page.url, text
                if verbose:
                    print(f"    OK  homepage works ({len(text.split())} words)")
                break

        if not working_home:
            if verbose:
                print("    FAILED — homepage unreachable")
            for pt in ["Homepage", "About", "Sustainability", "Products"]:
                scrape_status[pt] = "not_found"
            browser.close()
            return _build_output(company_name, base_url, scraped_pages, scrape_status)

        scraped_pages["Homepage"] = {
            "url": working_home, "word_count": len(home_text.split()),
            "text": home_text, "method": "direct"
        }
        scrape_status["Homepage"] = "success"

        home_netloc = urlparse(working_home).netloc
        remaining = ["About", "Sustainability", "Products"]
        tried_urls = set()

        # ── LLM PASS 1: classify links found on the homepage ──
        homepage_links = extract_page_links(page, home_netloc)
        llm_picks = classify_links_with_llm(homepage_links, company_name, remaining, verbose=verbose)

        for page_type in list(remaining):
            picked_url = llm_picks.get(page_type)
            if not picked_url:
                continue
            tried_urls.add(picked_url)
            text = get_page_text(page, picked_url)
            if len(text) >= MIN_CONTENT_LENGTH:
                scraped_pages[page_type] = {
                    "url": picked_url, "word_count": len(text.split()),
                    "text": text, "method": "llm-classified"
                }
                scrape_status[page_type] = "success"
                remaining.remove(page_type)
                if verbose:
                    print(f"  {page_type:<15} OK ({len(text.split())} words, llm-classified) -> {picked_url}")

        # ── LLM PASS 2: classify links found on the About hub page ──
        # Some sites (e.g. Michel et Augustin, Glenisk) only expose their
        # Sustainability/Products links inside the About/story hub page,
        # not on the homepage itself.
        if remaining and "About" in scraped_pages:
            hub_url = scraped_pages["About"]["url"]
            get_page_text(page, hub_url)  # navigate there
            hub_links = extract_page_links(page, home_netloc, exclude_urls=tried_urls)
            llm_picks_2 = classify_links_with_llm(hub_links, company_name, remaining, verbose=verbose)

            for page_type in list(remaining):
                picked_url = llm_picks_2.get(page_type)
                if not picked_url:
                    continue
                tried_urls.add(picked_url)
                text = get_page_text(page, picked_url)
                if len(text) >= MIN_CONTENT_LENGTH:
                    scraped_pages[page_type] = {
                        "url": picked_url, "word_count": len(text.split()),
                        "text": text, "method": "llm-classified-hub"
                    }
                    scrape_status[page_type] = "success"
                    remaining.remove(page_type)
                    if verbose:
                        print(f"  {page_type:<15} OK ({len(text.split())} words, llm-classified-hub) -> {picked_url}")

        # ── FALLBACK: keyword matching + hub-child content verification ──
        # Only runs for whatever the LLM passes above didn't resolve, so
        # sites the old logic already handled fine take no extra hit.
        if remaining:
            if verbose:
                print(f"  Falling back to keyword discovery for: {remaining}")

            # ── Get ranked candidate links from the homepage ──
            candidates = discover_links(page, working_home)

            # ── SECOND-LEVEL DISCOVERY ──
            # Some sites organize their story into thematic sub-pages
            # under a hub (e.g. glenisk.com/our-story/climate/) that
            # don't contain any of our keywords at all. Keyword matching
            # can't find these, so instead: fetch EVERY child page under
            # the hub and content-verify each one directly.
            hub_candidates = candidates.get("About", [])
            if hub_candidates:
                hub_url = hub_candidates[0]
                hub_path = urlparse(hub_url).path.rstrip("/")

                get_page_text(page, hub_url)  # navigate to the hub page first

                try:
                    child_links = page.eval_on_selector_all(
                        "a[href]",
                        """els => els.map(e => e.href)"""
                    )
                except Exception:
                    child_links = []

                children = []
                seen_children = set()
                for href in child_links:
                    parsed = urlparse(href)
                    if parsed.netloc != home_netloc:
                        continue
                    if is_junk_url(href):
                        continue
                    path = parsed.path.rstrip("/")
                    # Must be a child of the hub, one segment deeper.
                    if path.startswith(hub_path + "/") and path != hub_path:
                        if href not in seen_children:
                            seen_children.add(href)
                            children.append(href)

                for child_url in children:
                    if child_url in tried_urls:
                        continue
                    child_text = get_page_text(page, child_url)
                    if len(child_text) < MIN_CONTENT_LENGTH:
                        continue
                    low = child_text.lower()
                    for page_type in remaining:
                        signals = CONTENT_SIGNALS.get(page_type, [])
                        matches = sum(1 for s in signals if s in low)
                        if matches >= MIN_SIGNAL_MATCHES:
                            if child_url not in candidates[page_type]:
                                candidates[page_type].insert(0, child_url)
                            if verbose:
                                print(f"  Hub child matched {page_type} "
                                      f"({matches} signals) -> {child_url}")

            for page_type in list(remaining):
                found = False

                # Build the full ordered list of URLs to try:
                #   1. ranked auto-discovered candidates (homepage + nested)
                #   2. guessed paths as fallback
                urls_to_try = list(candidates.get(page_type, []))
                for kw in PAGE_KEYWORDS[page_type]:
                    if " " in kw:
                        # Space-form keywords (e.g. "our story") are only used
                        # for matching visible link text — skip them here since
                        # they don't form valid URL paths on their own.
                        continue
                    guess = working_home.rstrip("/") + "/" + kw
                    if guess not in urls_to_try:
                        urls_to_try.append(guess)

                for url in urls_to_try:
                    if is_junk_url(url) or url in tried_urls:
                        continue

                    text = get_page_text(page, url)
                    if len(text) < MIN_CONTENT_LENGTH:
                        continue

                    # CONTENT VERIFICATION — does it read like this page type?
                    if not verify_content(text, page_type):
                        if verbose:
                            print(f"  {page_type:<15} rejected (content mismatch) -> {url}")
                        continue

                    # Passed all checks.
                    method = "auto-discovered" if url in candidates.get(page_type, []) else "guessed-path"
                    scraped_pages[page_type] = {
                        "url": url, "word_count": len(text.split()),
                        "text": text, "method": method
                    }
                    scrape_status[page_type] = "success"
                    found = True
                    if verbose:
                        print(f"  {page_type:<15} OK ({len(text.split())} words, {method}) -> {url}")
                    break

                if not found:
                    scrape_status[page_type] = "not_found"
                    if verbose:
                        print(f"  {page_type:<15} not found")

        browser.close()

    return _build_output(company_name, base_url, scraped_pages, scrape_status)


def _build_output(company_name, base_url, scraped_pages, scrape_status):
    return {
        "company_name": company_name,
        "base_url": base_url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "pages": scraped_pages,
        "scrape_status": scrape_status,
    }


# ================================================================
# SAVE TO JSON
# ================================================================

def save_to_json(scrape_result, output_folder=OUTPUT_FOLDER):
    os.makedirs(output_folder, exist_ok=True)
    safe = scrape_result["company_name"].lower().replace(" ", "_")
    safe = re.sub(r"[^a-z0-9_]", "", safe)
    filepath = os.path.join(output_folder, f"{safe}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(scrape_result, f, indent=4, ensure_ascii=False)
    return filepath


# ================================================================
# SINGLE-URL ENTRY POINT  (replaces the old batch driver)
# ================================================================

def run_single_scrape(url: str, company_name: str = None, verbose: bool = True):
    """
    Scrapes a single SME website and saves the result to JSON.
    This is the function the dashboard/live tool should call.

    Args:
        url: The base URL of the SME website to scrape.
        company_name: Optional display name for the company.
                       Derived from the domain if not given.
        verbose: Whether to print progress to stdout.

    Returns:
        (result, filepath) tuple:
            result   - the dict returned by scrape_sme_website()
            filepath - path to the saved JSON file, or None on failure
    """
    if not company_name:
        company_name = _derive_company_name(url)

    result = scrape_sme_website(url, company_name, verbose=verbose)
    filepath = save_to_json(result)

    if verbose:
        print(f"\nSaved to: {filepath}")
        print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
        for pg, status in result["scrape_status"].items():
            if status == "success":
                info = result["pages"][pg]
                print(f"  {pg:<15} {status:<10} ({info['word_count']} words, via {info['method']})")
            else:
                print(f"  {pg:<15} {status}")

        problems_pages = [
            pg for pg, status in result["scrape_status"].items() if status != "success"
        ]
        if len(problems_pages) >= 2:
            print("\n  NOTE: several page types were not found — check the URL")
            print("  is correct, or this site may use unusual page paths.")
            print("  You may need custom keywords in PAGE_KEYWORDS.")

    return result, filepath


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape a single SME website for GreenLens (Stage 1)."
    )
    parser.add_argument("url", help="Base URL of the SME website, e.g. https://glenisk.com")
    parser.add_argument(
        "company_name",
        nargs="?",
        default=None,
        help="Optional company display name (derived from domain if omitted)",
    )
    return parser.parse_args()


# ================================================================
if __name__ == "__main__":
    args = _parse_args()
    run_single_scrape(args.url, args.company_name)