# Proof of Green

**An AI-powered green claims compliance tool: auditing EU food SME websites for greenwashing compliance against EU Directive 2024/825 (the "Empowering Consumers for the Green Transition" Directive, ECGT).**

MSc Business Analytics capstone dissertation project, Trinity Business School, Trinity College Dublin, in collaboration with EY Ireland.

- **Supervisor:** Dr. Baidyanath Biswas
- **Team:** Sundus Afreen, Tuna Cemal Erdem, Yifei Yu
- **Submission:** July 2026

---

## What this does

Proof of Green takes a single EU SME's website URL and runs it through a multi-stage pipeline that:

1. **Scrapes** the company's homepage, About, Sustainability, and Products pages
2. **Extracts** every environmental/sustainability claim made on those pages, using an LLM
3. **Independently verifies** each extracted claim using a second, separate model
4. **Cross-checks** the company against six real EU/national certification registries, to catch claimed certifications that can't be independently verified — a core greenwashing signal under ECGT
5. **Classifies** every claim RED/AMBER/GREEN against ECGT compliance criteria, with a cited rule, plain-language explanation, remediation guidance, and a flag for claims that genuinely warrant human review
6. **Checks recent news** for independent corroboration of environmental controversies (lawsuits, fines, greenwashing accusations) not visible from the company's own site

The output is a single Excel workbook per company: every claim, its category, verification status, certification cross-check results, RAG classification with rationale, and any relevant news findings. A Streamlit dashboard (`app.py`) sits on top of the pipeline for interactive use.

---

## Pipeline architecture

Each stage lives in its own file inside the `pipeline/` package and exposes one importable function, so the team can work on different stages without constant merge conflicts. `pipelineV1.py` is a thin orchestrator that imports and chains them together — it contains no scraping/extraction/verification/classification logic itself.

```
Stage 1: data_collection.py       → scrapes the company website
Stage 2: extract_claims.py        → extracts + verifies environmental claims
Stage 3: cert_verifier_api.py     → checks 6 certification registries
Stage 4: ecgt_pipeline_stage4.py  → RAG classification against ECGT rules
         news_verifier.py         → recent-news corroboration check
         pipelineV1.py            → chains everything for one company URL
```

**Concurrency:** Stage 3 and the news check depend only on the company's name and URL — not on anything Stage 1 scrapes or Stage 2 extracts — so both run concurrently with the Stage 1→2 chain rather than waiting for it to finish. Stage 4 runs last, since it needs Stage 2's claims and Stage 3's certification results merged together first.

### Stage 1 — `pipeline/data_collection.py`

Scrapes a company's Homepage, About, Sustainability, and Products pages using Playwright.

Page discovery works in layers, since food SME site navigation varies enormously:

1. **LLM-based link classification** (primary) — every link on the homepage (and, if needed, on the About/story hub page) is sent to Claude, which picks the best URL per page type. This handles branded/non-literal navigation labels in any language (e.g. "Tribu engagée," "L'aventure," "Notre histoire") that keyword matching can't.
2. **Keyword matching + hub-child content verification** (fallback) — only runs for whatever the LLM pass didn't resolve. Includes multilingual keyword lists and a mechanism for finding thematically-organized sub-pages nested under a hub page (e.g. `/our-story/climate/`) via direct content verification rather than keyword guessing.
3. **Guessed common paths** (last resort) — e.g. `/sustainability`, `/about-us`.

Junk pages (privacy policy, cookie notices, cart, login, etc.) are filtered out at every stage via a URL blocklist. Cookie-consent banners are dismissed via a combination of button-text matching and CSS selectors for common consent platforms (Complianz, Cookiebot, OneTrust, etc.).

**Output:** one JSON file per company in `scraped_data/`, containing the scraped text and metadata for each page type, plus which discovery method resolved it.

**Key functions:**
- `run_single_scrape(url, company_name=None)` → `(result_dict, json_filepath)`
- `scrape_sme_website(base_url, company_name)` — the core scraper, callable directly if you don't need the JSON-saving wrapper

### Stage 2 — `pipeline/extract_claims.py`

Reads a company's scraped pages and extracts every environmental/sustainability claim using Claude, then independently verifies each one using a separate OpenAI model — a genuinely different provider, not the same model checking its own work.

**Extraction categories:** carbon, biodiversity, packaging, water, sourcing, certification, waste, general.

**Verification checks five dimensions:** textual fidelity to the source, environmental relevance, correct attribution to the company (not an unrelated quote), completeness of the extracted passage, and category correctness.

