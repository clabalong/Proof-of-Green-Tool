"""
================================================================
 GREENLENS — CLAUDE SONNET 4.6 ECGT CLASSIFIER
================================================================
 Input  : all_claims_labeled.csv (26-column export from the
          merged "All Claims" sheet)
 Output : predictions.csv
           columns: all original + prediction (Claude's label)

 Run:
   export ANTHROPIC_API_KEY="your-key-here"
   python claude_classifier.py

 Then run:
   python openai_classifier.py
   python evaluate_f1.py
   python evaluate_kappa.py
================================================================
"""

import os
import time
import pandas as pd
from anthropic import Anthropic

# ── Configuration ────────────────────────────────────────────
MODEL     = "claude-haiku-4-5"
INPUT_CSV = "all_claims_labeled.csv"
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

# ── Column mapping: raw Excel export -> internal name ──────────
# The GreenLens "All Claims" export uses human-readable column names
# with spaces/capitals ("Verbatim Claim", "EMAS Verified", ...); the
# classifier internals use lowercase snake_case ("text",
# "emas_verified", ...). This maps one to the other so you never have
# to rename columns by hand before running this script. Extend this
# dict if the export schema changes.
RAW_TO_INTERNAL = {
    "Verbatim Claim":          "text",
    "Category":                "category",
    "EMAS Verified":           "emas_verified",
    "EU Organic Verified":     "eu_organic_verified",
    "B Corp Verified":         "bcorp_verified",
    "Bord Bia Verified":       "bordbia_verified",
    "Biopartenaire Verified":  "biopartenaire_verified",
    "BioED Verified":          "bioed_verified",
    "ECGT Label":              "label",
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


# ── ECGT Rules (v1) ─────────────────────────────────────────────
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
  • Claim references a specific certification AND registry confirms THAT
    SPECIFIC certification. Registry-type matching matters: a claim about
    organic status needs EU Organic (or BIOPARTENAIRE/BioED where relevant)
    confirmed specifically — a confirmed B Corp or Bord Bia certification
    does NOT verify an organic claim, a fair-trade claim, or a biodynamic
    claim. Those schemes check different things. Do not let confirmation
    of ANY registry create a blanket credibility floor for a claim about
    a DIFFERENT kind of certification.
  • Claim states a CONCRETE, VERIFIABLE FACT (named supplier, specific
    action already taken, measurable outcome, date, location) AND the
    TYPE-MATCHING registry is confirmed for this company.
  • Claim describes participation in a named, verifiable public programme,
    and the registry for that specific programme is confirmed.
  • Factual company history, founding story, or structure statements
    (who founded the company, when, where it's based, general geographic/
    origin description). These do NOT need registry confirmation — they
    aren't environmental performance claims being verified, they're
    background facts about the company. Do not route these into RED for
    lack of a certification; that's the wrong test for this claim type.

AMBER — NEEDS EVIDENCE. Use when:
  • Claim is SPECIFIC (a number, a named action, a concrete commitment)
    BUT no registry is confirmed for this company, OR the confirmed
    registry doesn't match the type of claim being made (e.g. company
    holds a confirmed cert, but not one that actually verifies THIS claim).
  • Claim is an ASPIRATION or FUTURE COMMITMENT with some specificity.
  • Claim uses vague language BUT company holds a CONFIRMED certification
    of the relevant type (certification is a credibility floor — even
    vague claims get AMBER when the company is independently verified for
    that kind of claim, not RED).
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
    IMPORTANT: this bare-tagline test applies to SHORT, STANDALONE
    phrases/labels — headings, single words, product tags with no
    subject+verb structure. It does NOT apply to full descriptive
    sentences just because their content is vague. A full sentence that
    happens to be vague marketing language (e.g. "...in the most
    sustainable way possible") is NOT automatically a bare tagline —
    apply BOUNDARY rule 4 below (default to AMBER under genuine
    uncertainty) rather than concluding RED just because the sentence's
    substance is thin.
  • Claim makes a sweeping whole-company green assertion with no evidence.
  • Claim names a certification the company does NOT hold.
  • Claim names a THIRD-PARTY CERTIFICATION, STANDARD, OR SCHEME that is
    NOT one of the six tracked registries (e.g. Fairtrade, Demeter,
    Biodynamic, FLO-Cert, Rainforest Alliance) as a BARE LABEL or PRODUCT
    TAG with no supporting sentence (e.g. "Black tablet 85% – Demeter –
    Biodynamic and Fairtrade"). Treat this the same as naming a
    certification that cannot be confirmed — RED. This is different from
    naming an untracked PARTNERSHIP or SPONSORSHIP (a charity partner, a
    conservation programme) in a full descriptive sentence, which is
    AMBER, not RED — the distinguishing factor is (a) whether it's a
    certification/standard claim specifically, which the Directive treats
    more strictly, and (b) whether there's a bare label with no sentence
    around it versus real descriptive detail.

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
     behavioural remains — then choose RED. This test applies regardless
     of whether the claim is a short label or a full sentence: a short
     standalone tagline (a heading, a single word, a product tag with no
     subject+verb) is simply the clearest, easiest case to apply it to.
     A longer sentence is NOT automatically exempt from RED just because
     it has sentence structure — if, after removing buzzwords, no
     concrete action, metric, or fact survives, it is still RED. Only
     lean AMBER on a vague sentence when you are genuinely torn, not as
     a default rule for all sentence-length claims.

═══════════════════════════════════════════════════════════
BOUNDARY — GREEN vs AMBER
═══════════════════════════════════════════════════════════

  1. Specific detail AND the TYPE-MATCHING registry confirmed → GREEN.
     Specific but no registry, or registry confirmed is the WRONG type
     for this claim (e.g. B Corp confirmed but the claim is about
     organic status) → AMBER.

  2. A specific claim from a company confirmed in the RELEVANT registry
     is GREEN, not AMBER. Do not downgrade to AMBER just because the
     wording "sounds like it might need more proof" — if the matching
     registry is confirmed, that IS the proof. But do not upgrade to
     GREEN just because SOME registry is confirmed if it isn't the one
     that actually verifies this claim.

  3. When uncertain between GREEN and AMBER, and the TYPE-MATCHING
     registry is confirmed → lean GREEN. If registry status is confirmed
     but for an unrelated certification, that is not grounds to lean
     GREEN — treat it the same as unconfirmed for this specific claim.

═══════════════════════════════════════════════════════════
LANGUAGE NOTE
═══════════════════════════════════════════════════════════
Claims may be in English, French, German, or Dutch.
Same rules apply regardless of language.
""".strip()

# ── Few-shot examples (v1) ─────────────────────────────────────
# 21 real examples from the actual claims_dataset.csv (8 companies).
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
    # ── Boundary J: registry-type mismatch — confirmed cert exists, but not the RIGHT one ──
    {"text": "Bread 41 is an organic bakery located in Dublin 2",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": True,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "AMBER",
     "note": "BOUNDARY: AMBER, NOT GREEN — B Corp is confirmed but B Corp does not verify organic status. The claim is about being organic; only EU Organic (or BIOPARTENAIRE/BioED) confirmation would make this GREEN. A confirmed cert of the WRONG type does not upgrade this."},
    # ── Boundary K: bare label naming an untracked certification scheme ──
    {"text": "Black tablet 85% – Demeter – Biodynamic and Fairtrade",
     "category": "certification",
     "emas_verified": False, "eu_organic_verified": True, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "RED",
     "note": "BOUNDARY: RED despite EU Organic confirmed elsewhere on the company — this is a bare product tag naming certifications (Demeter, Biodynamic, Fairtrade) that are NOT tracked registries and cannot be confirmed. An unrelated confirmed EU Organic cert does not extend credibility to different, unverifiable certification names used as a bare label."},
    # ── Boundary L: factual company history, no registry needed ──
    {"text": "Sisters Karen and Natalie Keane founded Bean and Goose with a simple idea: that chocolate could reflect a sense of place and a respect for where it comes from",
     "category": "general",
     "emas_verified": False, "eu_organic_verified": False, "bcorp_verified": False,
     "bordbia_verified": False, "biopartenaire_verified": False, "bioed_verified": False,
     "label": "GREEN",
     "note": "BOUNDARY: GREEN despite NO registry confirmed — this is a factual founding story, not an environmental performance claim. It doesn't need certification verification; don't route it to RED for lack of a cert that was never the relevant test."},
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


def load_and_prepare(input_csv: str) -> pd.DataFrame:
    """Load the raw GreenLens export and normalize it into the shape
    the classifier expects — column renaming, Yes/No -> True/False,
    and dropping rows already flagged as invalid claims upstream."""
    df = pd.read_csv(input_csv)

    # Rename any raw export columns we recognize; leave others untouched
    # so nothing is silently dropped if the schema evolves later.
    rename_map = {k: v for k, v in RAW_TO_INTERNAL.items() if k in df.columns}
    df = df.rename(columns=rename_map)

    missing = [v for v in ("text", "label") if v not in df.columns]
    if missing:
        raise ValueError(
            f"Expected column(s) {missing} not found after renaming. "
            f"Check RAW_TO_INTERNAL against your file's actual columns: "
            f"{list(df.columns)}"
        )

    # Convert Yes/No (or True/False, in case a differently-sourced file
    # already uses booleans) into real booleans for the verified fields.
    for field in VERIFIED_FIELDS:
        if field not in df.columns:
            df[field] = False
        else:
            df[field] = df[field].map({
                "Yes": True, "No": False,
                True: True, False: False,
                "True": True, "False": False,
            }).fillna(False)

    # Drop rows already flagged as invalid/incomplete claims upstream
    # (fragments, non-environmental text, nav menus, etc.) — these
    # shouldn't be sent to the classifier at all.
    if "GPT Valid Claim" in df.columns:
        before = len(df)
        df = df[df["GPT Valid Claim"] != "No"].copy()
        dropped = before - len(df)
        if dropped:
            print(f"Dropped {dropped} rows flagged invalid by GPT Valid Claim = No\n")

    return df


def run():
    df = load_and_prepare(INPUT_CSV)
    print(f"Loaded {len(df)} claims from {INPUT_CSV}\n")

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

    df["prediction"] = predictions
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\nSaved {OUTPUT_CSV}")
    print(f"Distribution: {dict(pd.Series(predictions).value_counts())}")
    print("\nNext: python openai_classifier.py")


if __name__ == "__main__":
    run()
