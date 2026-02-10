import streamlit as st
import pandas as pd
import re
from io import BytesIO
from openpyxl.styles import Font

# ---------------- PAGE ----------------
st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.caption("This webpage was developed with ChatGPT by Hunter T. _Last updated February 10, 2026._")

# ---------------- SESSION STATE ----------------
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

SPEAKER_RE = re.compile(
    r"^\[?([A-Za-z][A-Za-z .'\-]{1,40})\]?\s*(\d{1,2}:\d{2}(?::\d{2})?)?"
)

# ---------------- PARSER ----------------
def parse_transcript(text: str) -> pd.DataFrame:
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
        try:
            line = raw.strip()
        except Exception:
            continue

        if not line:
            continue

        speaker_match = SPEAKER_RE.match(line)

        if speaker_match:
            flush()

            try:
                current_speaker = speaker_match.group(1).strip()
            except Exception:
                current_speaker = None

            try:
                current_timestamp = speaker_match.group(2)
            except Exception:
                current_timestamp = None

            continue

        # Standalone timestamp line
        try:
            ts_match = TIMESTAMP_RE.search(line)
        except Exception:
            ts_match = None

        if ts_match and current_timestamp is None:
            current_timestamp = ts_match.group(1)
            continue

        buffer.append(line)

    flush()
    return pd.DataFrame(rows)

# ---------------- TIME HANDLING ----------------
def to_seconds(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        parts = list(map(int, ts.split(":")))
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    except Exception:
        return None

def from_seconds(seconds):
    try:
        seconds = max(0, int(seconds))
    except Exception:
        return ""

    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)

    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

# ---------------- TRANSFORMS ----------------
def normalize_speakers(df, counselor_name):
    def norm(name):
        try:
            return "Couns." if counselor_name.lower() in name.lower() else "Client"
        except Exception:
            return "Client"

    df["Speaker"] = df["Speaker"].apply(norm)
    return df

def apply_offset(df):
    def offset_ts(ts):
        secs = to_seconds(ts)
        if secs is None:
            return ts
        return from_seconds(secs - offset)

    try:
        mask = df["Speaker"] == "Couns."
        df.loc[mask, "Timestamp"] = df.loc[mask, "Timestamp"].apply(offset_ts)
    except Exception:
        pass

    return df

def merge_sequential(df):
    merged = []
    prev = None

    for _, row in df.iterrows():
        try:
            if prev and row["Speaker"] == prev["Speaker"]:
                prev["Text"] += " " + row["Text"]
            else:
                if prev:
                    merged.append(prev)
                prev = row.to_dict()
        except Exception:
            continue

    if prev:
        merged.append(prev)

    return pd.DataFrame(merged)

def finalize(df):
    try:
        df.loc[df["Speaker"] == "Client", "Timestamp"] = ""
    except Exception:
        pass

    def me_flag(row):
        try:
            return "ME" if row["Speaker"] == "Couns." and len(row["Text"].split()) <= 3 else ""
        except Exception:
            return ""

    df["ME"] = df.apply(me_flag, axis=1)

    return df[["Timestamp", "Speaker", "Text", "ME"]]

# ---------------- EXCEL ----------------
def make_excel(df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
        ws = writer.sheets["Sheet1"]

        for row in ws.iter_rows(min_row=2):
            try:
                if row[1].value == "Client":
                    for cell in row:
                        cell.font = Font(color="1F4FFF")
            except Exception:
                continue

        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 90
        ws.column_dimensions["D"].width = 5

    output.seek(0)
    return output

# ---------------- MAIN ----------------
if uploaded:
    counselor = st.text_input("Enter counselor’s full name as it appears in the transcript")

    if counselor:
        try:
            text = uploaded.read().decode("utf-8")
        except Exception:
            st.error("Could not read uploaded file.")
            st.stop()

        df = parse_transcript(text)

        if not df.empty:
            df = normalize_speakers(df, counselor)
            df = apply_offset(df)
            df = merge_sequential(df)
            df = finalize(df)

            st.dataframe(df, use_container_width=True)

            excel = make_excel(df)
            st.download_button(
                "⬇️ Download Excel",
                excel,
                file_name="counseling_transcript.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            st.button("🔄 Start new upload", on_click=reset_app)
        else:
            st.error("No usable speaker blocks were detected.")
