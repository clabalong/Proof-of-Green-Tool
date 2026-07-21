"""
================================================================
 GREENLENS — PIPELINE ORCHESTRATOR (Stage 1 -> Stage 2)
================================================================
 Thin glue script. Does not duplicate any scraping or extraction
 logic — it just imports the reusable functions from each stage
 and chains them together for a single company URL, in memory.

 This is what the live tool's dashboard backend should call.

 CLI usage:
     python pipeline_Old.py https://glenisk.com "Glenisk"

 Programmatic usage:
     from pipeline import run_pipeline
     claims_result, excel_path = run_pipeline(url, company_name)

 Requires ANTHROPIC_API_KEY to be set in the environment (used by
 both Stage 1's LLM link classification and Stage 2's extraction).
================================================================
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from data_collection import run_single_scrape
from extract_claims import process_scrape_result, write_excel


def run_pipeline(url: str, company_name: str = None, verbose: bool = True):
    """
    Runs Stage 1 (scrape) then Stage 2 (claim extraction) for one
    company URL, and saves a single-company Excel output.

    Returns:
        (claims_result, excel_path) tuple
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ERROR] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 1 — SCRAPING\n{'#'*60}")
    scrape_result, json_path = run_single_scrape(url, company_name, verbose=verbose)

    if verbose:
        print(f"\n{'#'*60}\n# STAGE 2 — CLAIM EXTRACTION\n{'#'*60}")
    source_label = Path(json_path).name if json_path else ""
    claims_result = process_scrape_result(client, scrape_result, source_file=source_label, verbose=verbose)

    safe_name = scrape_result["company_name"].lower().replace(" ", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = Path(f"greenlens_claims_{safe_name}_{timestamp}.xlsx")
    write_excel([claims_result], excel_path)

    if verbose:
        print(f"\n{'='*60}")
        print(f"  PIPELINE COMPLETE — {scrape_result['company_name']}")
        print(f"  Claims extracted : {len(claims_result['claims'])}")
        print(f"  Scraped JSON     : {json_path}")
        print(f"  Excel output     : {excel_path}")
        print(f"{'='*60}\n")

    return claims_result, excel_path


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Run the GreenLens Stage 1 -> Stage 2 pipeline for a single company."
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
