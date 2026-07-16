"""
================================================================
 GREENLENS — F1 EVALUATION (Claude vs Gemini, both vs human)
================================================================
 Input : predictions_gemini.csv  (has both prediction columns)
 Run   : python evaluate_f1.py
================================================================
"""
import pandas as pd
from sklearn.metrics import classification_report, f1_score

INPUT_CSV = "predictions_gemini.csv"
LABELS    = ["RED", "AMBER", "GREEN"]

df  = pd.read_csv(INPUT_CSV)
y_h = df["label"] if "label" in df.columns else df["human_label"]

def show_f1(y_true, y_pred, model_name):
    valid = y_pred.isin(LABELS)
    yt, yp = y_true[valid], y_pred[valid]
    macro    = f1_score(yt, yp, labels=LABELS, average="macro",    zero_division=0)
    weighted = f1_score(yt, yp, labels=LABELS, average="weighted", zero_division=0)
    report   = classification_report(yt, yp, labels=LABELS, output_dict=True, zero_division=0)

    print(f"\n{'='*55}")
    print(f"  {model_name}")
    print(f"{'='*55}")
    print(f"  {'Class':<8} {'Precision':>10} {'Recall':>8} {'F1':>8} {'Support':>9}")
    print(f"  {'-'*47}")
    for label in LABELS:
        r = report[label]
        print(f"  {label:<8} {r['precision']:>10.3f} {r['recall']:>8.3f} "
              f"{r['f1-score']:>8.3f} {int(r['support']):>9}")
    print(f"  {'-'*47}")
    print(f"  {'Macro F1':<30} {macro:>8.3f}")
    print(f"  {'Weighted F1':<30} {weighted:>8.3f}\n")
    return macro

print("\n" + "="*55)
print("  ECGT CLASSIFIER — F1 EVALUATION")
print("="*55)
print("  Both models evaluated against the same human labels.")

m_claude = show_f1(y_h, df["prediction"],       "Claude Sonnet 4.6")
m_gemini = show_f1(y_h, df["prediction_gemini"], "Gemini 2.5 Flash-Lite")

print(f"  {'─'*47}")
print(f"  {'Model':<30} {'Macro F1':>8}")
print(f"  {'─'*47}")
print(f"  {'Claude Sonnet 4.6':<30} {m_claude:>8.3f}")
print(f"  {'Gemini 2.5 Flash-Lite':<30} {m_gemini:>8.3f}")
winner = "Claude" if m_claude > m_gemini else "Gemini 2.5 Flash-Lite"
print(f"\n  {winner} achieves higher Macro F1 on this dataset.\n")