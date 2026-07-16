"""
================================================================
 GREENLENS — CLAUDE SONNET 4.6 ECGT CLASSIFIER
================================================================
 Input  : claims_dataset_adapted.csv  (or validation_test_set_real.csv)
 Output : predictions.csv
           columns: all original + prediction (Claude's label)

 Run:
   export ANTHROPIC_API_KEY="your-key-here"
   python claude_classifier.py

 Then run:
   python groq_classifier.py        <- Llama 3.1 8B on same claims
   python evaluate_f1.py            <- F1 vs human labels
   python evaluate_kappa_v2.py      <- cross-model Kappa
================================================================
"""

import os
import time
import pandas as pd
from anthropic import Anthropic

# ── Configuration ────────────────────────────────────────────
MODEL     = "claude-sonnet-4-6"
INPUT_CSV = "validation_test_set_real.csv"
OUTPUT_CSV = "predictions.csv"
LABELS    = ["RED", "AMBER", "GREEN"]
DELAY     = 0.3

VERIFIED_FIELDS = [
    "emas_verified", "eu_organic_verified", "bcorp_verified",
    "bordbia_verified", "biopartenaire_verified", "bioed_verified",
]

REGISTRY_DISPLAY = {
    "emas_verified":          "EMAS",
    "eu_organic_verified":    "EU Organic",
    "bcorp_verified":         "B Corp",
    "bordbia_verified":       "Bord Bia (Origin Green)",
    "biopartenaire_verified": "BIOPARTENAIRE",
    "bioed_verified":         "BioED",
}


def build_registry_summary(claim: dict) -> str:
    confirmed = [REGISTRY_DISPLAY[f] for f in VERIFIED_FIELDS if claim.get(f) is True]
    not_confirmed = [REGISTRY_DISPLAY[f] for f in VERIFIED_FIELDS if claim.get(f) is False]
    if not confirmed and not not_confirmed:
        return "No registry information available."
    if not confirmed:
        return "No certification confirmed in any checked registry."
    line = f"Confirmed: {', '.join(confirmed)}."
    if not_confirmed:
        line += f" Not confirmed: {', '.join(not_confirmed)}."
    return line


