"""
================================================================
 PROOF OF GREEN — APP (SME Owner view, first pass at the new design)
================================================================
 Built against the real, updated pipeline output (Citation + Guidance
 fields now included — see ecgt_pipeline_stage4.py / extract_claims.py
 changes made earlier this session).

 Design source: the dark navy/teal mockup image, matched as closely
 as Streamlit reasonably allows without a custom frontend. NOT yet
 built: Procurement Manager view (circular gauge, registry checklist,
 supplier comparison) — that's the next step once this is confirmed
 solid.

 Multi-file loading: automatically picks up every
 greenlens_claims_*.xlsx file sitting in the same folder as this
 script. As background pipeline runs finish and new company files
 land here, they show up on next refresh — no code changes needed.

 Local persistence: Agree/Review decisions on individual claims are
 saved to decisions.db (SQLite, created automatically on first run)
 in the same folder. Runs fully locally — see the earlier discussion
 on why local (not hosted) is the right fit for this tool right now.

 Run:
   pip install streamlit pandas openpyxl
   streamlit run app.py
================================================================
"""

import glob
import hashlib
import io
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from anthropic import Anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

st.set_page_config(page_title="Proof of Green", page_icon="🌱", layout="wide")

FILE_PATTERN = "greenlens_claims_*.xlsx"
DB_PATH = "decisions.db"

LABEL_COLOR = {"RED": "#E5484D", "AMBER": "#D9A441", "GREEN": "#3DAA6E"}
LABEL_BG = {"RED": "#3A1F22", "AMBER": "#3A3320", "GREEN": "#1F3A2A"}
LABEL_ORDER = {"RED": 0, "AMBER": 1, "GREEN": 2}
STAT_META = {
    "GREEN": ("Compliant", LABEL_COLOR["GREEN"]),
    "AMBER": ("Needs attention", LABEL_COLOR["AMBER"]),
    "RED": ("Violation", LABEL_COLOR["RED"]),
}

