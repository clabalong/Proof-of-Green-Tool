"""
================================================================
 GREENLENS — BATCH RUNNER
================================================================
 Runs pipelineV1.run_pipeline() across the full SME sample, one
 company at a time, reusing a SINGLE CertVerifier instance so
 EMAS/EU Organic country data is fetched once for the whole batch
 instead of once per company (that data doesn't change between
 companies, so re-fetching it 20 times would be pure waste).

 Each company is wrapped in its own try/except, so one failure
 (e.g. a 403 from anti-bot protection, a network timeout) doesn't
 take down the rest of the batch — it's logged and the run moves on.

 Usage:
     python batch_run.py

 To run a different set of companies, edit SME_SAMPLE below.
================================================================
"""

import time
from cert_verifier_api import CertVerifier, DEFAULT_COUNTRIES
from pipelineV1 import run_pipeline

# (url, company_name) — names match your original SME_SAMPLE list
# exactly, not auto-derived from the domain (avoids the "Thehappypear"
# style derivation bug for multi-word brand names).
SME_SAMPLE = [
    ("https://thehappypear.ie", "The Happy Pear"),
    ("https://keoghs.ie", "Keoghs Farm"),
    ("https://nieuwemelkboer.nl/", "De Nieuwe Melkboer"),
    ("https://www.irishseaspray.com/", "Irish Seaspray"),
    ("https://www.moyeecoffee.com/", "Moyee Coffee"),
    ("https://www.macroombuffalocheese.com/", "Macroom Buffalo"),
    ("https://www.lebensbaum.com/", "Lebensbaum"),
    ("https://galmere.ie/", "Galmere Fresh Foods"),
    ("https://ballymaloefoods.ie/", "Ballymaloe Foods"),
    ("https://vithit.ie/", "VITHIT"),
    ("https://bread41.ie/", "Bread 41"),
    ("https://beanandgoose.ie/", "Bean and Goose"),
    ("https://www.eatfiid.com/", "fiid"),
    ("https://www.trudieskitchen.com/", "Trudies Kitchen"),
    ("https://glenisk.com", "Glenisk"),
    ("https://danival.fr", "Danival"),
    ("https://flahavans.com", "Flahavan's"),
    ("https://belvas.be", "Belvas"),
    ("https://biologon.at", "Biologon GmbH"),
    ("https://belledonne.bio", "Belledonne"),
]


def run_batch(sample=None, delay_seconds: float = 2.0):
    """
    Runs the full pipeline for every (url, company_name) pair in sample,
    reusing one CertVerifier across all of them.

    Args:
        sample: list of (url, company_name) tuples. Defaults to SME_SAMPLE.
        delay_seconds: pause between companies, to avoid hammering the
                        various external servers back-to-back.

    Returns:
        list of dicts, one per company: {"company_name", "url", "status",
        "excel_path" or "error"}
    """
    sample = sample or SME_SAMPLE
    shared_verifier = CertVerifier(countries=DEFAULT_COUNTRIES)

    results = []
    print(f"\n{'#'*60}")
    print(f"# BATCH RUN — {len(sample)} companies")
    print(f"{'#'*60}")

    for i, (url, company_name) in enumerate(sample, start=1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(sample)}] {company_name}")
        print(f"{'='*60}")

        try:
            claims_result, cert_result, news_result, excel_path = run_pipeline(
                url, company_name, verbose=True, cert_verifier=shared_verifier
            )
            results.append({
                "company_name": company_name,
                "url": url,
                "status": "success",
                "claims_count": len(claims_result["claims"]),
                "excel_path": str(excel_path),
            })
        except Exception as e:
            print(f"  [ERROR] {company_name} failed: {type(e).__name__} — {e}")
            results.append({
                "company_name": company_name,
                "url": url,
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
            })

        if i < len(sample):
            time.sleep(delay_seconds)

    # ── Summary ──
    print(f"\n{'#'*60}")
    print(f"# BATCH COMPLETE")
    print(f"{'#'*60}")
    succeeded = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] == "failed"]
    print(f"  Succeeded: {len(succeeded)}/{len(sample)}")
    print(f"  Failed   : {len(failed)}/{len(sample)}")
    if failed:
        print(f"\n  Failed companies:")
        for r in failed:
            print(f"    - {r['company_name']}: {r['error']}")
    print()

    return results


if __name__ == "__main__":
    run_batch()