# ── ECGT Rules ───────────────────────────────────────────────
ECGT_RULES = """
You are a regulatory compliance classifier for EU Directive 2024/825 (ECGT).
Classify environmental claims from SME websites as RED, AMBER, or GREEN.

═══════════════════════════════════════════════════════════
STEP 1 — READ THE REGISTRY EVIDENCE FIRST
═══════════════════════════════════════════════════════════

Check the registry evidence line before reading the claim.

  "Confirmed: <registry>" → company holds a CONFIRMED certification.
  "No certification confirmed" → no independent verification found.
  "No registry information available" → treat as unconfirmed.

═══════════════════════════════════════════════════════════
STEP 2 — CLASSIFY
═══════════════════════════════════════════════════════════

GREEN — COMPLIANT. Use when:
  • Claim references a specific certification AND registry confirms it.
  • Claim states a CONCRETE, VERIFIABLE FACT (named supplier, specific
    action already taken, measurable outcome, date, location) AND
    registry is confirmed for this company.
  • Claim describes participation in a named, verifiable public programme.
  • Factual company history or structure statements.

AMBER — NEEDS EVIDENCE. Use when:
  • Claim is SPECIFIC (a number, a named action, a concrete commitment)
    BUT no registry is confirmed for this company.
  • Claim is an ASPIRATION or FUTURE COMMITMENT with some specificity.
  • Claim uses vague language BUT company holds a CONFIRMED certification
    (certification is a credibility floor — even vague claims get AMBER
    when the company is independently verified, not RED).
  • Registry says certification exists but doesn't fully cover this claim.

RED — LIKELY NON-COMPLIANT. Use ONLY when:
  • Claim is VAGUE AND GENERIC — no specific action, metric, or detail —
    AND no registry is confirmed for this company.
  • Claim is a pure marketing SLOGAN or TAGLINE with NO environmental
    behaviour, action, or commitment named at all (e.g. "Sustainability",
    "BIO, ÉQUITABLE & RESPONSABLE", "Respect de l'environnement",
    "Natural Ingredients", "Tous nos produits bio"). These are RED
    REGARDLESS of registry status — a confirmed certification cannot give
    substance to a tagline that asserts nothing checkable. Strip out the
    buzzwords: if nothing behavioural remains, it is RED.
  • Claim makes a sweeping whole-company green assertion with no evidence.
  • Claim names a certification the company does NOT hold.

KEY DISTINCTION for certified companies:
  Pure tagline (no behaviour at all) → RED even if certified.
  Vague but substantive (some behaviour named, just unquantified)
    → AMBER if certified.

═══════════════════════════════════════════════════════════
BOUNDARY — RED vs AMBER
═══════════════════════════════════════════════════════════

  1. Any specific detail (number, named action, date, named supplier)?
       YES → AMBER at minimum, never RED.
       NO  → Continue to rule 2.

  2. Is ANY behaviour, action, or commitment named — even a vague one —
     beyond a bare buzzword, label, or tagline?
       YES and company is confirmed → AMBER (credibility floor applies).
       NO  (pure tagline/label, nothing behavioural) → RED even if confirmed.

  3. Hedged language ("we aim to", "working towards") around a specific
     initiative is AMBER, not RED.

  4. When genuinely uncertain between RED and AMBER → choose AMBER,
     UNLESS the claim passes the "strip the buzzwords" test and nothing
     behavioural remains — then choose RED.

═══════════════════════════════════════════════════════════
BOUNDARY — GREEN vs AMBER
═══════════════════════════════════════════════════════════

  1. Specific detail AND registry confirmed → GREEN ONLY IF the
     specific detail describes something the certification directly
     speaks to (the certification itself, a formally verified action,
     a measured output that would appear in an audit).
     Examples of GREEN: "Certified B Corporation", "As part of our
     Origin Green membership we set targets", "We reduced Scope 1
     emissions by 18% per our published 2024 sustainability report".

  2. General environmental practices — packaging choices, sourcing
     preferences, energy suppliers, product ingredients — are AMBER
     even from a confirmed company, because the certification does
     not independently verify those specific operational claims. The
     fact that a company is B-Corp certified does not mean every
     sentence on their website is third-party verified.
     Examples of AMBER even with confirmed registry: "We use
     compostable packaging where possible", "We source organic
     ingredients", "We power our sites with renewable energy",
     "We work with local farmers".

  3. When genuinely uncertain between GREEN and AMBER → choose AMBER.
     Do not default to GREEN just because the company is certified.

═══════════════════════════════════════════════════════════
LANGUAGE NOTE
═══════════════════════════════════════════════════════════
Claims may be in English, French, German, or Dutch.
Same rules apply regardless of language.
""".strip()