**Resilience:** malformed JSON output (commonly caused by an unescaped quote inside a verbatim claim) is recovered via a `json_repair` fallback rather than losing the whole page's claims. If that also fails, the raw response is saved to `debug_failed_json/` for inspection.

**No-GPT variant:** `pipeline/extract_claimsNO_GPT_Check.py` + `archive/pipelineNO_GPT_Check.py` provide a cheaper alternative using self-reported confidence thresholding instead of a second-model check, for situations where the OpenAI cost/latency isn't justified. Kept in sync with the main extraction prompt; the verification mechanism is the only intentional difference.

**Key functions:**
- `process_scrape_result(anthropic_client, openai_client, scrape_result_dict)` → claims dict
- `write_excel(results, output_path)`

### Stage 3 — `pipeline/cert_verifier_api.py`

Checks a company against **six** certification registries:

| Registry | Method | Notes |
|---|---|---|
| **EMAS** | Fetch-all-then-cache | Bulk download of the entire EU registry, filtered client-side by country and cached in memory |
| **EU Organic (TRACES NT)** | Parallel-batch paginated fetch | Pages fetched in concurrent batches (not one at a time), cached per country after first fetch |
| **B Corp** | Live search (Typesense) | Genuinely dynamic — no pre-fetching |
| **Bord Bia (Origin Green)** | Local JSON cache (`data/bordbia_members_cache.json`) | Live scraping is unreliable due to bot-detection; uses a pre-scraped snapshot. Matches primarily by domain, with fuzzy name matching as fallback |
| **Biopartenaire** | Live HTTP fetch | Confirmed server-rendered plain HTML, no Playwright needed. Domain-matching only |
| **BioED** | Live HTTP fetch | Confirmed server-rendered plain HTML. Fuzzy name matching only |

**Fuzzy name matching** (`difflib`), refined through testing: legal-suffix stripping (Ltd, GmbH, SARL, etc.), word-boundary-anchored containment (not raw substring matching, which caused false positives), and an empirically-set match threshold of 90/100.

**Country override:** pass an explicit country to skip TLD-guessing entirely (`run_certification_stage(company_name, countries=["Ireland"], ...)`) — worth doing whenever the real country is known, since an ambiguous domain (`.com`, `.bio`) otherwise falls back to checking all four panel countries.

**Robustness:** every registry lookup is wrapped in error handling — a timeout or failure on one registry degrades gracefully to "unable to verify" rather than crashing the whole pipeline run.

**Key functions:**
- `run_certification_stage(company_name, countries=None, company_url=None)` → dict with all 6 registries' results
- `merge_certifications_into_claims(claims_result, cert_result)` — stamps every claim with per-registry verification flags
- `append_certifications_sheet(excel_path, results)` — adds a "Certifications" sheet to the Excel output

### Stage 4 — `pipeline/ecgt_pipeline_stage4.py`

The substantive ECGT compliance classifier. Two-step design, intentionally kept separate:

