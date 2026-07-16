"""
================================================================
 GREENLENS — CROSS-MODEL KAPPA (Claude vs Gemini only)
================================================================
 Input : predictions_gemini.csv
 Run   : python evaluate_kappa.py
================================================================
"""
import pandas as pd
from sklearn.metrics import cohen_kappa_score, confusion_matrix

INPUT_CSV = "predictions_gemini.csv"
LABELS    = ["RED", "AMBER", "GREEN"]

df  = pd.read_csv(INPUT_CSV)
valid = (df["prediction"].isin(LABELS) & df["prediction_gemini"].isin(LABELS))
yc = df.loc[valid, "prediction"]
yg = df.loc[valid, "prediction_gemini"]
n  = valid.sum()

kappa = cohen_kappa_score(yc, yg)

if   kappa > 0.80: interp = "Excellent agreement"
elif kappa > 0.60: interp = "Good agreement"
elif kappa > 0.40: interp = "Moderate agreement"
else:              interp = "Poor agreement"

print(f"\n{'='*55}")
print(f"  CROSS-MODEL COHEN'S KAPPA")
print(f"  Claude Sonnet 4.6 vs Gemini 2.5 Flash-Lite")
print(f"  (both received identical prompts and examples)")
print(f"{'='*55}")
print(f"\n  Kappa = {kappa:.3f}   [{interp}]")
print(f"  Claims evaluated: {n}\n")

cm = confusion_matrix(yc, yg, labels=LABELS)
print(f"  Agreement matrix (Claude rows, Gemini columns):")
print(f"  {'claude \\ gemini':<16}" + "".join(f"{l:>8}" for l in LABELS))
for i, label in enumerate(LABELS):
    row_str = f"  {label:<16}" + "".join(f"{cm[i][j]:>8}" for j in range(3))
    print(row_str)
print()
for i, label in enumerate(LABELS):
    tot = cm[i].sum()
    pct = cm[i][i] / tot * 100 if tot else 0
    print(f"  {label:<7}: {cm[i][i]}/{tot} claims in agreement ({pct:.0f}%)")
print()