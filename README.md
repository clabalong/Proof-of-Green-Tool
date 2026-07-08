# GreenLens

**An AI pipeline for auditing EU food SME websites for greenwashing compliance against EU Directive 2024/825 (the "Empowering Consumers for the Green Transition" Directive, ECGT).**

MSc Business Analytics capstone dissertation project, Trinity Business School, Trinity College Dublin, in collaboration with EY Ireland.

- **Supervisor:** Dr. Baidyanath Biswas
- **Team:** Tuna Erdem, Sundus, Yifei
- **Deadline:** 24 July 2026

---

## What this does

GreenLens takes a single EU food/beverage SME's website URL and runs it through a multi-stage pipeline that:

1. **Scrapes** the company's homepage, About, Sustainability, and Products pages
2. **Extracts** every environmental/sustainability claim made on those pages, using an LLM
3. **Independently verifies** each extracted claim using a second, separate model
4. **Cross-checks** the company against six real EU/national certification registries, to catch claimed certifications that can't be independently verified — a core greenwashing signal under ECGT

The output is a single Excel workbook per company: every claim, its category, its confidence and verification status, and whether the company's claimed certifications actually check out.

This was built for a sample of ~15–20 SMEs across Ireland, France, Belgium, and Austria.

---

## Pipeline architecture

Each stage lives in its own file and exposes one importable function, so the three of us can work on different stages without constant merge conflicts. `pipelineV1.py` is a thin orchestrator that imports and chains them together — it contains no scraping/extraction/verification logic itself.

```
Stage 1: data_collection.py       → scrapes the company website
Stage 2: extract_claims.py        → extracts + verifies environmental claims
Stage 3: cert_verifier_api.py     → checks 6 certification registries
         pipelineV1.py            → chains Stages 1-3 for one company URL
```

### Stage 1 — `data_collection.py`

Scrapes a company's Homepage, About, Sustainability, and Products pages using Playwright.

Page discovery works in layers, since food SME site navigation varies enormously:

1. **LLM-based link classification** (primary) — every link on the homepage (and, if needed, on the About/story hub page) is sent to Claude, which picks the best URL per page type. This handles branded/non-literal navigation labels in any language (e.g. "Tribu engagée," "L'aventure," "Notre histoire") that keyword matching can't.
2. **Keyword matching + hub-child content verification** (fallback) — only runs for whatever the LLM pass didn't resolve. Includes multilingual keyword lists and a mechanism for finding thematically-organized sub-pages nested under a hub page (e.g. `/our-story/climate/`) via direct content verification rather than keyword guessing.
3. **Guessed common paths** (last resort) — e.g. `/sustainability`, `/about-us`.

Junk pages (privacy policy, cookie notices, cart, login, etc.) are filtered out at every stage via a URL blocklist.

