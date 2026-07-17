"""
================================================================
 GREENLENS — STAGE 4: ECGT CLASSIFICATION (label + explanation)
================================================================
 Two-step design, intentionally kept separate:

   STEP A — THE DECISION
     Calls classify_one() from claude_classifier.py completely
     unmodified: same ECGT_RULES, same few-shot block, same
     max_tokens=10, same forced one-word output. This is the exact
     function validated against validation_test_set_real.csv
     (macro-F1 ~0.61, kappa ~0.41). Nothing about the decision
     procedure changes here — that's the whole point.

   STEP B — THE EXPLANATION
     A second, separate API call that receives the claim AND the
     label Step A already produced, and is explicitly told the label
     is final. Its only job is to say which rule applies and why,
     and to flag genuinely borderline cases for human review. It
     cannot change the label — there is no code path where its
     output overrides Step A's.

 Why split like this: adding reasoning space to a single combined
 call can change what a model decides, not just how it explains the
 decision (well-documented for chain-of-thought prompting). Splitting
 into two calls means the label your dissertation validated is
 provably the same label that ships in production — the explanation
 layer is auxiliary documentation ABOUT a locked decision, not a
 second, undocumented decision-making path.

 Input  : all_claims_labeled.csv (or any file load_and_prepare()
          from claude_classifier.py can read — post cert-verification,
          i.e. after Stage 3) — for STANDALONE / batch reprocessing use.

 For live pipeline use (pipelineV1.py), import
 run_ecgt_classification_stage() instead of running this file
 directly — it operates on a single company's already-in-memory
 claims_result dict, matching the calling convention every other
 stage (run_certification_stage, check_news_controversy, etc.) uses.

 Output : ecgt_classified.csv (standalone mode) — original columns +
          four new ones matching the production schema:
            ECGT Label, ECGT Rule Triggered, ECGT Explanation,
            ECGT Review Flag

 NOTE ON COLUMN NAMING: write_excel() in extract_claims.py uses a FIXED
 header list and a positional values list keyed by snake_case dict
 keys (e.g. c.get("emas_verified")) — it does NOT dynamically render
 whatever keys exist on a claim dict. This stage writes
 "ecgt_label" / "ecgt_rule_triggered" / "ecgt_explanation" /
 "ecgt_review_flag" (snake_case) onto each claim dict to match that
 convention. extract_claims.py has been updated correspondingly to
 add "ECGT Label", "ECGT Rule Triggered", "ECGT Explanation",
 "ECGT Review Flag" to its header/value lists — both files need to
 stay in sync if either changes.

 Run (standalone):
   export ANTHROPIC_API_KEY="your-key-here"
   python ecgt_pipeline_stage4.py
================================================================
"""

import os
import json
import time
import pandas as pd
import json_repair
from anthropic import Anthropic

# Import Step A completely unmodified — this is the validated procedure.
from claude_classifier_tool import (
    ECGT_RULES,
    classify_one,
    build_registry_summary,
    load_and_prepare,
)

MODEL_EXPLAIN = "claude-haiku-4-5"   # same model family as Step A
INPUT_CSV     = "all_claims_labeled.csv"
OUTPUT_CSV    = "ecgt_classified.csv"
DELAY         = 0.3
EXPLAIN_MAX_TOKENS = 300
STEP_A_RETRIES = 3   # thin retry wrapper around classify_one — does not
                      # touch classify_one's internals, so the decision
                      # procedure itself stays byte-identical; only
                      # transient API failures get retried.


def classify_one_with_retry(client: Anthropic, claim_text: str,
                             registry_summary: str, category: str) -> str:
    """Wraps classify_one() with retries on transient failures only.
    Does not modify classify_one() itself — same prompt, same
    max_tokens, same output parsing as the validated version."""
    for attempt in range(STEP_A_RETRIES):
        result = classify_one(client, claim_text, registry_summary, category)
        if result != "ERROR":
            return result
        wait = 2.0 * (2 ** attempt)
        print(f"    [Step A retry] attempt {attempt + 1} failed, waiting {wait:.1f}s...")
        time.sleep(wait)
    return "ERROR"


