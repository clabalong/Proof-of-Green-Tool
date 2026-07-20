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
          SIX new ones matching the production schema:
            ECGT Label, ECGT Rule Triggered, ECGT Citation,
            ECGT Explanation, ECGT Guidance, ECGT Review Flag

 NOTE ON COLUMN NAMING: write_excel() in extract_claims.py uses a FIXED
 header list and a positional values list keyed by snake_case dict
 keys (e.g. c.get("emas_verified")) — it does NOT dynamically render
 whatever keys exist on a claim dict. This stage writes
 "ecgt_label" / "ecgt_rule_triggered" / "ecgt_citation" /
 "ecgt_explanation" / "ecgt_guidance" / "ecgt_review_flag" (snake_case)
 onto each claim dict to match that convention. extract_claims.py MUST
 be updated to add the two new columns ("ECGT Citation", "ECGT
 Guidance") to its header/value lists, or they will silently not
 appear in the Excel output — both files need to stay in sync.

 Run (standalone):
   export ANTHROPIC_API_KEY="your-key-here"
   python ecgt_pipeline_stage4.py
================================================================
"""

import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
EXPLAIN_MAX_TOKENS = 350   # bumped from 300 — citation + guidance add length
STEP_A_RETRIES = 3   # thin retry wrapper around classify_one — does not
                      # touch classify_one's internals, so the decision
                      # procedure itself stays byte-identical; only
                      # transient API failures get retried.

# How many claims to classify concurrently in run_ecgt_classification_stage()
# (the live-pipeline path). Each claim's Step A + Step B sequence is fully
# independent of every other claim's, so this is safe — it does not touch
# classify_one()/explain_claim()'s internals, just runs more of the same
# unchanged per-claim procedure at once. Tune down if you see 429 rate-limit
# errors; bounded concurrency is a better throttle than the fixed per-call
# sleeps below, which the standalone batch mode (run()) still uses.
CLASSIFICATION_MAX_WORKERS = 6

# Citation reference table — the 8 rules from EU_Directive_2024_825_Rulings.json
# that are actually in scope for food-SME environmental claims (the other 10,
# ECGT_006-012 and ECGT_016-018, cover durability/software/reparability for
# goods with digital elements and don't apply here — see the scoping decision
# made earlier in this project). Given to Step B directly so it cites REAL
# rule IDs and Annex/Article references from the Directive, not invented ones.
ECGT_CITATION_TABLE = """
ECGT_001 | Annex I, point 2a   | Uncertified Sustainability Label | RED
ECGT_002 | Annex I, point 4a   | Generic Environmental Claim Without Evidence | RED
ECGT_003 | Annex I, point 4b   | Overstated Scope of Environmental Claim | RED
ECGT_004 | Annex I, point 4c   | Carbon Offset-Based Neutrality Claim | RED
ECGT_005 | Annex I, point 10a  | Presenting Legal Requirements as Distinctive Features | RED
ECGT_013 | Article 6(2)(d)     | Unsubstantiated Future Environmental Performance Claim | RED
ECGT_014 | Article 6(2)(e)     | Advertising Irrelevant Consumer Benefits | AMBER
ECGT_015 | Article 7(7)        | Incomplete Environmental/Social Comparison Information | AMBER
"""


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


def extract_json_object(raw: str) -> tuple[str, str]:
    """Finds the first balanced {...} object in raw text and returns
    (json_substring, discarded_trailing_text). Handles nested braces
    and braces inside quoted strings correctly, so a '}' inside an
    explanation string doesn't prematurely end the match.

    This is the root-cause fix for 'Extra data' JSON errors: rather
    than parsing the whole response and hoping nothing follows the
    JSON, we only ever hand the parser the exact object substring."""
    start = raw.find("{")
    if start == -1:
        return raw, ""

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1], raw[i + 1:].strip()
    return raw[start:], ""  # unbalanced — return what we have, nothing discarded


def explain_claim(client: Anthropic, claim_text: str, registry_summary: str,
                   category: str, label: str) -> dict:
    """STEP B — documents a label that has already been decided.
    This call is not permitted to change the label; it only explains
    it, cites the specific Directive rule, gives actionable guidance,
    and flags whether a human should double-check it."""
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

Also cite the specific EU Directive (EU) 2024/825 rule this claim
falls under, from this reference table (the only 8 rules relevant to
food-SME environmental claims — pick the single best match, or
"None — general principle, no single rule dominant" if genuinely
none fit well):

{ECGT_CITATION_TABLE}

Finally, give ONE actionable guidance category for what the company
should do about this specific claim:
  - "REMOVE" — claim is a bare tagline/label with no salvageable
    environmental content; it should be deleted, not fixed with more
    evidence (typically RED, ECGT_001/002-style cases).
  - "REWRITE" — claim's problem is how it's framed, not a lack of
    evidence (overstated scope, offset-based neutrality stated as if
    achieved, a legal minimum presented as a differentiator); it
    needs rephrasing to be accurate.
  - "SUBSTANTIATE" — claim's core content is fine but lacks
    evidence, certification, or disclosed methodology/baseline; it
    needs supporting proof, not different wording (typically AMBER
    cases).
  - "NONE" — claim is compliant as-is (typically GREEN), no action
    needed.

Return ONLY valid JSON, no markdown fences, no other text, in this
exact format:
{{
  "rule_triggered": "short reference, e.g. 'RED - bare tagline, no behaviour named' or 'AMBER - specific but no registry confirmed'",
  "citation": "e.g. 'ECGT_002 - Annex I, point 4a - Generic Environmental Claim Without Evidence' or the none-fits string above",
  "explanation": "1-3 sentence explanation of why this label applies",
  "guidance": "REMOVE, REWRITE, SUBSTANTIATE, or NONE",
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
        json_str, discarded = extract_json_object(raw)
        if discarded:
            print(f"    [Step B] model appended extra text after the JSON "
                  f"(discarded, label unaffected): {discarded[:200]!r}")
        try:
            result = json.loads(json_str)
        except json.JSONDecodeError as parse_err:
            result = json_repair.loads(json_str)
            print(f"    [Step B] strict JSON parse failed ({parse_err}) — "
                  f"recovered via json_repair")
        return {
            "rule_triggered": str(result.get("rule_triggered", "")),
            "citation": str(result.get("citation", "")),
            "explanation": str(result.get("explanation", "")),
            "guidance": str(result.get("guidance", "")).upper() or "NONE",
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
            "citation": "ERROR",
            "explanation": f"Explanation generation failed: {e}",
            "guidance": "ERROR",
            "review_flag": True,
        }


def _classify_and_explain_one(anthropic_client: Anthropic, claim: dict) -> tuple[dict, str, dict]:
    """
    Runs the EXACT same Step A -> Step B sequence as before for one
    claim, unchanged. This is the unit of work parallelized across
    claims in run_ecgt_classification_stage() — nothing about the
    classification procedure itself differs from the original
    sequential version; only the number of claims in flight at once
    changes.
    """
    registry = build_registry_summary(claim)
    category = str(claim.get("category") or "general")
    text = str(claim.get("text", ""))

    # STEP A — the decision, unchanged, validated procedure
    label = classify_one_with_retry(anthropic_client, text, registry, category)

    # STEP B — explanation only, cannot revise the label above
    if label in ("RED", "AMBER", "GREEN"):
        info = explain_claim(anthropic_client, text, registry, category, label)
    else:
        info = {"rule_triggered": "N/A", "citation": "N/A", "explanation":
                 "Step A classification failed or returned UNKNOWN.",
                 "guidance": "N/A", "review_flag": True}

    return claim, label, info


def run_ecgt_classification_stage(claims_result: dict, anthropic_client: Anthropic,
                                   verbose: bool = True,
                                   max_workers: int = CLASSIFICATION_MAX_WORKERS) -> dict:
    """
    Stage 4 entry point for pipelineV1.py. Runs AFTER Stage 3
    (merge_certifications_into_claims must already have populated the
    six *_verified fields on each claim in claims_result["claims"]).

    Classifies claims CONCURRENTLY (up to max_workers at once) rather
    than one at a time — each claim's Step A + Step B sequence is
    completely independent of every other claim's, so this changes
    only the scheduling, not the classification procedure itself.

    Mutates claims_result["claims"] in place, adding six fields to
    each claim dict: "ecgt_label", "ecgt_rule_triggered",
    "ecgt_citation", "ecgt_explanation", "ecgt_guidance",
    "ecgt_review_flag" — see module docstring for the extract_claims.py
    sync requirement.

    Args:
        claims_result: the same dict produced by process_scrape_result()
                        and already updated by merge_certifications_into_claims()
        anthropic_client: an anthropic.Anthropic client instance
                           (reuse the one already created in pipelineV1.py —
                           no separate API key handling needed here; the
                           Anthropic SDK's client is safe to share across
                           threads for concurrent requests)
        verbose: whether to print progress to stdout
        max_workers: how many claims to classify concurrently (default
                      CLASSIFICATION_MAX_WORKERS). Reduce if you see
                      429 rate-limit errors.

    Returns:
        dict summary: {"label_distribution": {...}, "review_flagged": int,
                        "total_claims": int}
    """
    claims = claims_result.get("claims", [])
    total = len(claims)
    label_counts = {"RED": 0, "AMBER": 0, "GREEN": 0, "ERROR": 0, "UNKNOWN": 0}
    review_flagged = 0
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_classify_and_explain_one, anthropic_client, claim)
            for claim in claims
        ]
        # as_completed yields whichever finishes first — claims are NOT
        # necessarily processed/reported in their original order, same
        # tradeoff already accepted elsewhere in this pipeline
        # (pipelineV1.py's Stage 3/news-check progress reporting).
        for future in as_completed(futures):
            claim, label, info = future.result()

            claim["ecgt_label"] = label
            claim["ecgt_rule_triggered"] = info["rule_triggered"]
            claim["ecgt_citation"] = info["citation"]
            claim["ecgt_explanation"] = info["explanation"]
            claim["ecgt_guidance"] = info["guidance"]
            claim["ecgt_review_flag"] = info["review_flag"]

            label_counts[label] = label_counts.get(label, 0) + 1
            if info["review_flag"]:
                review_flagged += 1

            completed += 1
            if verbose and (completed % 10 == 0 or completed == total):
                print(f"  Classified {completed}/{total}  (latest: {label})")

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

    labels, rules_triggered, citations, explanations, guidances, review_flags = [], [], [], [], [], []
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
            info = {"rule_triggered": "N/A", "citation": "N/A", "explanation":
                     "Step A classification failed or returned UNKNOWN.",
                     "guidance": "N/A", "review_flag": True}
        time.sleep(DELAY)

        labels.append(label)
        rules_triggered.append(info["rule_triggered"])
        citations.append(info["citation"])
        explanations.append(info["explanation"])
        guidances.append(info["guidance"])
        review_flags.append(info["review_flag"])

        if i % 10 == 0 or i == total:
            print(f"  Processed {i}/{total}  (latest: {label})")

    df["ECGT Label"] = labels
    df["ECGT Rule Triggered"] = rules_triggered
    df["ECGT Citation"] = citations
    df["ECGT Explanation"] = explanations
    df["ECGT Guidance"] = guidances
    df["ECGT Review Flag"] = review_flags

    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\nSaved {OUTPUT_CSV}")
    print(f"Label distribution: {dict(pd.Series(labels).value_counts())}")
    print(f"Flagged for review: {sum(review_flags)}/{total}")


if __name__ == "__main__":
    run()