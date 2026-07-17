"""
================================================================
 GREENLENS — PIPELINE ORCHESTRATOR
 (Stage 1 -> Stage 2 -> Stage 3 -> Stage 4 -> News Check)
================================================================
 Thin glue script. Does not duplicate any scraping, extraction,
 certification, classification, or news-check logic — it just
 imports the reusable functions from each stage and chains them
 together for a single company URL.

 This is what the live tool's dashboard backend should call.

 CLI usage:
     python pipelineV1.py https://glenisk.com "Glenisk"

 Programmatic usage:
     from pipelineV1 import run_pipeline
     claims_result, cert_result, ecgt_result, news_result, excel_path = run_pipeline(url, company_name)

 Requires ANTHROPIC_API_KEY and OPENAI_API_KEY to be set in the
 environment (used by Stage 1's LLM link classification, Stage 2's
 extraction + verification, Stage 4's classification, and the news
 check's interpretation step — Stage 4 reuses the same Anthropic
 client already created for Stages 1/2, no separate key needed).
 NEWSAPI_KEY is also required for the news check — if unset, that
 stage is skipped gracefully rather than failing the whole pipeline.
================================================================
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
import openai

from data_collection import run_single_scrape
from extract_claims import process_scrape_result, write_excel
from cert_verifier_api import run_certification_stage, append_certifications_sheet, merge_certifications_into_claims
from ecgt_pipeline_stage4 import run_ecgt_classification_stage
from news_verifier import check_news_controversy, append_news_sheet


def run_pipeline(url: str, company_name: str = None, verbose: bool = True, cert_verifier=None):
    """
    Runs Stage 1 (scrape) -> Stage 2 (claim extraction + GPT verification)
    -> Stage 3 (certification check) -> Stage 4 (ECGT classification)
    -> news controversy check for one company URL, and saves a
    single-company Excel output with "Certifications" and "News Check"
    sheets (ECGT fields are merged directly into the "All Claims" sheet,
    same pattern as the certification fields).

    Args:
        url: company website URL
        company_name: optional display name (derived from domain if omitted)
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

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 1 — SCRAPING\n{'#'*60}")
    scrape_result, json_path = run_single_scrape(url, company_name, verbose=verbose)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 2 — CLAIM EXTRACTION + GPT VERIFICATION\n{'#'*60}")
    source_label = Path(json_path).name if json_path else ""
    claims_result = process_scrape_result(
        anthropic_client, openai_client, scrape_result, source_file=source_label, verbose=verbose
    )

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 3 — CERTIFICATION CHECK\n{'#'*60}")
    cert_result = run_certification_stage(
        scrape_result["company_name"], company_url=url, verifier=cert_verifier, verbose=verbose
    )
    merge_certifications_into_claims(claims_result, cert_result)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 4 — ECGT CLASSIFICATION\n{'#'*60}")
    ecgt_result = run_ecgt_classification_stage(claims_result, anthropic_client, verbose=verbose)

    if verbose:
        print(f"\n{'#'*60}\n# NEWS CONTROVERSY CHECK\n{'#'*60}")
    news_result = check_news_controversy(scrape_result["company_name"], anthropic_client, verbose=verbose)

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
        description="Run the GreenLens Stage 1 -> Stage 2 -> Stage 3 -> News Check pipeline for a single company."
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