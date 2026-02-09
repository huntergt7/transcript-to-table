import streamlit as st
import pandas as pd
import re
from io import BytesIO
from openpyxl.styles import Font

# ---------------- PAGE SETUP ----------------
st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.caption("This code was generated using ChatGPT by Hunter T. Last updated February 9, 2026.")

# ---------------- SESSION STATE ----------------
if "ready_to_download" not in st.session_state:
    st.session_state.ready_to_download = False

if "offset_seconds" not in st.session_state:
    st.session_state.offset_seconds = 0

# ---------------- RESET ----------------
def reset_app():
    st.session_state.ready_to_download = False
    st.session_state.offset_seconds = 0
    st.rerun()

# ---------------- CONTROLS ----------------
offset_seconds = st.number_input(
    "How many seconds should be removed from counselor timestamps?",
    min_value=0,
    max_value=3600,
    step=1,
    key="offset_seconds"
)

uploaded_file = st.file_uploader(
    "Upload transcript (any standard format)",
    type=["txt", "vtt"]
)

# ---------------- UNIVERSAL PARSER ----------------

TIMESTAMP_REGEX = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})(?:\.\d+)?"
)

def parse_transcript(text):
    rows = []
    current_speaker = None
    current_timestamp = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 1️⃣ Extract timestamp if present
        ts_match = TIMESTAMP_REGEX.search(line)
        if ts_match:
            current_timestamp = ts_match.group(1)
            line = line.replace(ts_match.group(0), "").strip(" -:()[]")

        # 2️⃣ Extract speaker if present
        speaker = None

        # Patterns like "John Smith:" or "[John Smith]"
        speaker_match = re.match(r"^\[?([A-Za-z][A-Za-z .'-]{1,50})\]?\s*[:\-]", line)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            line = line[speaker_match.end():].strip()
        else:
            # Patterns like "John Smith says..."
            possible = line.split(":")
            if len(possible) > 1 and len(possible[0].split()) <= 4:
                speaker = possible[0].strip()
                line = possible[1].strip()

        if speaker:
            current_speaker = speaker

        # Remaining line is dialogue
        if line:
            rows.append({
                "Speaker": current_speaker,
                "Timestamp": current_timestamp,
                "Text": line
            })

    return pd.DataFrame(rows)

# ---------------- TIMESTAMPS ----------------
def timestamp_to_seconds(ts):
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0

def seconds_to_timestamp(seconds):
    seconds = max(0, seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def apply_offset(ts, offset):
    try:
        return seconds_to_timestamp(timestamp_to_seconds(ts) - offset)
    except:
        return ts

# ---------------- TRANSFORMS ----------------
def normalize_speakers(df, counselor_name):
    df["Speaker"] = df["Speaker"].apply(
        lambda n: "Couns." if counselor_name.lower() in (n or "").lower() else "Client"
    )
    return df

def merge_sequential_by_speaker(df):
    merged = []
    prev = None

    for _, row in df.iterrows():
        if prev and row["Speaker"] == prev["Speaker"]:
            prev["Text"] += " " + row["Text"]
        else:
            if prev:
                merged.append(prev)
            prev = row.to_dict()

    if prev:
        merged.append(prev)

    return pd.DataFrame(merged)

def clear_client_timestamps(df):
    df.loc[df["Speaker"] == "Client", "Timestamp"] = ""
    return df

def add_me_column(df):
    df["ME"] = df.apply(
        lambda r: "ME"
        if r["Speaker"] == "Couns." and len(r["Text"].split()) <= 3
        else "",
        axis=1
    )
    return df

# ---------------- EXCEL ----------------
def style_excel(df):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transcript")
        ws = writer.sheets["Transcript"]

        for row in ws.iter_rows(min_row=2):
            if row[1].value == "Client":
                for cell in row:
                    cell.font = Font(color="1F4FFF")

        ws.column_dimensions["A"].width = 14
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 90
        ws.column_dimensions["D"].width = 6

    output.seek(0)
    return output

# ---------------- MAIN ----------------
if uploaded_file:
    counselor_name = st.text_input(
        "Enter the counselor’s full name as it appears in the transcript"
    )

    text = uploaded_file.read().decode("utf-8")
    df = parse_transcript(text)

    if counselor_name and not df.empty:
        df["Timestamp"] = df["Timestamp"].apply(
            lambda ts: apply_offset(ts, st.session_state.offset_seconds) if pd.notna(ts) else ts
        )

        df = normalize_speakers(df, counselor_name)
        df = merge_sequential_by_speaker(df)
        df = clear_client_timestamps(df)
        df = add_me_column(df)

        df = df[["Timestamp", "Speaker", "Text", "ME"]]

        st.dataframe(df, use_container_width=True)

        if not st.session_state.ready_to_download:
            excel = style_excel(df)
            st.download_button(
                "⬇️ Download Excel",
                excel,
                file_name="counseling_transcript.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            st.session_state.ready_to_download = True

        st.button("🔄 Start new upload", on_click=reset_app)

    elif not counselor_name:
        st.info("Enter the counselor’s name to continue.")
    else:
        st.error("Could not parse transcript.")