- **Step A (the decision):** a validated, unmodified classification call producing RED/AMBER/GREEN — few-shot prompted against ECGT rules, forced single-word output.
- **Step B (the explanation):** a second call that receives the label Step A already produced (explicitly told it's final) and documents which rule applies, cites the specific Directive provision, gives remediation guidance (REMOVE/REWRITE/SUBSTANTIATE/NONE), and flags genuinely borderline cases for human review. It cannot change the label.

Classification runs **concurrently across claims** (not one at a time) — each claim's Step A + Step B sequence is fully independent of every other claim's, so multiple claims classify in parallel without touching the validated decision procedure itself.

**Key function:**
- `run_ecgt_classification_stage(claims_result, anthropic_client)` → adds six fields to every claim (`ecgt_label`, `ecgt_rule_triggered`, `ecgt_citation`, `ecgt_explanation`, `ecgt_guidance`, `ecgt_review_flag`) and returns a summary dict

### News check — `pipeline/news_verifier.py`

Searches recent news (Google News RSS, up to a year back) for independent negative corroboration — lawsuits, fines, greenwashing accusations, regulatory action — that a company's own website wouldn't surface. Deliberately negative-signal-only: positive press doesn't tell us anything about whether specific claims are accurate. A Claude-based interpretation step filters out namesake mismatches and irrelevant coverage before flagging anything as a genuine controversy. Most companies in a small-SME sample will correctly return no results in any given window — this is expected, not a bug.

### `pipeline/pipelineV1.py` — orchestrator

Chains every stage for one company URL, with Stage 3/news-check parallelization and an optional `on_progress` callback for UI integration.

```python
from pipeline.pipelineV1 import run_pipeline

claims_result, cert_result, ecgt_result, news_result, excel_path = run_pipeline(
    "https://glenisk.com", "Glenisk", countries=["Ireland"]
)
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

**3. Set your API keys:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export NEWSAPI_KEY="..."   # only needed if using the legacy NewsAPI path; Google News RSS needs no key
```
Add these to your shell profile (`~/.zshrc` on Mac) to persist across sessions. If running via PyCharm's Run button rather than Terminal, also add them under that run configuration's **Environment variables** field — PyCharm run configs don't inherit shell environment variables automatically. For Streamlit, set them via `.streamlit/secrets.toml` if deploying to Streamlit Cloud, or the environment/`.env` file for a self-hosted deployment.

**4. `data/bordbia_members_cache.json` must be present** (Stage 3 reads it directly via a path resolved relative to `cert_verifier_api.py`'s own location, so it works regardless of what directory the app is launched from).

---

## Usage

**Run the Streamlit dashboard:**
```bash
streamlit run app.py
```

**Run the full pipeline for one company from the CLI:**
```bash
python pipeline/pipelineV1.py https://glenisk.com "Glenisk"
python pipeline/pipelineV1.py https://glenisk.com "Glenisk" "Ireland"   # explicit country, skips TLD-guessing
```
Company name is optional — derived from the domain if omitted.

**Run a single stage standalone** (useful for debugging one part without re-running everything):
```bash
python pipeline/data_collection.py https://glenisk.com "Glenisk"
python pipeline/cert_verifier_api.py   # runs the built-in demo check on a few sample companies
```

**Batch mode for Stage 2** (processing a folder of already-scraped JSON files):
```bash
python pipeline/extract_claims.py --input ./scraped_data/ --output results.xlsx
```

---

## Project structure

```
├── app.py                          # Streamlit dashboard entry point
├── pipeline/                       # live pipeline package
│   ├── __init__.py
│   ├── data_collection.py          # Stage 1: scraping
│   ├── extract_claims.py           # Stage 2: claim extraction + GPT verification
│   ├── cert_verifier_api.py        # Stage 3: certification checks (6 registries)
│   ├── ecgt_pipeline_stage4.py     # Stage 4: ECGT RAG classification
│   ├── claude_classifier_tool.py   # Step A classification internals (validated, unmodified)
│   ├── news_verifier.py            # recent-news corroboration check
│   └── pipelineV1.py               # main orchestrator
├── Classifier/                     # validation-only scripts — NOT part of the live pipeline
│   ├── claude_classifier.py
│   ├── evaluate_f1.py
│   ├── evaluate_kappa.py
│   ├── gemini_classifier.py
│   └── validation_test_set_real.csv
├── archive/                        # deprecated, kept for reference only
│   ├── pipeline_Old.py
│   ├── pipelineNO_GPT_Check.py     # orchestrator using the confidence-threshold variant
│   └── extract_claimsNO_GPT_Check.py  # Stage 2, confidence-threshold variant
├── data/
│   └── bordbia_members_cache.json  # static Origin Green members snapshot
├── test_scripts/                        # one-off diagnostic scripts
├── .streamlit/                     # Streamlit config/secrets
├── requirements.txt
├── .gitignore
└── scraped_data/                   # generated at runtime, gitignored
```

---

## Known limitations

Worth stating explicitly for the dissertation's methodology/limitations section:

- **Confidence scores are self-reported by the extracting LLM**, not calibrated probabilities — the second-model verification pass is a partial mitigation, not a full fix, since Anthropic's API doesn't expose token-level log-probabilities.
- **Bord Bia matching relies on a static cache**, not a live feed — if Origin Green's membership list changes, the cache needs to be manually regenerated.
- **Biopartenaire and BioED are French-specific labels** — only meaningfully relevant to French companies in the sample; expect these columns to be empty for the rest, which is expected, not a bug.
- **The 90-point fuzzy match threshold** was tuned empirically against observed true/false match pairs in this specific sample.
- **Certain company websites employ anti-scraping measures** (headless-browser detection) that block automated access even after standard evasion attempts; affected companies were excluded from the sample rather than pursued via further circumvention.
- **The news check uses an unofficial, undocumented Google feed** with no published SLA, returns a relevance-ranked (not exhaustive) result set regardless of the requested time window, and provides no full article text without a separate scraping step.
- **Second-model verification and classification add real cost and latency**, and depend on external providers' APIs remaining available.

---

## Team

Built by Sundus Afreen, Tuna Cemal Erdem, and Yifei Yu, as part of an MSc Business Analytics capstone at Trinity College Dublin, in collaboration with EY Ireland, supervised by Dr. Baidyanath Biswas.