REGISTRY_COLS = [
    ("EMAS Verified", "EMAS"),
    ("EU Organic Verified", "EU Organic"),
    ("B Corp Verified", "B Corp"),
    ("Bord Bia Verified", "Bord Bia"),
    ("Biopartenaire Verified", "Biopartenaire"),
    ("BioED Verified", "BioED"),
]

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 10px;
        background-color: #1A2540;
        border: 1px solid #26365C;
    }
    div[data-testid="column"] button {
        border-radius: 8px !important;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


def category_breakdown_chart(sub: pd.DataFrame):
    """Stacked horizontal bar: claims per category, segmented by RAG label.
    Shows which TYPES of claims are driving the RED/AMBER counts — new
    information, not just a re-plot of the totals already in the stat
    cards above it."""
    if sub.empty or "Category" not in sub.columns:
        return

    counts = sub.groupby(["Category", "ECGT Label"]).size().unstack(fill_value=0)
    for lbl in ["RED", "AMBER", "GREEN"]:
        if lbl not in counts.columns:
            counts[lbl] = 0
    counts = counts[["RED", "AMBER", "GREEN"]]
    # Sort so the category with the most claims sits at the top of the chart
    counts = counts.loc[counts.sum(axis=1).sort_values(ascending=True).index]

    fig = go.Figure()
    for lbl in ["GREEN", "AMBER", "RED"]:  # stacking order: RED ends up nearest the axis
        fig.add_trace(go.Bar(
            y=counts.index, x=counts[lbl], name=lbl,
            orientation="h", marker_color=LABEL_COLOR[lbl],
        ))
    fig.update_layout(
        barmode="stack",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#E8EDF2", family="Inter, sans-serif", size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=10, r=10, t=36, b=10),
        height=max(220, 42 * len(counts)),
        xaxis=dict(gridcolor="#26365C", title="Claims", zeroline=False),
        yaxis=dict(title=""),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ------------------------------
# Data loading — multi-file
# ------------------------------
def _is_yes(val) -> bool:
    return str(val).strip().lower() in ("yes", "true", "1")


def claim_id(row) -> str:
    """Stable ID for a claim, used as the persistence key. Built from
    company + page + claim text since there's no explicit ID column."""
    raw = f"{row.get('Company','')}|{row.get('Page','')}|{row.get('Verbatim Claim','')}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


@st.cache_data
def load_data(pattern: str) -> pd.DataFrame:
    files = glob.glob(pattern)
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        try:
            frames.append(pd.read_excel(f, sheet_name="All Claims"))
        except Exception as e:
            st.warning(f"Could not read {f}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["review_flag_bool"] = df.get("ECGT Review Flag", pd.Series(dtype=object)).apply(_is_yes)
    df["label_sort"] = df["ECGT Label"].map(LABEL_ORDER).fillna(3)
    df["claim_id"] = df.apply(claim_id, axis=1)
    return df


@st.cache_data
def load_news_data(pattern: str) -> pd.DataFrame:
    """News Check is a separate sheet, one row per company normally, but
    MULTIPLE rows per company when controversy is detected (one row per
    flagged article — see news_verifier.py's append_news_sheet). Older
    pipeline runs may not have this sheet at all, so missing it entirely
    for a given file is expected, not an error worth warning about."""
    files = glob.glob(pattern)
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        try:
            frames.append(pd.read_excel(f, sheet_name="News Check"))
        except Exception:
            pass  # sheet genuinely absent on older runs — silently skip
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ------------------------------
# Local persistence (SQLite)
# ------------------------------
@st.cache_resource
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            claim_id TEXT PRIMARY KEY,
            decision TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_summaries (
            company TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            generated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_decision(conn, cid: str):
    row = conn.execute("SELECT decision FROM decisions WHERE claim_id = ?", (cid,)).fetchone()
    return row[0] if row else None


def set_decision(conn, cid: str, decision: str):
    conn.execute(
        """INSERT INTO decisions (claim_id, decision, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(claim_id) DO UPDATE SET decision = excluded.decision,
                                                updated_at = excluded.updated_at""",
        (cid, decision, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_ai_summary(conn, company: str):
    row = conn.execute(
        "SELECT summary, generated_at FROM ai_summaries WHERE company = ?", (company,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def set_ai_summary(conn, company: str, summary: str):
    conn.execute(
        """INSERT INTO ai_summaries (company, summary, generated_at) VALUES (?, ?, ?)
           ON CONFLICT(company) DO UPDATE SET summary = excluded.summary,
                                               generated_at = excluded.generated_at""",
        (company, summary, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def generate_ai_summary(company: str, sub: pd.DataFrame, news_row) -> str:
    """On-demand executive summary — a single live API call, made only
    when the user clicks the button, NOT part of the pipeline. Uses only
    data already computed (RAG counts, registry status, existing claim
    explanations); does not re-classify anything."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ("ERROR: ANTHROPIC_API_KEY not set in environment. "
                "Set it the same way you would for the pipeline itself.")

    total = len(sub)
    red = int((sub["ECGT Label"] == "RED").sum())
    amber = int((sub["ECGT Label"] == "AMBER").sum())
    green = int((sub["ECGT Label"] == "GREEN").sum())
    gap_score = red / total if total else 0

    confirmed = [label for col, label in REGISTRY_COLS if _is_yes(sub[col].iloc[0])
                 if col in sub.columns and len(sub)]
    registry_text = ", ".join(confirmed) if confirmed else "None confirmed"

    news_text = safe_str(news_row.get("Summary"), "No news check data available.") \
        if news_row is not None else "No news check data available."

    red_claims = sub[sub["ECGT Label"] == "RED"]
    red_text = "\n".join(
        f'- "{safe_str(r.get("Verbatim Claim"))[:150]}" — {safe_str(r.get("ECGT Explanation"))}'
        for _, r in red_claims.head(8).iterrows()
    ) or "None."

    amber_claims = sub[sub["ECGT Label"] == "AMBER"]
    amber_text = "\n".join(
        f'- "{safe_str(r.get("Verbatim Claim"))[:150]}" — {safe_str(r.get("ECGT Explanation"))}'
        for _, r in amber_claims.head(8).iterrows()
    ) or "None."

    prompt = f"""You are writing a concise executive summary for a supplier
due-diligence brief, for a procurement manager reviewing this company's
environmental marketing claims under EU Directive 2024/825 (ECGT).

Company: {company}
Total claims analysed: {total}
Compliant (GREEN): {green} ({green/total:.0%} of total)
Needs attention (AMBER): {amber} ({amber/total:.0%} of total)
Violations (RED): {red} ({red/total:.0%} of total)
Gap Score (RED / total): {gap_score:.0%}

Registries confirmed: {registry_text}

News coverage summary: {news_text}

RED (likely non-compliant) claims found:
{red_text}

AMBER (needs evidence) claims found:
{amber_text}

Write a concise 2-paragraph executive summary (roughly half the length
of a full brief) suitable for a procurement due-diligence file. First
paragraph: overall compliance posture and the single most significant
concern, citing one concrete example from the RED/AMBER claims above.
Second paragraph: whether the confirmed certifications provide
meaningful assurance given what they actually cover, and a
plain-language procurement recommendation. Write in a neutral,
professional, factual tone appropriate for a compliance record — no
marketing language, no hedging beyond what the data supports. Do not
state any fact not given above."""

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=450,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"ERROR generating summary: {e}"


def build_pdf_brief(company: str, sub: pd.DataFrame, news_row, ai_summary: str) -> bytes:
    """Assembles a due-diligence brief PDF from data already computed —
    no new classification, no re-running the pipeline. AI summary is
    optional and passed in already-generated (or None)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             topMargin=2*cm, bottomMargin=2*cm,
                             leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BriefTitle", parent=styles["Title"], fontSize=20)
    h2 = ParagraphStyle("BriefH2", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    body = styles["BodyText"]
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=9, textColor=colors.grey)

    total = len(sub)
    red = int((sub["ECGT Label"] == "RED").sum())
    amber = int((sub["ECGT Label"] == "AMBER").sum())
    green = int((sub["ECGT Label"] == "GREEN").sum())
    gap_score = red / total if total else 0

    story = []
    story.append(Paragraph("Proof of Green — Due Diligence Brief", title_style))
    story.append(Paragraph(company, styles["Heading1"]))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"{total} claims analysed", small))
    story.append(Spacer(1, 0.5*cm))

    stats_table = Table(
        [["Total", "GREEN", "AMBER", "RED", "Gap Score"],
         [str(total), str(green), str(amber), str(red), f"{gap_score:.0%}"]],
        colWidths=[3*cm]*5,
    )
    stats_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A2540")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 0.5*cm))

    if ai_summary and not ai_summary.startswith("ERROR"):
        story.append(Paragraph("Executive Summary", h2))
        for para in ai_summary.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), body))
                story.append(Spacer(1, 0.2*cm))

    story.append(Paragraph("Registry Verification", h2))
    confirmed = [label for col, label in REGISTRY_COLS if _is_yes(sub[col].iloc[0])
                 if col in sub.columns and len(sub)]
    story.append(Paragraph(
        ", ".join(confirmed) if confirmed else "No registries confirmed.", body))

    story.append(Paragraph("News Coverage", h2))
    news_text = safe_str(news_row.get("Summary"), "No news check data available.") \
        if news_row is not None else "No news check data available."
    story.append(Paragraph(news_text, body))

    story.append(PageBreak())
    story.append(Paragraph("RED Claims (Likely Non-Compliant)", h2))
    red_claims = sub[sub["ECGT Label"] == "RED"]
    if red_claims.empty:
        story.append(Paragraph("None.", body))
    else:
        for _, r in red_claims.iterrows():
            story.append(Paragraph(f'<b>Claim:</b> {safe_str(r.get("Verbatim Claim"))}', body))
            story.append(Paragraph(f'<b>Article:</b> {safe_str(r.get("ECGT Citation"))}', body))
            story.append(Paragraph(f'<b>Explanation:</b> {safe_str(r.get("ECGT Explanation"))}', body))
            story.append(Spacer(1, 0.3*cm))

    story.append(Paragraph("AMBER Claims (Needs Evidence)", h2))
    amber_claims = sub[sub["ECGT Label"] == "AMBER"]
    if amber_claims.empty:
        story.append(Paragraph("None.", body))
    else:
        for _, r in amber_claims.iterrows():
            story.append(Paragraph(f'<b>Claim:</b> {safe_str(r.get("Verbatim Claim"))}', body))
            story.append(Paragraph(f'<b>Article:</b> {safe_str(r.get("ECGT Citation"))}', body))
            story.append(Paragraph(f'<b>Guidance:</b> {safe_str(r.get("ECGT Guidance"))}', body))
            story.append(Paragraph(f'<b>Explanation:</b> {safe_str(r.get("ECGT Explanation"))}', body))
            story.append(Spacer(1, 0.3*cm))

    doc.build(story)
    return buf.getvalue()


def registry_summary(row) -> str:
    confirmed = [label for col, label in REGISTRY_COLS if _is_yes(row.get(col))]

    return ", ".join(confirmed) if confirmed else "None confirmed"


def html_block(content: str):
    """st.markdown(unsafe_allow_html=True) still runs content through
    Markdown processing — standard Markdown treats 4+ leading spaces on a
    line as a code block, so pretty-indented multi-line HTML f-strings get
    partially rendered as literal text instead of parsed as HTML. Stripping
    per-line indentation before rendering avoids this."""
    stripped = "\n".join(line.strip() for line in content.strip().split("\n"))
    st.markdown(stripped, unsafe_allow_html=True)


def safe_str(val, default="—") -> str:
    """Converts a row value to a display string, treating pandas NaN
    (empty Excel cells) as the default rather than showing literal 'nan'."""
    if pd.isna(val):
        return default
    s = str(val).strip()
    return s if s else default


def short_citation(citation) -> str:
    """Citation field is long ('ECGT_002 - Annex I, point 4a - Generic...') —
    the mockup shows just the short article ref ('Annex I 4a'). Extract that
    if the format matches; otherwise fall back to the first ~20 chars.

    NOTE: pandas represents an empty Excel cell as a float NaN, not an empty
    string — pd.isna() must be checked BEFORE calling any string method, or
    this crashes with 'float has no attribute lower' on real data."""
    if pd.isna(citation):
        return "—"
    citation = str(citation).strip()
    if not citation or citation in ("N/A", "None", "nan", ""):
        return "—"
    if citation.lower().startswith("none"):
        return "General principle"
    parts = citation.split(" - ")
    if len(parts) >= 2:
        return parts[1].replace("point ", "").replace("Annex I,", "Annex I")
    return citation[:20]


def stat_block(count: int, label: str):
    sublabel, color = STAT_META[label]
    html_block(f"""
    <div style="background:{color}18; border-left:5px solid {color}; border-radius:10px;
                padding:20px 22px; height:120px;">
        <div style="font-size:34px; font-weight:800; color:{color}; line-height:1;">{count}</div>
        <div style="font-size:14px; font-weight:700; color:{color}; letter-spacing:0.5px; margin-top:6px;">{label}</div>
        <div style="font-size:12px; color:#8A97AC; margin-top:2px;">{sublabel}</div>
    </div>
    """)


def stat_card(value: str, label: str, sublabel: str, color: str):
    """Same visual style as stat_block, but for non-RAG stats (e.g. Gap
    Score) that need their own value/label/color rather than a RAG count."""
    html_block(f"""
    <div style="background:{color}18; border-left:5px solid {color}; border-radius:10px;
                padding:20px 22px; height:120px;">
        <div style="font-size:34px; font-weight:800; color:{color}; line-height:1;">{value}</div>
        <div style="font-size:14px; font-weight:700; color:{color}; letter-spacing:0.5px; margin-top:6px;">{label}</div>
        <div style="font-size:12px; color:#8A97AC; margin-top:2px;">{sublabel}</div>
    </div>
    """)


def claim_row(row, conn):
    import html as html_lib

    FLAG_COLOR = "#A78BFA"  # purple — distinct from RED/AMBER/GREEN so it never
                              # reads as a compliance-status color by mistake

    label = row["ECGT Label"]
    color = LABEL_COLOR.get(label, "#888")
    bg = LABEL_BG.get(label, "#222")
    cid = row["claim_id"]
    is_flagged = row["review_flag_bool"]
    current_decision = get_decision(conn, cid)
    preview = html_lib.escape(safe_str(row.get("Verbatim Claim", ""))[:90])
    citation_short = short_citation(row.get("ECGT Citation", ""))
    decision_marker = f" · {current_decision}" if current_decision else ""

    citation_html = (
        f'<span style="color:#8A97AC;">{html_lib.escape(citation_short)}</span>'
        f'<span style="color:#8A97AC;"> — </span>'
        if citation_short != "—" else ""
    )

    flag_badge = (
        f'<span style="background:{FLAG_COLOR}26; color:{FLAG_COLOR}; font-weight:700; '
        f'font-size:11px; padding:3px 10px; border-radius:999px; letter-spacing:0.3px; '
        f'white-space:nowrap;">⚑ NEEDS REVIEW</span>'
        if is_flagged else ""
    )

    expand_key = f"expanded_{cid}"
    if expand_key not in st.session_state:
        st.session_state[expand_key] = False

    # Header: built as raw HTML with the row's own inline style, so the
    # color is guaranteed correct — not dependent on CSS targeting a
    # specific instance of a shared widget class, which Streamlit doesn't
    # support cleanly for st.expander. Flagged claims get a full outline
    # (not just the RAG-colored left edge) so they're visible while
    # scanning, not just when read closely.
    outline = f"2px solid {FLAG_COLOR}" if is_flagged else f"1px solid {bg}"
    second_row = (
        f'<div style="margin-top:6px;">{flag_badge}'
        f'<span style="color:#8A97AC; font-size:13px; margin-left:8px;">{decision_marker}</span></div>'
        if (is_flagged or decision_marker) else ""
    )
    html_block(f"""
    <div style="background:{bg}; border-radius:8px; padding:12px 16px; margin-bottom:2px;
                border-top:{outline}; border-right:{outline}; border-bottom:{outline};
                border-left:4px solid {color};">
        <div>
            <span style="color:{color}; font-weight:800; font-size:13px;">{label}</span>
            <span style="color:#8A97AC;"> · </span>
            {citation_html}
            <span style="color:#E8EDF2; font-size:14px;">{preview}</span>
        </div>
        {second_row}
    </div>
    """)

    toggle_label = "▾ Hide details" if st.session_state[expand_key] else "▸ Show details"
    if st.button(toggle_label, key=f"toggle_{cid}", use_container_width=False):
        st.session_state[expand_key] = not st.session_state[expand_key]
        st.rerun()

    if not st.session_state[expand_key]:
        st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
        return

    with st.container(border=True):
        st.markdown(f"**Claim:** {safe_str(row.get('Verbatim Claim'))}")
        translation = row.get("English Translation")
        if pd.notna(translation) and translation != row.get("Verbatim Claim"):
            st.markdown(f"**English translation:** {translation}")
        st.markdown(f"**Category:** {safe_str(row.get('Category'))}")

        st.markdown("---")
        st.markdown(f"**Rule triggered:**  \n{safe_str(row.get('ECGT Rule Triggered'))}")
        st.markdown(f"**Article:** {safe_str(row.get('ECGT Citation'))}")
        st.markdown(f"**Explanation:**  \n{safe_str(row.get('ECGT Explanation'))}")
        st.markdown(f"**Registries confirmed:** {registry_summary(row)}")

        guidance = str(row.get("ECGT Guidance", "NONE")).upper()
        if guidance not in ("NONE", "NAN", ""):
            html_block(f"""
            <div style="background:{bg}; border:1px solid {color}55; border-radius:8px;
                        padding:12px 16px; margin-top:10px;">
                <div style="color:{color}; font-weight:700; font-size:13px; margin-bottom:4px;">
                    🚩 Fix Guidance — {guidance}
                </div>
                <div style="color:#C7D0DE; font-size:13px;">{safe_str(row.get('ECGT Explanation'), '')}</div>
            </div>
            """)

        if row["review_flag_bool"]:
            st.info("🚩 Model flagged this as genuinely uncertain — human judgment needed.")

        st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
        b1, b2, b3 = st.columns([1, 1, 3])
        if b1.button("✓ Agree", key=f"agree_{cid}",
                     type="primary" if current_decision == "AGREE" else "secondary"):
            set_decision(conn, cid, "AGREE")
            st.rerun()
        if b2.button("↻ Review", key=f"review_{cid}",
                     type="primary" if current_decision == "REVIEW" else "secondary"):
            set_decision(conn, cid, "REVIEW")
            st.rerun()
        if current_decision:
            b3.caption(f"Recorded: {current_decision}")

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)


# ------------------------------
# Load data + DB
# ------------------------------
df = load_data(FILE_PATTERN)
news_df = load_news_data(FILE_PATTERN)
conn = get_db()

if df.empty:
    st.error(f"No files matching {FILE_PATTERN} found in this folder. "
             f"Drop your greenlens_claims_*.xlsx exports here and refresh.")
    st.stop()

companies = sorted(df["Company"].dropna().unique())

# ------------------------------
# Sidebar
# ------------------------------
st.sidebar.markdown(
    '<div style="padding:6px 0 14px 0;">'
    '<span style="font-size:20px; font-weight:800;">🌱 Proof of Green</span><br>'
    '<span style="font-size:12px; color:#8A97AC;">ECGT compliance screening</span>'
    '</div>', unsafe_allow_html=True,
)
company = st.sidebar.selectbox("Company", companies)
view = st.sidebar.radio("View", ["SME Owner", "Procurement Manager"])
st.sidebar.caption(f"{len(companies)} compan{'y' if len(companies)==1 else 'ies'} loaded "
                    f"({len(glob.glob(FILE_PATTERN))} files)")

st.sidebar.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
with st.sidebar.expander("➕ Run pipeline on a new company"):
    new_url = st.text_input("Website URL", key="new_url", placeholder="https://example.com")
    new_company_name = st.text_input("Company name", key="new_company_name", placeholder="")
    st.caption("Optional — derived from the URL if left blank, but recommended for accuracy.")
    new_country = st.text_input("Country", key="new_country", placeholder="")
    st.caption("Optional — improves certification-check accuracy over guessing from the "
                "domain (e.g. .com/.bio give no country hint on their own).")

    if st.button("▶ Run Pipeline", key="run_pipeline_btn", type="primary", use_container_width=True):
        if not new_url.strip():
            st.error("Website URL is required.")
        elif not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("OPENAI_API_KEY"):
            # Checked BEFORE calling run_pipeline() on purpose: that function
            # calls sys.exit(1) if these are missing, which would kill the
            # whole Streamlit server process, not just show an error here.
            st.error("ANTHROPIC_API_KEY and/or OPENAI_API_KEY not set in the "
                        "environment this app is running in. Set them the same "
                        "way you would to run the pipeline from the command line, "
                        "then restart the app.")
        else:
            # Imported here, not at module load time, so a problem with any
            # pipeline dependency (Playwright, cert/news verifier modules,
            # etc.) can't break the rest of the dashboard for someone who
            # never uses this feature.
            try:
                from pipelineV1 import run_pipeline
            except Exception as e:
                st.error(f"Could not import pipelineV1: {e}")
                st.stop()

            with st.spinner(f"Running full pipeline for {new_company_name or new_url} — "
                             f"this typically takes 10-15 minutes, please don't close this tab..."):
                try:
                    countries = [new_country.strip()] if new_country.strip() else None
                    result = run_pipeline(
                        new_url.strip(),
                        new_company_name.strip() or None,
                        countries=countries,
                        verbose=False,
                    )
                    _, _, ecgt_result, news_result, excel_path = result
                except Exception as e:
                    st.error(f"Pipeline run failed: {e}")
                    st.stop()

            st.success(f"Done — saved to {excel_path}. "
                        f"ECGT labels: {ecgt_result['label_distribution']}")
            load_data.clear()
            load_news_data.clear()
            st.rerun()

sub = df[df["Company"] == company]

if view == "SME Owner":
    st.title("SME Owner View")
    st.caption(f"{company} — {len(sub)} claims analysed")

    # ------------------------------
    # Stat bars
    # ------------------------------
    s1, s2, s3, s4 = st.columns(4)
    with s1:
        stat_block(int((sub["ECGT Label"] == "GREEN").sum()), "GREEN")
    with s2:
        stat_block(int((sub["ECGT Label"] == "AMBER").sum()), "AMBER")
    with s3:
        stat_block(int((sub["ECGT Label"] == "RED").sum()), "RED")
    with s4:
        gap_score = (sub["ECGT Label"] == "RED").sum() / len(sub) if len(sub) else 0
        stat_card(f"{gap_score:.0%}", "GAP SCORE", "RED ÷ total claims", "#3FA9C9")

    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)

    category_breakdown_chart(sub)

    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)

    # ------------------------------
    # Filter pills
    # ------------------------------
    state_key = f"label_filter_{company}"
    if state_key not in st.session_state:
        st.session_state[state_key] = "All"

    counts = {
        "All": len(sub),
        "RED": int((sub["ECGT Label"] == "RED").sum()),
        "AMBER": int((sub["ECGT Label"] == "AMBER").sum()),
        "GREEN": int((sub["ECGT Label"] == "GREEN").sum()),
        "Flagged": int(sub["review_flag_bool"].sum()),
    }
    options = ["All", "RED", "AMBER", "GREEN", "Flagged"]
    btn_cols = st.columns(len(options))
    for col, option in zip(btn_cols, options):
        is_active = st.session_state[state_key] == option
        label_text = f"⚑ Flagged ({counts[option]})" if option == "Flagged" else f"{option} ({counts[option]})"
        if col.button(label_text, key=f"{state_key}_{option}",
                      type="primary" if is_active else "secondary", use_container_width=True):
            st.session_state[state_key] = option
            st.rerun()

    st.caption("sorted: highest risk first")

    selected = st.session_state[state_key]
    if selected == "All":
        filtered = sub
    elif selected == "Flagged":
        filtered = sub[sub["review_flag_bool"]]
    else:
        filtered = sub[sub["ECGT Label"] == selected]
    filtered = filtered.sort_values("label_sort")

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

    if filtered.empty:
        st.info("No claims in this category.")
    else:
        for _, row in filtered.iterrows():
            claim_row(row, conn)

# ------------------------------
# Procurement Manager View — per-company detail + cross-company comparison
# ------------------------------
elif view == "Procurement Manager":
    st.title("Procurement Manager View")
    st.caption(f"{company} — supplier due diligence")

    gap_score = (sub["ECGT Label"] == "RED").sum() / len(sub) if len(sub) else 0
    company_news = news_df[news_df["Company"] == company] if not news_df.empty else pd.DataFrame()
    news_row = company_news.iloc[0] if not company_news.empty else None

    st.markdown("<div style='height:6px;'></div>", unsafe_allow_html=True)
    st.subheader("Due Diligence Brief")
    ai_summary, ai_generated_at = get_ai_summary(conn, company)

    ai_col1, ai_col2 = st.columns([1, 1])
    with ai_col1:
        btn_label = "🔄 Regenerate AI Summary" if ai_summary else "✨ Generate AI Summary"
        if st.button(btn_label, key=f"gen_summary_{company}"):
            with st.spinner("Generating executive summary..."):
                new_summary = generate_ai_summary(company, sub, news_row)
                set_ai_summary(conn, company, new_summary)
            st.rerun()
    with ai_col2:
        pdf_bytes = build_pdf_brief(company, sub, news_row, ai_summary)
        st.download_button(
            "⬇ Download Brief (PDF)", data=pdf_bytes,
            file_name=f"{company.replace(' ', '_')}_due_diligence_brief.pdf",
            mime="application/pdf", key=f"download_brief_{company}",
        )

    if ai_summary:
        if ai_summary.startswith("ERROR"):
            st.error(ai_summary)
        else:
            st.markdown(f"""
            <div style="background:#1A2540; border:1px solid #26365C; border-radius:8px;
                        padding:14px 18px; margin-top:10px;">
                {ai_summary.replace(chr(10)+chr(10), '<br><br>')}
            </div>
            """, unsafe_allow_html=True)
            st.caption(f"Generated {ai_generated_at}")
    else:
        st.caption("No AI summary generated yet for this company — click above to generate one, "
                    "or download the brief without it (structured data only).")

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)

    g1, g2 = st.columns([1, 2])
    with g1:
        st.subheader("Registry Verified")
        confirmed_any = False
        for col, reg_label in REGISTRY_COLS:
            if _is_yes(sub[col].iloc[0]) if col in sub.columns and len(sub) else False:
                st.markdown(f"✅ **{reg_label}** — confirmed")
                confirmed_any = True
        if not confirmed_any:
            st.caption("No registries confirmed for this company.")

    with g2:
        for lbl in ["GREEN", "AMBER", "RED"]:
            pct = (sub["ECGT Label"] == lbl).sum() / len(sub) if len(sub) else 0
            html_block(f"""
            <div style="margin-bottom:10px;">
                <div style="display:flex; justify-content:space-between; margin-bottom:3px;">
                    <span style="color:{LABEL_COLOR[lbl]}; font-weight:700; font-size:13px;">{lbl}</span>
                    <span style="color:#8A97AC; font-size:12px;">{pct:.0%}</span>
                </div>
                <div style="background:#1A2540; border-radius:6px; height:10px; overflow:hidden;">
                    <div style="background:{LABEL_COLOR[lbl]}; width:{pct*100}%; height:100%;"></div>
                </div>
            </div>
            """)

    st.markdown("<div style='height:10px;'></div>", unsafe_allow_html=True)
    r1, r2 = st.columns([1, 1.6])

    with r1:
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=gap_score * 100,
            number={"suffix": "%", "font": {"color": "#E8EDF2", "size": 40}},
            gauge={
                "axis": {"range": [0, 100], "tickcolor": "#8A97AC"},
                "bar": {"color": "#3FA9C9"},
                "bgcolor": "#1A2540",
                "borderwidth": 0,
            },
        ))
        # Deliberately NOT adding a LOW/MEDIUM/HIGH risk band here — those
        # thresholds were never validated, same reasoning as dropping the
        # risk badge earlier. This shows the raw gap score only.
        fig.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#E8EDF2", family="Inter, sans-serif"),
            height=220, margin=dict(l=20, r=20, t=20, b=10),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("Gap Score = RED claims ÷ total claims")

    with r2:
        st.subheader("Supplier Comparison — Gap Score")
        portfolio = (
            df.groupby("Company")
            .apply(lambda g: pd.Series({
                "Total": len(g),
                "RED": int((g["ECGT Label"] == "RED").sum()),
            }), include_groups=False)
            .reset_index()
        )
        portfolio["Gap Score"] = portfolio["RED"] / portfolio["Total"]
        portfolio = portfolio.sort_values("Gap Score", ascending=True)

        fig2 = go.Figure(go.Bar(
            y=portfolio["Company"], x=portfolio["Gap Score"], orientation="h",
            marker_color=["#3FA9C9" if c != company else "#E8EDF2" for c in portfolio["Company"]],
            text=[f"{v:.0%}" for v in portfolio["Gap Score"]], textposition="outside",
        ))
        fig2.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#E8EDF2", family="Inter, sans-serif", size=13),
            margin=dict(l=10, r=40, t=10, b=10),
            height=max(200, 42 * len(portfolio)),
            xaxis=dict(gridcolor="#26365C", tickformat=".0%", title=""),
            yaxis=dict(title=""),
        )
        st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})
        st.caption(f"Currently viewing: {company} (highlighted)")

    st.markdown("<div style='height:20px;'></div>", unsafe_allow_html=True)
    st.subheader("News Coverage")

    if company_news.empty:
        st.caption("No news check data available for this company "
                    "(older pipeline run, or news check was skipped).")
    else:
        # First row carries the company-level summary regardless of how
        # many rows exist (one row per flagged article when controversy
        # is detected — see news_verifier.py's append_news_sheet).
        first = news_row
        controversy = _is_yes(first.get("Controversy Detected"))

        if controversy:
            html_block(f"""
            <div style="background:{LABEL_BG['RED']}; border-left:4px solid {LABEL_COLOR['RED']};
                        border-radius:8px; padding:12px 16px; margin-bottom:10px;">
                <div style="color:{LABEL_COLOR['RED']}; font-weight:700; font-size:13px;">
                    ⚠ Controversy detected
                </div>
                <div style="color:#C7D0DE; font-size:13px; margin-top:4px;">{safe_str(first.get('Summary'))}</div>
            </div>
            """)
            # Flagged Article/URL/Reason are per-row when controversy=Yes;
            # NaN-check each field individually since a row could have a
            # partial fill.
            flagged_rows = company_news[company_news["Flagged Article"].notna()] \
                if "Flagged Article" in company_news.columns else pd.DataFrame()
            for _, frow in flagged_rows.iterrows():
                st.markdown(f"**{safe_str(frow.get('Flagged Article'))}**")
                url = safe_str(frow.get("URL"), "")
                if url:
                    st.markdown(f"[{url}]({url})")
                st.markdown(f"*{safe_str(frow.get('Reason'))}*")
                st.markdown("---")
        else:
            html_block(f"""
            <div style="background:{LABEL_BG['GREEN']}; border-left:4px solid {LABEL_COLOR['GREEN']};
                        border-radius:8px; padding:12px 16px;">
                <div style="color:{LABEL_COLOR['GREEN']}; font-weight:700; font-size:13px;">
                    ✓ No controversy detected
                </div>
                <div style="color:#C7D0DE; font-size:13px; margin-top:4px;">{safe_str(first.get('Summary'))}</div>
            </div>
            """)

    st.markdown("<div style='height:16px;'></div>", unsafe_allow_html=True)
    st.subheader("AMBER Claims — Needs Evidence")
    amber = sub[sub["ECGT Label"] == "AMBER"].sort_values("label_sort")
    if amber.empty:
        st.caption("No AMBER claims for this company.")
    else:
        for _, row in amber.iterrows():
            claim_row(row, conn)