# ── Few-shot examples ─────────────────────────────────────────
# 21 real examples from the actual claims_dataset.csv (8 companies).
# Type A RED examples are real claims from certified companies —
# the single-word and short-phrase cases that are RED regardless
# of registry status because they name no environmental behaviour.
FEW_SHOT_EXAMPLES = [
    # ── Core RED ──
    {"text": "We are a sustainable company", "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED", "note": None},
    {"text": "Always sustainably produced.", "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED", "note": None},
    {"text": "eco-friendly products for you and the planet", "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED", "note": None},
    # ── Core AMBER ──
    {"text": "We aim to be carbon neutral by 2030", "category": "carbon",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER", "note": None},
    {"text": "Our packaging is 100% recyclable", "category": "packaging",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER", "note": None},
    {"text": "We are committed to reducing greenhouse gas emissions", "category": "carbon",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER", "note": None},
    # ── Core GREEN ──
    {"text": "We installed LED lighting across all sites in 2023", "category": "carbon",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN", "note": None},
    {"text": "As part of our Origin Green membership we have set targets",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN", "note": None},
    {"text": "Certified B Corporation", "category": "certification",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN", "note": None},
    # ── Boundary A: RED despite confirmed registry (real claims, real companies) ──
    {"text": "Sustainability",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED",
     "note": "BOUNDARY: RED despite Bord Bia confirmed — single word, no behaviour named"},
    {"text": "Sustainability Journey",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED",
     "note": "BOUNDARY: RED despite Bord Bia confirmed — phrase with no action"},
    {"text": "Natural Ingredients",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED",
     "note": "BOUNDARY: RED despite B Corp + Bord Bia — product label, no environmental behaviour"},
    {"text": "BIO, ÉQUITABLE & RESPONSABLE",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": True, "bioed_verified": True,
     "label": "RED",
     "note": "BOUNDARY: RED despite EU Organic + BIOPARTENAIRE + BioED — tagline, no behaviour"},
    {"text": "Respect de l'environnement",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": True, "bioed_verified": True,
     "label": "RED",
     "note": "BOUNDARY: RED despite BIOPARTENAIRE — generic phrase, no action"},
    {"text": "Tous nos produits bio",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": True, "bioed_verified": True,
     "label": "RED",
     "note": "BOUNDARY: RED despite EU Organic + BIOPARTENAIRE — bare product label"},
    # ── Boundary B: AMBER — specific claim, company IS certified ──
    {"text": "The company transitioned from virgin plastic to 100% recycled plastic for our Mayo bottles, replacing 428 kg of virgin plastic",
     "category": "packaging",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER",
     "note": "BOUNDARY: AMBER — specific numbers, company certified, but this claim isn't what the certification covers"},
    {"text": "In partnership with FoodCloud, we have helped redistribute 254,000 meals",
     "category": "waste",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER",
     "note": "BOUNDARY: AMBER — specific action, company certified, claim not directly covered by that cert"},
    # ── Boundary C/D: GREEN — specific + confirmed ──
    {"text": "sa labellisation B Corp* en 2019",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": True, "bioed_verified": True,
     "label": "GREEN",
     "note": "BOUNDARY: GREEN — specific date AND confirmed; do not downgrade to AMBER"},
    {"text": "The Happy Pear is officially a Certified B Corporation",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN",
     "note": "BOUNDARY: GREEN — confirmed cert directly named in claim"},
    {"text": "We've been active members of Bord Bia's sustainability initiative, Origin Green, since it began",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": True, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN",
     "note": "BOUNDARY: GREEN — programme membership named and confirmed"},
    {"text": "Cette année marque aussi le 1er produit Belledonne labellisé BIOPARTENAIRE",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": True, "bioed_verified": True,
     "label": "GREEN",
     "note": "BOUNDARY: GREEN — specific milestone AND confirmed; do not downgrade"},
]


def build_fewshot_block() -> str:
    lines = [
        "Here are labelled examples to guide your classification.",
        "Pay close attention to the BOUNDARY examples.\n",
    ]
    for ex in FEW_SHOT_EXAMPLES:
        registry = build_registry_summary(ex)
        if ex.get("note"):
            lines.append(f"[{ex['note']}]")
        lines.append(f'Claim: "{ex["text"]}"')
        lines.append(f"Category: {ex.get('category', 'general')}")
        lines.append(f"Registry evidence: {registry}")
        lines.append(f"Correct label: {ex['label']}")
        lines.append("")
    return "\n".join(lines)


_FEWSHOT_BLOCK = build_fewshot_block()


def classify_one(client: Anthropic, claim_text: str,
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
        response = client.messages.create(
            model=MODEL, max_tokens=10,
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.content[0].text.strip().upper()
        for label in LABELS:
            if label in answer:
                return label
        return "UNKNOWN"
    except Exception as e:
        print(f"    API error: {e}")
        return "ERROR"


def run():
    df = pd.read_csv(INPUT_CSV)
    print(f"Loaded {len(df)} claims from {INPUT_CSV}\n")

    # Normalise boolean columns
    for field in VERIFIED_FIELDS:
        if field not in df.columns:
            df[field] = False
        else:
            df[field] = df[field].map(
                {True: True, False: False, "True": True, "False": False}
            ).fillna(False)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print('ERROR: export ANTHROPIC_API_KEY="your-key-here"')
        return
    client = Anthropic(api_key=api_key)

    predictions, total = [], len(df)
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        claim = row.to_dict()
        registry = build_registry_summary(claim)
        category = str(claim.get("category") or "general")
        pred = classify_one(client, str(claim.get("text", "")), registry, category)
        predictions.append(pred)
        if i % 10 == 0 or i == total:
            print(f"  Classified {i}/{total}")
        time.sleep(DELAY)

    # Rename human_label → label so evaluate_f1.py works unchanged
    if "human_label" in df.columns and "label" not in df.columns:
        df = df.rename(columns={"human_label": "label"})
    df["prediction"] = predictions
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\nSaved {OUTPUT_CSV}")
    print(f"Distribution: {dict(pd.Series(predictions).value_counts())}")
    print("\nNext: python groq_classifier.py")


if __name__ == "__main__":
    run()