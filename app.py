import io
import re
import html
from typing import List, Dict, Optional, Tuple

import pandas as pd
import streamlit as st
from openpyxl.styles import Font


# ---------------------------
# Regex patterns (timestamps & speakers)
# ---------------------------

# First timestamp on a segment; supports:
#   (MM:SS.xxx), (MM:SS), HH:MM:SS(.ms), MM:SS(.ms)
TS_RE = re.compile(
    r"""
    (?:
        \(\s*(\d{1,2}):(\d{2})(?:\.\d+)?\s*\)    # (MM:SS.xxx) or (MM:SS)
      | \b(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?\b  # HH:MM:SS(.ms)
      | \b(\d{1,3}):(\d{2})(?:\.\d+)?\b          # MM:SS(.ms)
    )
    """,
    re.VERBOSE
)

# Speaker patterns at line start (ordered attempts)
SPEAKER_PATTERNS = [
    re.compile(r'^\s*\[([A-Za-z0-9 .\'\-&/]+)\]\s*'),                # [Name]
    re.compile(r'^\s*([A-Za-z0-9 .\'\-&/]+)\s*\(.*?\)\s*'),          # Name ( ... )
    re.compile(r'^\s*([A-Za-z0-9 .\'\-&/]+)\s*[:：\-–—]\s*'),        # Name:  or Name — / -
]

WRAP_CHARS = " \t\r\n\"'{}<>“”‘’"


# ---------------------------
# Helpers
# ---------------------------

def strip_wrappers(s: str) -> str:
    return s.strip(WRAP_CHARS)


def clean_quote(s: str) -> str:
    s = s.strip()
    s = s.lstrip("-–—:·•*#> ").strip()
    s = strip_wrappers(s)
    s = re.sub(r"\s+", " ", s)
    return s


def find_first_timestamp_segment(text: str) -> Optional[Tuple[str, int, int, int, int]]:
    """
    Return (raw_match, start, end, mm, ss) for the earliest timestamp in text.
    mm and ss are extracted from the last two time components by design.
    """
    matches = list(TS_RE.finditer(text))
    if not matches:
        return None
    m = min(matches, key=lambda x: x.start())
    nums = [g for g in m.groups() if g]
    if len(nums) >= 2:
        mm = int(nums[-2])
        ss = int(nums[-1])
        return (m.group(0), m.start(), m.end(), mm, ss)
    return None


def speaker_at_start(text: str) -> Tuple[Optional[str], int]:
    """
    Try to extract a speaker label at the start of text.
    Returns (speaker, consumed_length). If none, returns (None, 0).
    """
    for pat in SPEAKER_PATTERNS:
        m = pat.match(text)
        if m:
            spk = strip_wrappers(m.group(1))
            return spk, m.end()
    return None, 0


def apply_shift_and_format_mmss(mm: int, ss: int, shift_seconds: float) -> str:
    """
    Convert mm:ss to seconds, subtract shift_seconds, clip at 0, return 'MM:SS'.
    """
    total = mm * 60 + ss
    new_total = int(max(0, total - float(shift_seconds)))
    new_mm, new_ss = divmod(new_total, 60)
    return f"{new_mm:02d}:{new_ss:02d}"


def safe_replace_whole_name(text: str, name: str, replacement: str) -> str:
    """
    Case-insensitive replace of a full name with boundaries (not inside other words).
    Works for multi-word names.
    """
    if not name:
        return text
    pattern = re.compile(r'(?<!\w)' + re.escape(name) + r'(?!\w)', re.IGNORECASE)
    return pattern.sub(replacement, text)


def is_meaningful(text: str) -> bool:
    return bool(text and re.search(r'\w', text))