def explain_claim(client: Anthropic, claim_text: str, registry_summary: str,
                   category: str, label: str) -> dict:
    """STEP B — documents a label that has already been decided.
    This call is not permitted to change the label; it only explains
    it and flags whether a human should double-check it."""
    prompt = f"""{ECGT_RULES}

You are documenting a compliance classification decision that has
ALREADY been made and MUST NOT be changed. Your task is only to
explain why this label was assigned per the rules above — you are
NOT being asked to classify this claim; the classification is final.

Claim: "{claim_text}"
Category: {category}
Registry evidence: {registry_summary}

DECIDED LABEL: {label}

Identify which specific part of the rules above most directly
applies (a STEP 2 GREEN/AMBER/RED criterion, or a named BOUNDARY
rule), write a concise 1-3 sentence explanation a compliance
reviewer could read to understand the reasoning, and indicate
whether this is a genuinely borderline case a human should
double-check — for example: a named certification outside the six
tracked registries, an out-of-registry-scope partnership/sponsorship
claim, an ambiguous specificity call, or any case where reasonable
disagreement is plausible.

Return ONLY valid JSON, no markdown fences, no other text, in this
exact format:
{{
  "rule_triggered": "short reference, e.g. 'RED - bare tagline, no behaviour named' or 'AMBER - specific but no registry confirmed'",
  "explanation": "1-3 sentence explanation of why this label applies",
  "review_flag": true or false
}}"""

    try:
        response = client.messages.create(
            model=MODEL_EXPLAIN,
            max_tokens=EXPLAIN_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as parse_err:
            result = json_repair.loads(raw)
            print(f"    [Step B] strict JSON parse failed ({parse_err}) — "
                  f"recovered via json_repair")
        return {
            "rule_triggered": str(result.get("rule_triggered", "")),
            "explanation": str(result.get("explanation", "")),
            "review_flag": bool(result.get("review_flag", False)),
        }
    except Exception as e:
        print(f"    [Step B error] {e}")
        try:
            print(f"    [Step B raw response] {raw!r}")
        except NameError:
            pass  # failed before `raw` was even assigned (e.g. API call itself failed)
        # Explanation failing does NOT touch the label — it's already
        # locked in from Step A. We just record that documentation
        # failed, and flag for review since we have no explanation to
        # show a reviewer.
        return {
            "rule_triggered": "ERROR",
            "explanation": f"Explanation generation failed: {e}",
            "review_flag": True,
        }


def run_ecgt_classification_stage(claims_result: dict, anthropic_client: Anthropic,
                                   verbose: bool = True) -> dict:
    """
    Stage 4 entry point for pipelineV1.py. Runs AFTER Stage 3
    (merge_certifications_into_claims must already have populated the
    six *_verified fields on each claim in claims_result["claims"]).

    Mutates claims_result["claims"] in place, adding four fields to
    each claim dict: "ECGT Label", "ECGT Rule Triggered",
    "ECGT Explanation", "ECGT Review Flag" — see module docstring for
    the caveat on whether these column names survive write_excel().

    Args:
        claims_result: the same dict produced by process_scrape_result()
                        and already updated by merge_certifications_into_claims()
        anthropic_client: an anthropic.Anthropic client instance
                           (reuse the one already created in pipelineV1.py —
                           no separate API key handling needed here)
        verbose: whether to print progress to stdout

    Returns:
        dict summary: {"label_distribution": {...}, "review_flagged": int,
                        "total_claims": int}
    """
    claims = claims_result.get("claims", [])
    total = len(claims)
    label_counts = {"RED": 0, "AMBER": 0, "GREEN": 0, "ERROR": 0, "UNKNOWN": 0}
    review_flagged = 0

    for i, claim in enumerate(claims, start=1):
        registry = build_registry_summary(claim)
        category = str(claim.get("category") or "general")
        text = str(claim.get("text", ""))

        # STEP A — the decision, unchanged, validated procedure
        label = classify_one_with_retry(anthropic_client, text, registry, category)
        time.sleep(DELAY)

        # STEP B — explanation only, cannot revise the label above
        if label in ("RED", "AMBER", "GREEN"):
            info = explain_claim(anthropic_client, text, registry, category, label)
        else:
            info = {"rule_triggered": "N/A", "explanation":
                     "Step A classification failed or returned UNKNOWN.",
                     "review_flag": True}
        time.sleep(DELAY)

        claim["ecgt_label"] = label
        claim["ecgt_rule_triggered"] = info["rule_triggered"]
        claim["ecgt_explanation"] = info["explanation"]
        claim["ecgt_review_flag"] = info["review_flag"]

        label_counts[label] = label_counts.get(label, 0) + 1
        if info["review_flag"]:
            review_flagged += 1

        if verbose and (i % 10 == 0 or i == total):
            print(f"  Classified {i}/{total}  (latest: {label})")

    if verbose:
        print(f"  ECGT distribution: {label_counts}")
        print(f"  Flagged for review: {review_flagged}/{total}")

    return {
        "label_distribution": label_counts,
        "review_flagged": review_flagged,
        "total_claims": total,
    }


def run():
    df = load_and_prepare(INPUT_CSV)
    print(f"Loaded {len(df)} claims from {INPUT_CSV}\n")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print('ERROR: export ANTHROPIC_API_KEY="your-key-here"')
        return
    client = Anthropic(api_key=api_key)

    labels, rules_triggered, explanations, review_flags = [], [], [], []
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        claim = row.to_dict()
        registry = build_registry_summary(claim)
        category = str(claim.get("category") or "general")
        text = str(claim.get("text", ""))

        # STEP A — the decision, unchanged, validated procedure
        label = classify_one_with_retry(client, text, registry, category)
        time.sleep(DELAY)

        # STEP B — explanation only, cannot revise the label above
        if label in ("RED", "AMBER", "GREEN"):
            info = explain_claim(client, text, registry, category, label)
        else:
            # Step A itself failed (ERROR/UNKNOWN) — nothing for Step B
            # to document; flag for review directly.
            info = {"rule_triggered": "N/A", "explanation":
                     "Step A classification failed or returned UNKNOWN.",
                     "review_flag": True}
        time.sleep(DELAY)

        labels.append(label)
        rules_triggered.append(info["rule_triggered"])
        explanations.append(info["explanation"])
        review_flags.append(info["review_flag"])

        if i % 10 == 0 or i == total:
            print(f"  Processed {i}/{total}  (latest: {label})")

    df["ECGT Label"] = labels
    df["ECGT Rule Triggered"] = rules_triggered
    df["ECGT Explanation"] = explanations
    df["ECGT Review Flag"] = review_flags

    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\nSaved {OUTPUT_CSV}")
    print(f"Label distribution: {dict(pd.Series(labels).value_counts())}")
    print(f"Flagged for review: {sum(review_flags)}/{total}")


if __name__ == "__main__":
    run()