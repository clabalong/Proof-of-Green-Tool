"""
================================================================
 GREENLENS — PIPELINE ORCHESTRATOR
 (Stage 1 -> Stage 2 -> Stage 3 -> Stage 4 -> News Check)
================================================================
 Thin glue script. Does not duplicate any scraping, extraction,
 certification, classification, or news-check logic — it just
 imports the reusable functions from each stage and chains them
 together for a single company URL.

 PARALLELIZED: Stage 3 (certification check) and the news check
 depend ONLY on the company name and URL — neither needs anything
 Stage 1 scrapes or Stage 2 extracts. So both run concurrently with
 the Stage 1 -> Stage 2 chain instead of waiting for it to finish
 first. Stage 4 (ECGT classification) still runs last, since it
 needs BOTH Stage 2's claims AND Stage 3's certification results
 merged together.

 Dependency chain:
     Stage 1 -> Stage 2 ─┐
     Stage 3 ────────────┼─> merge -> Stage 4
     News check ─────────┘  (independent, just runs alongside)

 This is what the live tool's dashboard backend should call.

 CLI usage:
     python pipelineV1.py https://glenisk.com "Glenisk"

 Programmatic usage:
     from pipelineV1 import run_pipeline
     claims_result, cert_result, ecgt_result, news_result, excel_path = run_pipeline(url, company_name)

 Requires ANTHROPIC_API_KEY and OPENAI_API_KEY to be set in the
 environment (used by Stage 1's LLM link classification, Stage 2's
 extraction + verification, Stage 4's classification, and the news
 check's interpretation step — Stage 4 and the news check reuse the
 same Anthropic client already created for Stages 1/2, no separate
 key needed; the Anthropic SDK's client is safe to share across
 threads for concurrent requests).
 NEWSAPI_KEY is also required for the news check — if unset, that
 stage is skipped gracefully rather than failing the whole pipeline.
================================================================
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic
import openai

from data_collection import run_single_scrape, _derive_company_name
from extract_claims import process_scrape_result, write_excel
from cert_verifier_api import run_certification_stage, append_certifications_sheet, merge_certifications_into_claims
from ecgt_pipeline_stage4 import run_ecgt_classification_stage
from news_verifier import check_news_controversy, append_news_sheet


def run_pipeline(url: str, company_name: str = None, verbose: bool = True, cert_verifier=None):
    """
    Runs Stage 1 (scrape) -> Stage 2 (claim extraction + GPT verification)
    IN PARALLEL with Stage 3 (certification check) and the news
    controversy check, then Stage 4 (ECGT classification) once Stage 2
    and Stage 3 have both completed. Saves a single-company Excel output
    with "Certifications" and "News Check" sheets (ECGT fields are
    merged directly into the "All Claims" sheet, same pattern as the
    certification fields).

    Args:
        url: company website URL
        company_name: optional display name (derived from domain if omitted —
                       resolved ONCE upfront here, so Stage 1, Stage 3, and
                       the news check all use the identical name rather than
                       Stage 1 deriving its own copy independently)
        verbose: whether to print progress to stdout
        cert_verifier: an existing CertVerifier instance to reuse across
                        multiple calls (recommended for batch runs, so
                        EMAS/EU Organic country data is fetched once, not
                        once per company). A new one is created if not given.

    Returns:
        (claims_result, cert_result, ecgt_result, news_result, excel_path) tuple
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("[ERROR] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("[ERROR] OPENAI_API_KEY environment variable not set.")
        sys.exit(1)

    anthropic_client = anthropic.Anthropic(api_key=anthropic_key)
    openai_client = openai.OpenAI(api_key=openai_key)

    # Resolve the company name ONCE, upfront — before any stage runs.
    # This is what makes parallelizing Stage 3 / the news check safe:
    # both need a company name, and previously that name only existed
    # after Stage 1 finished (derived internally from the URL if not
    # given). Deriving it here means every stage uses the identical
    # name from the start, and Stage 3/news check can begin immediately
    # instead of waiting on Stage 1.
    if not company_name:
        company_name = _derive_company_name(url)

    def _run_scrape_and_extract():
        if verbose:
            print(f"\n{'#'*60}\n# STAGE 1 — SCRAPING\n{'#'*60}")
        scrape_result, json_path = run_single_scrape(url, company_name, verbose=verbose)

        if verbose:
            print(f"\n{'#'*60}\n# STAGE 2 — CLAIM EXTRACTION + GPT VERIFICATION\n{'#'*60}")
        source_label = Path(json_path).name if json_path else ""
        claims_result = process_scrape_result(
            anthropic_client, openai_client, scrape_result, source_file=source_label, verbose=verbose
        )
        return scrape_result, json_path, claims_result

    def _run_cert_check():
        if verbose:
            print(f"\n{'#'*60}\n# STAGE 3 — CERTIFICATION CHECK (running in parallel with Stage 1/2)\n{'#'*60}")
        return run_certification_stage(
            company_name, company_url=url, verifier=cert_verifier, verbose=verbose
        )

    def _run_news_check():
        if verbose:
            print(f"\n{'#'*60}\n# NEWS CONTROVERSY CHECK (running in parallel with Stage 1/2)\n{'#'*60}")
        return check_news_controversy(company_name, anthropic_client, verbose=verbose)

    # Stage 3 and the news check need only company_name/url, so they run
    # concurrently with the Stage 1 -> Stage 2 chain rather than after it.
    # NOTE: with verbose=True, output from all three interleaves in the
    # terminal since they print concurrently — a known, accepted tradeoff
    # for the faster total runtime. Each line is still prefixed with which
    # stage it's from, so it's readable, just not perfectly sequential.
    with ThreadPoolExecutor(max_workers=3) as executor:
        scrape_extract_future = executor.submit(_run_scrape_and_extract)
        cert_future = executor.submit(_run_cert_check)
        news_future = executor.submit(_run_news_check)

        scrape_result, json_path, claims_result = scrape_extract_future.result()
        cert_result = cert_future.result()
        news_result = news_future.result()

    merge_certifications_into_claims(claims_result, cert_result)

    # Stage 4 needs BOTH Stage 2's claims and Stage 3's cert results
    # merged together, so it can only start once both of the above are
    # fully done — no parallelization opportunity here.
    if verbose:
        print(f"\n{'#'*60}\n# STAGE 4 — ECGT CLASSIFICATION\n{'#'*60}")
    ecgt_result = run_ecgt_classification_stage(claims_result, anthropic_client, verbose=verbose)

    safe_name = scrape_result["company_name"].lower().replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = Path(f"greenlens_claims_{safe_name}_{timestamp}.xlsx")
    write_excel([claims_result], excel_path)
    append_certifications_sheet(excel_path, {scrape_result["company_name"]: cert_result})
    append_news_sheet(excel_path, {scrape_result["company_name"]: news_result})

    if verbose:
        print(f"\n{'='*60}")
        print(f"  PIPELINE COMPLETE — {scrape_result['company_name']}")
        print(f"  Claims extracted : {len(claims_result['claims'])}")
        matched_registries = [r for r, info in cert_result.items() if info["matched"]]
        print(f"  Certifications   : {', '.join(matched_registries) if matched_registries else 'none matched'}")
        print(f"  ECGT labels      : {ecgt_result['label_distribution']}")
        print(f"  Flagged for review: {ecgt_result['review_flagged']}/{ecgt_result['total_claims']}")
        print(f"  News controversy : {'YES — see News Check sheet' if news_result.get('controversy_detected') else 'none detected'}")
        print(f"  Scraped JSON     : {json_path}")
        print(f"  Excel output     : {excel_path}")
        print(f"{'='*60}\n")

    return claims_result, cert_result, ecgt_result, news_result, excel_path


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Run the GreenLens Stage 1 -> Stage 2 -> Stage 3 -> Stage 4 -> News Check pipeline for a single company."
    )
    parser.add_argument("url", help="Base URL of the SME website, e.g. https://glenisk.com")
    parser.add_argument(
        "company_name",
        nargs="?",
        default=None,
        help="Optional company display name (derived from domain if omitted)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_pipeline(args.url, args.company_name)