def word_count(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    return len([w for w in re.split(r"\s+", s) if w])


# ---------------------------
# Core parser
# ---------------------------

def parse_dialogue_lines(
    lines: List[str],
    counselor_name: str,
    client_name: str,
    shift_seconds: float
) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    pending_timestamp: Optional[str] = None
    pending_speaker_raw: Optional[str] = None
    last_speaker_public: Optional[str] = None  # 'Couns', 'Client', or other literal

    for raw in lines:
        # Normalize text & HTML entities (e.g., --> arrows)
        line = html.unescape(raw).replace("\ufeff", "")
        line = line.strip()
        if not line:
            continue

        # Ignore WEBVTT header
        if line.upper().startswith("WEBVTT"):
            continue

        # Split on long separator dashes if present
        segments = re.split(r'-{3,}', line)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            # WEBVTT cue line: take first timestamp only and carry it forward
            if "-->" in seg:
                ts_info = find_first_timestamp_segment(seg)
                if ts_info:
                    _, _, _, mm, ss = ts_info
                    pending_timestamp = apply_shift_and_format_mmss(mm, ss, shift_seconds)
                continue

            # 1) Extract first timestamp in this segment (keep only the first overall)
            ts_info = find_first_timestamp_segment(seg)
            if ts_info:
                raw_ts, s, e, mm, ss = ts_info
                local_ts = apply_shift_and_format_mmss(mm, ss, shift_seconds)
                if pending_timestamp is None:
                    pending_timestamp = local_ts
                seg = (seg[:s] + " " + seg[e:]).strip()

            # 2) Extract speaker at start (if any)
            spk_raw, consumed = speaker_at_start(seg)
            if spk_raw:
                pending_speaker_raw = spk_raw
                seg = seg[consumed:].strip()

            # 3) Remaining = quote fragment
            quote_fragment = clean_quote(seg)
            if not is_meaningful(quote_fragment):
                continue

            # 4) Map raw speaker to public label (Couns/Client/Other)
            def map_public_name(name: Optional[str]) -> Optional[str]:
                if not name:
                    return None
                if name.casefold() == counselor_name.casefold():
                    return "Couns"
                if name.casefold() == client_name.casefold():
                    return "Client"
                return name  # keep others as-is

            candidate_public = map_public_name(pending_speaker_raw) or last_speaker_public

            # 5) Anonymize names inside quotes
            quote_fragment = safe_replace_whole_name(quote_fragment, counselor_name, "Couns")
            quote_fragment = safe_replace_whole_name(quote_fragment, client_name, "Client")

            # 6) Append or create row
            if rows and candidate_public and rows[-1]["Speaker"] == candidate_public:
                rows[-1]["Quote"] = (rows[-1]["Quote"] + " " + quote_fragment).strip()
                if rows[-1]["Speaker"] == "Client":
                    rows[-1]["Timestamp"] = ""  # ensure no timestamp for any Client row
            else:
                ts_for_row = pending_timestamp or ""
                speaker_for_row = candidate_public or ""
                if speaker_for_row == "Client":
                    ts_for_row = ""
                rows.append({
                    "Timestamp": ts_for_row,
                    "Speaker": speaker_for_row,
                    "Quote": quote_fragment,
                })
                last_speaker_public = speaker_for_row

            # Tokens consumed after materializing a quote
            pending_timestamp = None
            pending_speaker_raw = None

    # Add Tag column: 'ME' if Couns quote is 3 words or fewer
    for row in rows:
        if row["Speaker"] == "Client":
            row["Timestamp"] = ""  # ensure all Client rows have empty timestamp
        row["Tag"] = "ME" if (row["Speaker"] == "Couns" and word_count(row["Quote"]) <= 3) else ""

    return rows


# ---------------------------
# Streamlit UI
# ---------------------------
st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.caption("This code was generated using ChatGPT by Hunter T. Last updated February 9, 2026.")

with st.sidebar:
    st.header("Settings")
    counselor_name = st.text_input("Counselor name (as it appears in transcript)", placeholder="e.g., Jane D")
    client_name = st.text_input("Client name (as it appears in transcript)", placeholder="e.g., Emily B")
    shift_seconds = st.number_input("Seconds to subtract in case you trimmed the start of your video", min_value=0.0, value=0.0, step=0.5, help="Subtract from all timestamps before formatting to MM:SS")
    st.markdown("---")
    input_method = st.radio("Input method", ["Upload .txt", "Paste text"], index=0)

uploaded_text = None

if input_method == "Upload .txt":
    uploaded_file = st.file_uploader("Upload transcript (.txt)", type=["txt"])
    if uploaded_file is not None:
        uploaded_text = uploaded_file.read().decode("utf-8", errors="ignore")
else:
    uploaded_text = st.text_area("Paste transcript text here", height=260, placeholder="Paste your transcript...")

process_btn = st.button("Parse & Generate")

if process_btn:
    if not uploaded_text:
        st.error("Please upload a .txt file or paste the transcript text.")
    elif not counselor_name or not client_name:
        st.error("Please enter both Counselor and Client names in the sidebar.")
    else:
        # Prepare lines and parse
        lines = uploaded_text.splitlines(keepends=True)
        rows = parse_dialogue_lines(lines, counselor_name, client_name, shift_seconds)

        if not rows:
            st.warning("No dialogue rows were parsed. Check your input and name settings.")
        else:
            df = pd.DataFrame(rows, columns=["Timestamp", "Speaker", "Quote", "Tag"])

            st.subheader("Preview")
            st.dataframe(df, width='stretch', height=360)

            # Write to XLSX (in-memory) with styling for Client rows
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Dialogue")
                ws = writer.sheets["Dialogue"]

                # Style: make Client rows blue
                blue_font = Font(color="0000FF")
                n_rows = len(df)
                n_cols = 4
                for i in range(2, n_rows + 2):  # row 1 is header
                    if ws.cell(row=i, column=2).value == "Client":
                        for col in range(1, n_cols + 1):
                            ws.cell(row=i, column=col).font = blue_font

            output.seek(0)
            suggested_name = "dialogue.xlsx"
            if input_method == "Upload .txt" and uploaded_file is not None:
                base = uploaded_file.name.rsplit(".", 1)[0]
                suggested_name = f"{base}.xlsx"

            st.download_button(
                label="⬇️ Download XLSX",
                data=output.getvalue(),
                file_name=suggested_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            # Quick stats
            total = len(df)
            n_client = int((df["Speaker"] == "Client").sum())
            n_couns = int((df["Speaker"] == "Couns").sum())
            st.info(f"Rows: {total} • Client rows: {n_client} • Couns rows: {n_couns} • Tags (ME): {int((df['Tag']=='ME').sum())}")

st.markdown("---")
st.caption("Tip: Names are anonymized case-insensitively with word boundaries. If your platform formats speakers differently, share a sample and we’ll tune the patterns.")
