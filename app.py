import streamlit as st
import pandas as pd
import re
from io import BytesIO

st.set_page_config(
    page_title="Transcript → Table Converter",
    page_icon="📝",
    layout="centered"
)

st.title("📝 Transcript → Table Converter")
st.write("Upload a transcript and download a formatted table (Excel).")

uploaded_file = st.file_uploader(
    "Upload transcript (.txt or .vtt)",
    type=["txt", "vtt"]
)

def parse_txt(text):
    """
    Basic parser for transcripts like:
    [Speaker] 12:34
    Text...
    """
    rows = []
    current_speaker = None
    current_timestamp = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        header_match = re.match(r"\[(.+?)\]\s+(\d{1,2}:\d{2}(:\d{2})?)", line)
        if header_match:
            current_speaker = header_match.group(1)
            current_timestamp = header_match.group(2)
        else:
            rows.append({
                "Speaker": current_speaker,
                "Timestamp": current_timestamp,
                "Text": line
            })

    return pd.DataFrame(rows)


def parse_vtt(text):
    """
    Basic VTT parser:
    00:00:01.000 --> 00:00:04.000
    Speaker: text
    """
    rows = []
    lines = text.splitlines()
    timestamp = None

    for line in lines:
        line = line.strip()

        if "-->" in line:
            timestamp = line.split("-->")[0].strip()
            continue

        if ":" in line and timestamp:
            speaker, text_part = line.split(":", 1)
            rows.append({
                "Speaker": speaker.strip(),
                "Timestamp": timestamp,
                "Text": text_part.strip()
            })

    return pd.DataFrame(rows)


def style_excel(df):
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transcript")
        ws = writer.sheets["Transcript"]

        for row in ws.iter_rows(min_row=2):
            speaker = row[0].value
            if speaker:
                if speaker.lower().startswith("host"):
                    for cell in row:
                        cell.font = cell.font.copy(color="1F4FFF")
                else:
                    for cell in row:
                        cell.font = cell.font.copy(color="000000")

        ws.column_dimensions["A"].width = 18
        ws.column_dimensions["B"].width = 14
        ws.column_dimensions["C"].width = 80

    output.seek(0)
    return output


if uploaded_file:
    file_text = uploaded_file.read().decode("utf-8")

    if uploaded_file.name.endswith(".txt"):
        df = parse_txt(file_text)
    else:
        df = parse_vtt(file_text)

    if df.empty:
        st.error("Could not parse transcript.")
    else:
        st.success(f"Parsed {len(df)} rows.")
        st.dataframe(df, use_container_width=True)

        excel_file = style_excel(df)

        st.download_button(
            label="⬇️ Download Excel",
            data=excel_file,
            file_name="transcript_table.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
