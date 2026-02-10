import streamlit as st
import pandas as pd
import re
from io import BytesIO
from openpyxl.styles import Font

# ---------------- PAGE ----------------
st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.caption("This webpage was developed with ChatGPT by Hunter T. _Last updated February 9, 2026._")

# ---------------- SESSION ----------------
for key, default in {
    "ready": False,
    "offset": 0,
    "file_key": 0
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------- RESET ----------------
def reset_app():
    st.session_state.ready = False
    st.session_state.offset = 0
    st.session_state.file_key += 1

# ---------------- INPUTS ----------------
offset = st.number_input(
    "How many seconds should be removed from counselor timestamps?",
    min_value=0,
    max_value=3600,
    step=1,
    key="offset"
)

uploaded = st.file_uploader(
    "Upload transcript",
    type=["txt", "vtt"],
    key=f"uploader_{st.session_state.file_key}"
)

# ---------------- REGEX ----------------
TIMESTAMP_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
SPEAKER_RE = re.compile(r"^\[?([A-Za-z][A-Za-z .'-]{1,40})\]?\s*[:\-]")

# ---------------- PARSER ----------------
def parse_transcript(text):
    rows = []
    current_speaker = None
    current_timestamp = None
    buffer = []

    def flush():
        nonlocal buffer
        if current_speaker and buffer:
            rows.append({
                "Speaker": current_speaker,
                "Timestamp": current_timestamp,
                "Text": " ".join(buffer).strip()
            })
        buffer = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        # Detect speaker line (REQUIRED to start a new row)
        speaker_match = re.match(
            r"^\[?([A-Za-z][A-Za-z .'-]{1,40})\]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?",
            line
        )

        if speaker_match:
            # New speaker → flush previous row
            flush()

            current_speaker = speaker_match.group(1).strip()
            current_timestamp = speaker_match.group(2)

            continue  # next lines are dialogue

        # Detect standalone timestamp lines
        ts_match = TIMESTAMP_RE.search(line)
        if ts_match and current_timestamp is None:
            current_timestamp = ts_match.group(1)
            continue

        # Otherwise: dialogue line
        buffer.append(line)

    flush()
    return pd.DataFrame(rows)

# ---------------- TIME ----------------
def to_seconds(ts):
    parts = list(map(int, ts.split(":")))
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))

def from_seconds(s):
    s = max(0, s)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

# ---------------- TRANSFORMS ----------------
def normalize(df, counselor):
    df["Speaker"] = df["Speaker"].apply(
        lambda x: "Couns." if counselor.lower() in x.lower() else "Client"
    )
    return df

def apply_offset(df):
    df.loc[df["Speaker"] == "Couns.", "Timestamp"] = df.loc[
        df["Speaker"] == "Couns.", "Timestamp"
    ].apply(lambda t: from_seconds(to_seconds(t) - offset))
    return df

def merge_sequential(df):
    out, prev = [], None
    for _, r in df.iterrows():
        if prev and r["Speaker"] == prev["Speaker"]:
            prev["Text"] += " " + r["Text"]
        else:
            if prev:
                out.append(prev)
            prev = r.to_dict()
    if prev:
        out.append(prev)
    return pd.DataFrame(out)

def finalize(df):
    df.loc[df["Speaker"] == "Client", "Timestamp"] = ""
    df["ME"] = df.apply(
        lambda r: "ME" if r["Speaker"] == "Couns." and len(r["Text"].split()) <= 3 else "",
        axis=1
    )
    return df[["Timestamp", "Speaker", "Text", "ME"]]

# ---------------- EXCEL ----------------
def make_excel(df):
    bio = BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
        ws = writer.sheets["Sheet1"]

        for row in ws.iter_rows(min_row=2):
            if row[1].value == "Client":
                for cell in row:
                    cell.font = Font(color="1F4FFF")

        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 90
        ws.column_dimensions["D"].width = 5

    bio.seek(0)
    return bio

# ---------------- MAIN ----------------
if uploaded:
    counselor = st.text_input("Enter counselor’s full name")

    if counselor:
        text = uploaded.read().decode("utf-8")
        df = parse_transcript(text)

        if not df.empty:
            df = normalize(df, counselor)
            df = apply_offset(df)
            df = merge_sequential(df)
            df = finalize(df)

            st.dataframe(df, width='stretch')

            excel = make_excel(df)
            st.download_button(
                "⬇️ Download Excel",
                excel,
                file_name="counseling_transcript.xlsx"
            )

            st.button("🔄 Start new upload", on_click=reset_app)
        else:
            st.error("Transcript could not be parsed.")