**Output:** one JSON file per company in `scraped_data/`, containing the scraped text and metadata for each page type, plus which discovery method resolved it (useful for the dissertation's methodology section — you can report what % of pages needed LLM-based vs. keyword-based discovery).

**Key functions:**
- `run_single_scrape(url, company_name=None)` → `(result_dict, json_filepath)`
- `scrape_sme_website(base_url, company_name)` — the core scraper, callable directly if you don't need the JSON-saving wrapper

### Stage 2 — `extract_claims.py`

Reads a company's scraped pages and extracts every environmental/sustainability claim using Claude, then independently verifies each one using GPT-5.5 (a separate model/provider — not the same model checking its own work).

**Extraction categories:** carbon, biodiversity, packaging, water, sourcing, certification, waste, general.

**Why a separate verification pass:** self-reported LLM confidence scores (asked for during the same generation pass as extraction) are a known-weak signal — models tend to be overconfident, and there's no calibration guarantee. Anthropic's API doesn't expose token-level log-probabilities either, so a genuinely independent second model judging the extraction ("does this claim actually appear in the source? is the category reasonable?") is the strongest cheap signal currently available.

**Resilience:** if the model's JSON output is malformed (a common cause: an unescaped quote character inside a verbatim claim, e.g. from a customer testimonial or normalized guillemets), a `json_repair` fallback recovers the claims instead of silently losing the whole page. If even that fails, the raw response is saved to `debug_failed_json/` for inspection.

**Output columns include:** Company, Page, Category, Verbatim Claim, English Translation, Language, Confidence, GPT Verified, GPT Notes, plus the six certification columns (added by Stage 3's merge step).

**Key functions:**
- `process_scrape_result(anthropic_client, openai_client, scrape_result_dict)` → claims dict (the one to call from a pipeline; takes Stage 1's output directly, no disk round-trip needed)
- `process_company_file(anthropic_client, openai_client, json_path)` — file-based wrapper, used by the CLI/batch mode
- `write_excel(results, output_path)`

### Stage 3 — `cert_verifier_api.py`

Checks a company against **six** certification registries, each with a genuinely different data-access strategy:

| Registry | Method | Notes |
|---|---|---|
| **EMAS** | Fetch-all-then-cache | One bulk download of the entire EU registry, filtered client-side by country and cached in memory |
| **EU Organic (TRACES NT)** | Paginated per-country fetch | Cached per country after first fetch |
| **B Corp** | Live search (Typesense) | Genuinely dynamic — no pre-fetching |
| **Bord Bia (Origin Green)** | Local JSON cache (`bordbia_members_cache.json`) | Origin Green's site has bot-detection that makes live scraping fragile, so this uses a pre-scraped snapshot. Matches primarily by **domain** (most cache entries are raw URLs, not clean names), with fuzzy name matching as fallback |
| **Biopartenaire** | Live HTTP fetch | Confirmed server-rendered plain HTML, no Playwright needed. Domain-matching only (the page is a logo grid linking to each member's own site) |
| **BioED** | Live HTTP fetch | Confirmed server-rendered plain HTML. Name-based fuzzy matching only (page exposes names, not links) |

**Fuzzy name matching:** uses `difflib`, with two important refinements found through testing:
- **Legal-suffix stripping** (Ltd, GmbH, SARL, etc.) before comparison — otherwise genuine matches like "Glenisk" vs. "Glenisk Ltd" score below threshold purely due to the extra word.
- **Word-boundary containment bonus** — required to be anchored to whole words, not raw substrings, after discovering that raw substring matching produces false positives (e.g. "Aniva SRL" coincidentally containing the letters of "Danival").
- **Match threshold: 90/100** — empirically chosen; genuine matches (after the above fixes) score 95–100, while coincidental short-name overlaps top out around 83.

**Country-guessing optimization:** for EU Organic specifically (the only registry with real per-country network cost), the company's domain TLD is used to guess a single country to check when confident (`.ie` → Ireland, `.fr` → France, etc.), falling back to checking all four countries when the guess would be unreliable (e.g. `.com`).

**Key functions:**
- `run_certification_stage(company_name, company_url=None)` → dict with all 6 registries' results
- `merge_certifications_into_claims(claims_result, cert_result)` — stamps every claim with per-registry verification flags, so the flat claims dataset is self-contained for downstream classification (no joins needed)
- `append_certifications_sheet(excel_path, results)` — adds a "Certifications" summary sheet to the Excel output

### `pipelineV1.py` — orchestrator

Chains Stages 1→2→3 for one company URL. This is what any future dashboard/UI should call.

```python
from pipelineV1 import run_pipeline
claims_result, cert_result, excel_path = run_pipeline("https://glenisk.com", "Glenisk")
```

---

## Setup

**1. Clone and enter the project folder, then create a virtual environment:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**2. Install dependencies:**
```bash
pip install -r requirements.txt
playwright install chromium
```

**3. Set your API keys** (needed by Stages 1–3):
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
```
Add these to your shell profile (`~/.zshrc` on Mac) to persist across sessions. If running via PyCharm's Run button rather than Terminal, also add them under that run configuration's **Environment variables** field — PyCharm run configs don't inherit shell environment variables automatically.

**4. Make sure `bordbia_members_cache.json` is present** in the project root (Stage 3 reads it directly; it's a static snapshot, not fetched live, since Origin Green's site has bot-detection).

---

## Usage

**Run the full pipeline for one company:**
```bash
python pipelineV1.py https://glenisk.com "Glenisk"
```
Company name is optional — derived from the domain if omitted.

**Run a single stage standalone** (useful for debugging one part without re-running everything):
```bash
python data_collection.py https://glenisk.com "Glenisk"
python cert_verifier_api.py   # runs the built-in demo check on a few sample companies
```

**Batch mode for Stage 2** (processing a folder of already-scraped JSON files):
```bash
python extract_claims.py --input ./scraped_data/ --output results.xlsx
```

---

## Project structure

```
├── data_collection.py            # Stage 1: scraping
├── extract_claims.py             # Stage 2: claim extraction + GPT verification
├── cert_verifier_api.py          # Stage 3: certification checks (6 registries)
├── pipelineV1.py                 # Orchestrator, chains Stages 1-3
├── pipeline_Old.py               # Deprecated, kept for reference only
├── bordbia_members_cache.json    # Static Origin Green members snapshot
├── requirements.txt
├── .gitignore
├── scripts/
│   ├── inspect_bioed_html.py     # One-off diagnostic: BioED page structure
│   └── test_biopartenaire_bioed.py  # One-off diagnostic: feasibility test
└── scraped_data/                 # Generated at runtime, gitignored
```

---

## Known limitations

Worth stating explicitly for the dissertation's methodology/limitations section:

- **Confidence scores are self-reported by the extracting LLM**, not calibrated probabilities — the GPT verification pass is a partial mitigation, not a full fix, since Anthropic's API doesn't expose token-level log-probabilities.
- **Bord Bia matching relies on a static cache**, not a live feed — if Origin Green's membership list changes, the cache needs to be manually regenerated.
- **Biopartenaire and BioED are French-specific labels** — only meaningfully relevant to the French companies in the sample (Danival, Belledonne); expect these columns to be empty for the Irish/Belgian/Austrian companies, which is expected, not a bug.
- **The 90-point fuzzy match threshold** was tuned empirically against observed true/false match pairs in this specific sample — worth a sensitivity check if the SME panel changes significantly.
- **GPT verification adds cost and latency** per page (one extra API call), and depends on a second provider's API being available.

---

## Roadmap

- **Stage 4/5 — ECGT rules engine**: RED/AMBER/GREEN classification against Directive 2024/825 and Ireland SI 124/2026, few-shot prompted. Currently under active tuning (AMBER recall and French-claim RED-classification issues being investigated separately).
- **Batch runner** across the full SME sample, reusing a single `CertVerifier` instance so EMAS/EU Organic data is fetched once rather than once per company.
- **Dashboard/demo** (Streamlit or similar) wrapping `run_pipeline()` for a live, single-URL-in demo.

---

## Team

Built by Tuna Erdem, with Sundus and Yifei, as part of an MSc Business Analytics capstone at Trinity College Dublin, in collaboration with EY Ireland.
