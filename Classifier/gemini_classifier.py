"""
================================================================
 GREENLENS — GEMINI 2.5 FLASH-LITE ECGT CLASSIFIER
================================================================
 Model  : gemini-2.5-flash-lite  ($0.10 / $0.40 per 1M tokens)
 Cost for 248 claims: ~$0.05 total.

 Fix vs previous version:
   gemini-2.5-flash-lite has "thinking" mode on by default.
   This causes response.text to return None when the response
   contains multiple parts (thinking block + answer block).
   Fix: disable thinking via thinking_budget=0, which also
   halves the token cost and speeds up the response.
   Text is extracted safely from candidates[0] rather than
   using the .text shortcut that fails with thinking on.

 Input  : predictions.csv
 Output : predictions_gemini.csv  (adds prediction_gemini column)

 Run:
   export GEMINI_API_KEY="your-key-here"
   python gemini_classifier.py
================================================================
"""

import os
import time
import pandas as pd
from google import genai
from google.genai import types

from claude_classifier import (
    ECGT_RULES,
    FEW_SHOT_EXAMPLES,
    VERIFIED_FIELDS,
    build_registry_summary,
    build_fewshot_block,
)

MODEL      = "gemini-2.5-flash"
INPUT_CSV  = "predictions.csv"
OUTPUT_CSV = "predictions_gemini.csv"
LABELS     = ["RED", "AMBER", "GREEN"]
DELAY      = 0.5

_FEWSHOT_BLOCK = build_fewshot_block()

# Disable thinking — keeps responses fast, cheap, and in a single
# text part so extraction is reliable.
_CONFIG = types.GenerateContentConfig(
    thinking_config=types.ThinkingConfig(thinking_budget=0),
    max_output_tokens=10,
    temperature=0,
)


def extract_text(response) -> str:
    """
    Safe text extraction that works whether thinking is on or off.
    Walks candidate parts and returns the last non-empty text part,
    which is always the actual answer regardless of response structure.
    """
    try:
        text = ""
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                text = part.text  # keep overwriting — last text part is the answer
        return text.strip().upper()
    except Exception:
        return ""


def classify_one_gemini(client, claim_text: str,
                         registry_summary: str, category: str) -> str:
    prompt = f"""{ECGT_RULES}

{_FEWSHOT_BLOCK}

Now classify this new claim. Respond with ONLY one word:
RED, AMBER, or GREEN. No explanation, no punctuation.

Claim: "{claim_text}"
Category: {category}
Registry evidence: {registry_summary}

Your answer (one word):"""

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=_CONFIG,
        )
        answer = extract_text(response)
        for label in LABELS:
            if label in answer:
                return label
        print(f"    [Unexpected response] '{answer}'")
        return "UNKNOWN"
    except Exception as e:
        print(f"    [Gemini error] {e}")
        return "ERROR"


def run():
    df = pd.read_csv(INPUT_CSV)
    print(f"Loaded {len(df)} claims from {INPUT_CSV}\n")

    for field in VERIFIED_FIELDS:
        if field not in df.columns:
            df[field] = False
        else:
            df[field] = df[field].map(
                {True: True, False: False, "True": True, "False": False}
            ).fillna(False)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print('ERROR: export GEMINI_API_KEY="your-key-here"')
        return
    client = genai.Client(api_key=api_key)

    predictions = []
    total = len(df)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        claim    = row.to_dict()
        registry = build_registry_summary(claim)
        category = str(claim.get("category") or "general")
        pred     = classify_one_gemini(
            client, str(claim.get("text", "")), registry, category
        )
        predictions.append(pred)

        if i % 10 == 0 or i == total:
            print(f"  Classified {i}/{total}  (latest: {pred})")

        time.sleep(DELAY)

    df["prediction_gemini"] = predictions
    df.to_csv(OUTPUT_CSV, index=False)

    errors = sum(1 for p in predictions if p not in LABELS)
    print(f"\nSaved {OUTPUT_CSV}")
    print(f"Gemini distribution: {dict(pd.Series(predictions).value_counts())}")
    if errors:
        print(f"WARNING: {errors} ERROR/UNKNOWN predictions")
    print("\nNext: python evaluate_f1.py && python evaluate_kappa_v2.py")


if __name__ == "__main__":
    run()