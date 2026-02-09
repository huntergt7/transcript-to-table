import streamlit as st
import pandas as pd
import re
from io import BytesIO
from openpyxl.styles import Font

# ---------------- PAGE SETUP ----------------
st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.subtitle("This code was generated using ChatGPT by Hunter T. Last updated February 9, 2026.")

# ---------------- SESSION STATE ----------------
if "ready_to_download" not in st.session_state:
    st.session_state.ready_to_download = False

if "offset_seconds" not in st.session_state:
    st.session_state.offset_seconds = 0

if "uploaded" not in st.session_state:
    st.session_state.uploaded = False

# ---------------- RESET ----------------
def reset_app():
    st.session_state.ready_to_download = False
    st.session_state.uploaded = False
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
    "Upload transcript (Zoom or Riverside)",
    type=["txt", "vtt"]
)

# ---------------- DETECTION ----------------
def detect_transcript_type(text):
    if "WEBVTT" in text or "-->" in text:
        return "vtt"
    if re.search(r"\[.+?\]\s+\d{1,2}:\d{2}", text):
        return "zoom"
    return "generic"

# ---------------- PARSERS ----------------
def parse_zoom_txt(text):
    rows = []
    speaker = None
    timestamp = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        match = re.match(r"\[(.+?)\]\s+(\d{1,2}:\d{2}(:\d{2})?)", line)
        if match:
            speaker = match.group(1)
            timestamp = match.group(2)
        else:
            rows.append({
                "Speaker": speaker,
                "Timestamp": timestamp,
                "Text": line
            })

    return pd.DataFrame(rows)

def parse_vtt(text):
    rows = []
    timestamp = None

    for line in text.splitlines():
        line = line.strip()

        if "-->" in line:
            timestamp = line.split("-->")[0].strip().split(".")[0]
            continue

        if ":" in line and timestamp:
            speaker, text_part = line.split(":", 1)
            rows.append({
                "Speaker": speaker.strip(),
                "Timestamp": timestamp,
                "Text": text_part.strip()
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
        lambda n: "Couns." if counselor_name.lower() in n.lower() else "Client"
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
    st.session_state.uploaded = True

    counselor_name = st.text_input(
        "Enter the counselor’s full name as it appears in the transcript"
    )

    text = uploaded_file.read().decode("utf-8")
    df = parse_vtt(text) if detect_transcript_type(text) == "vtt" else parse_zoom_txt(text)

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
