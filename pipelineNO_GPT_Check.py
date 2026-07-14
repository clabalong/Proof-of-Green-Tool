"""
================================================================
 GREENLENS — PIPELINE ORCHESTRATOR, NO-GPT VARIANT
 (Stage 1 -> Stage 2 [confidence threshold] -> Stage 3 -> News Check)
================================================================
 Thin glue script. Does not duplicate any scraping, extraction,
 certification, or news-check logic — it just imports the reusable
 functions from each stage and chains them together for a single
 company URL.

 Difference from the main pipeline (pipelineV1.py): Stage 2 does NOT
 make a second (OpenAI/GPT) call to independently verify each claim.
 Instead, extract_claimsNO_GPT_Check's process_scrape_result() flags a
 claim "confidence_verified" if its own extraction confidence score is
 >= CONFIDENCE_THRESHOLD (0.8 by default). No OPENAI_API_KEY is needed.
 Everything else — Stage 1, Stage 3, the news check — is identical to
 the main pipeline.

 CLI usage:
     python pipelineNO_GPT_Check.py https://glenisk.com "Glenisk"
     python pipelineNO_GPT_Check.py https://glenisk.com "Glenisk" --threshold 0.7

 Programmatic usage:
     from pipelineNO_GPT_Check import run_pipeline
     claims_result, cert_result, news_result, excel_path = run_pipeline(url, company_name)

 Requires ANTHROPIC_API_KEY to be set in the environment (used by both
 Stage 1's LLM link classification and Stage 2's extraction). NEWSAPI_KEY
 is also required for the news check — if unset, that stage is skipped
 gracefully rather than failing the whole pipeline.
================================================================
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from data_collection import run_single_scrape
from extract_claimsNO_GPT_Check import process_scrape_result, write_excel, CONFIDENCE_THRESHOLD
from cert_verifier_api import run_certification_stage, append_certifications_sheet, merge_certifications_into_claims
from news_verifier import check_news_controversy, append_news_sheet


def run_pipeline(url: str, company_name: str = None, verbose: bool = True, cert_verifier=None,
                  confidence_threshold: float = CONFIDENCE_THRESHOLD):
    """
    Runs Stage 1 (scrape) -> Stage 2 (claim extraction + confidence-threshold
    verification) -> Stage 3 (certification check) -> news controversy check
    for one company URL, and saves a single-company Excel output with
    "Certifications" and "News Check" sheets.

    Args:
        url: company website URL
        company_name: optional display name (derived from domain if omitted)
        verbose: whether to print progress to stdout
        cert_verifier: an existing CertVerifier instance to reuse across
                        multiple calls (recommended for batch runs, so
                        EMAS/EU Organic country data is fetched once, not
                        once per company). A new one is created if not given.
        confidence_threshold: claims with extraction confidence >= this
                               are marked verified (default 0.8)

    Returns:
        (claims_result, cert_result, news_result, excel_path) tuple
    """
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("[ERROR] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    anthropic_client = anthropic.Anthropic(api_key=anthropic_key)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 1 — SCRAPING\n{'#'*60}")
    scrape_result, json_path = run_single_scrape(url, company_name, verbose=verbose)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 2 — CLAIM EXTRACTION + CONFIDENCE THRESHOLD ({confidence_threshold})\n{'#'*60}")
    source_label = Path(json_path).name if json_path else ""
    claims_result = process_scrape_result(
        anthropic_client, scrape_result, source_file=source_label,
        confidence_threshold=confidence_threshold, verbose=verbose
    )

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 3 — CERTIFICATION CHECK\n{'#'*60}")
    cert_result = run_certification_stage(
        scrape_result["company_name"], company_url=url, verifier=cert_verifier, verbose=verbose
    )
    merge_certifications_into_claims(claims_result, cert_result)

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
        print(f"  News controversy : {'YES — see News Check sheet' if news_result.get('controversy_detected') else 'none detected'}")
        print(f"  Scraped JSON     : {json_path}")
        print(f"  Excel output     : {excel_path}")
        print(f"{'='*60}\n")

    return claims_result, cert_result, news_result, excel_path


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Run the GreenLens Stage 1 -> Stage 2 (confidence threshold) -> Stage 3 -> "
                    "News Check pipeline for a single company (no-GPT variant)."
    )
    parser.add_argument("url", help="Base URL of the SME website, e.g. https://glenisk.com")
    parser.add_argument(
        "company_name",
        nargs="?",
        default=None,
        help="Optional company display name (derived from domain if omitted)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=CONFIDENCE_THRESHOLD,
        help=f"Confidence threshold for claim verification (default: {CONFIDENCE_THRESHOLD})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_pipeline(args.url, args.company_name, confidence_threshold=args.threshold)