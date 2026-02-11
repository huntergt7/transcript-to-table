import io
import re
import html
from typing import List, Dict, Optional, Tuple

import pandas as pd
import streamlit as st
from openpyxl.styles import Font

# =========================
# Toggleable debug flag
# =========================
DEBUG_ON = False  # <-- Set True to default-enable parser trace and internal logging


# =========================
# Parsing helpers & regexes
# =========================

WRAP_CHARS = " \t\r\n\"'{}<>“”‘’"

def _strip_wrappers(s: str) -> str:
    return s.strip(WRAP_CHARS)

def _clean_quote(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^[\-–—:·•*#> ]+", "", s).strip()  # strip leading punctuation/dashes
    s = _strip_wrappers(s)
    s = re.sub(r"\s+", " ", s)
    return s

def _is_meaningful(text: str) -> bool:
    return bool(text and re.search(r"\w", text))

# Timestamp helpers
_TS_TOKEN_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:\.\d+)?$")
_TS_RE_ANYWHERE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\b")

def _to_mmss(mm: int, ss: int, shift_seconds: float = 0.0) -> str:
    total = mm * 60 + ss
    total = int(max(0, total - float(shift_seconds)))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


# ---------- Name normalization & replacement (emoji/punctuation safe) ----------
def _normalize_name_input(name: str) -> str:
    """Trim and collapse internal whitespace to single spaces for consistent matching."""
    name = (name or "").strip()
    name = re.sub(r"\s+", " ", name)
    return name

# Characters we treat as boundaries around a name (no need for \b)
_BOUNDARY = r"[^\S\r\n]|[\-–—:;,/\\\[\]\(\)\{\}“”\"'<>…!?.]"  # whitespace or common punctuation

def _build_name_inner_pattern(name: str) -> Optional[str]:
    """
    Allow any Unicode chars in a name (including emojis).
    Collapse spaces in the NAME to '\s+' so the source can have variable spaces.
    """
    if not name:
        return None
    tokens = _normalize_name_input(name).split()
    # Escape each token literally (preserves emojis), allow \s+ between tokens
    return r"\s+".join(map(re.escape, tokens))

def _whole_name_captor(name: str) -> Optional[re.Pattern]:
    """
    Build a robust, case-insensitive regex that:
      - Matches the full name with flexible internal whitespace (e.g., 'Hun   ter  T')
      - Uses custom boundary chars instead of \b so it works with emojis/punct
      - Captures the leading boundary (if any) to preserve it in substitution
    Pattern shape: (^|B)(NAME)(?=(B|$))
    """
    inner = _build_name_inner_pattern(name)
    if not inner:
        return None
    pattern = rf"(^|{_BOUNDARY})({inner})(?=({_BOUNDARY}|$))"
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)

def _safe_replace_whole_name(text: str, name: str, replacement: str) -> str:
    """
    Replace the WHOLE name with replacement, preserving surrounding punctuation/spacing.
    Works for any Unicode (emojis included).
    """
    pat = _whole_name_captor(name)
    if not pat:
        return text

    def _sub(m: re.Match) -> str:
        prefix = m.group(1) or ""
        # trailing boundary is asserted but not consumed; no need to reproduce it here
        return f"{prefix}{replacement}"

    return pat.sub(_sub, text)


# ---------- Speaker patterns (emoji/punctuation-capable) ----------
# 1) [Name] rest  → capture ANYTHING inside brackets (not closing bracket)
_SPEAKER_BRACKET_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*(?P<rest>.*)$")

# 2) Name (something) rest → capture ANY name (non-greedy) before parentheses
#    Will only be used if the parenthesized 'something' contains a timestamp.
_SPEAKER_PAREN_ANY_RE = re.compile(r"^\s*(?P<name>.+?)\s*\((?P<ts>[^)]*?)\)\s*(?P<rest>.*)$")

# 3) Name :/—/- rest → capture ANY name (non-greedy) before delimiter
_SPEAKER_DELIM_ANY_RE = re.compile(r"^\s*(?P<name>.+?)\s*[:：\-–—]\s+(?P<rest>.*)$")

def _build_bare_token_patterns(counselor_name: str, client_name: str):
    """
    Build patterns to catch lines beginning with bare tokens (no colon), like:
      'Client Yeah, …' or 'Couns Hmm.' or the user's exact name with emojis/punct.
    Returns list of compiled regexes [(label, pattern), ...].
    """
    pats = []

    def add(name: str, label: str):
        name = _normalize_name_input(name)
        if not name:
            return
        inner = _build_name_inner_pattern(name)
        # From start: optional spaces, NAME, then (space|punct|end), then rest
        # Note: we allow optional delimiter immediately after the name as well.
        pattern = rf"^\s*({inner})(?=({_BOUNDARY}|$))\s*(?P<rest>.*)$"
        pats.append((label, re.compile(pattern, re.IGNORECASE | re.UNICODE)))

    # user-provided names
    add(counselor_name, "Couns")
    add(client_name, "Client")
    # already-anonymized tokens
    add("Couns", "Couns")
    add("Client", "Client")

    return pats


# =========================
# Core parser
# =========================

def parse_dialogue_text(
    text: str,
    counselor_name: str,
    client_name: str,
    shift_seconds: float = 0.0,
    allow_bare_tokens: bool = True,
    trace: bool = False
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Parse free-form transcript text into a DataFrame with:
      Timestamp | Speaker | Quote | Tag

    Rules:
    - Normalize timestamps to MM:SS (after subtracting 'shift_seconds')
    - Remove timestamps on Client rows
    - Combine multi-line quotes by the same speaker
    - Tag = 'ME' if a Couns quote has <= 3 words
    - Handle:
        * Name (MM:SS[.ms]) [quote same or next lines]
        * [Name] quote / Name: quote / Name — quote
        * VTT cue lines '... --> ...' (use first timestamp for next spoken line)
        * (Opt) bare tokens at start ('Client ...' / 'Couns ...' / exact name with emojis)
    """
    logs: List[str] = []
    def log(msg):
        if trace:
            logs.append(msg)

    # Normalize inputs to avoid trailing/leading/extra-space issues
    counselor_name = _normalize_name_input(counselor_name)
    client_name    = _normalize_name_input(client_name)

    lines = text.splitlines(keepends=False)
    rows: List[Dict[str, str]] = []
    pending_speaker: Optional[str] = None
    pending_ts: Optional[str] = None

    # Pre-build bare token patterns (emoji-capable)
    bare_pats = _build_bare_token_patterns(counselor_name, client_name) if allow_bare_tokens else []

    def map_public(name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        nm = _normalize_name_input(name).casefold()
        if nm in (counselor_name.casefold(), "couns"):
            return "Couns"
        if nm in (client_name.casefold(), "client"):
            return "Client"
        return name

    for idx, raw in enumerate(lines, start=1):
        line = html.unescape(raw).replace("\ufeff", "").strip()
        if not line:
            continue

        # Skip WEBVTT header
        if line.upper().startswith("WEBVTT"):
            log(f"[L{idx}] Skip WEBVTT header")
            continue

        # Handle WEBVTT cue lines: use the first timestamp only and carry forward
        if "-->" in line:
            anym = _TS_RE_ANYWHERE.search(line)
            if anym:
                if anym.group(3):
                    mm, ss = int(anym.group(2)), int(anym.group(3))
                else:
                    mm, ss = int(anym.group(1)), int(anym.group(2))
                pending_ts = _to_mmss(mm, ss, shift_seconds)
                log(f"[L{idx}] Cue range → carry TS={pending_ts}")
            continue

        # --- Case 1: Name (timestamp) at line start ---
        m = _SPEAKER_PAREN_ANY_RE.match(line)
        if m:
            raw_name, ts_token, rest = m.group("name"), m.group("ts"), m.group("rest")
            # Only treat as a speaker line if the (...) contains a timestamp
            anym = _TS_RE_ANYWHERE.search(ts_token)
            if anym:
                if anym.group(3):
                    mm, ss = int(anym.group(2)), int(anym.group(3))
                else:
                    mm, ss = int(anym.group(1)), int(anym.group(2))
                pending_speaker = _normalize_name_input(raw_name)
                pending_ts = _to_mmss(mm, ss, shift_seconds)
                log(f"[L{idx}] Name(ts) → pending speaker='{pending_speaker}', TS={pending_ts}")

                q = _clean_quote(rest)
                if _is_meaningful(q):
                    speaker_public = map_public(pending_speaker) or ""
                    ts_for_row = "" if speaker_public == "Client" else (pending_ts or "")

                    # Anonymize inside quote (emoji/punct safe)
                    q = _safe_replace_whole_name(q, counselor_name, "Couns")
                    q = _safe_replace_whole_name(q, client_name, "Client")

                    rows.append({"Timestamp": ts_for_row, "Speaker": speaker_public, "Quote": q})
                    log(f"[L{idx}] NEW ROW: {speaker_public} | TS={ts_for_row} | +{len(q)} chars")
                    pending_speaker = None
                    pending_ts = None
                # If no meaningful rest, keep tokens pending for next lines
                continue
            # If no timestamp inside (), fall through to other cases

        # --- Case 2: [Name] ... ---
        m = _SPEAKER_BRACKET_RE.match(line)
        if m:
            raw_name, rest = m.group("name"), m.group("rest")
            pending_speaker = _normalize_name_input(raw_name)

            # Timestamp in rest?
            anym = _TS_RE_ANYWHERE.search(rest)
            if anym:
                if anym.group(3):
                    mm, ss = int(anym.group(2)), int(anym.group(3))
                else:
                    mm, ss = int(anym.group(1)), int(anym.group(2))
                pending_ts = _to_mmss(mm, ss, shift_seconds)
                s, e = anym.span()
                rest = (rest[:s] + " " + rest[e:]).strip()
                log(f"[L{idx}] [Name] with inline ts → pending TS={pending_ts}")

            q = _clean_quote(rest)
            if _is_meaningful(q):
                speaker_public = map_public(pending_speaker) or ""
                ts_for_row = "" if speaker_public == "Client" else (pending_ts or "")

                q = _safe_replace_whole_name(q, counselor_name, "Couns")
                q = _safe_replace_whole_name(q, client_name, "Client")

                rows.append({"Timestamp": ts_for_row, "Speaker": speaker_public, "Quote": q})
                log(f"[L{idx}] NEW ROW: {speaker_public} | TS={ts_for_row} | +{len(q)} chars")
                pending_speaker = None
                pending_ts = None
            continue

        # --- Case 3: Name :/—/- rest ---
        m = _SPEAKER_DELIM_ANY_RE.match(line)
        if m:
            raw_name, rest = m.group("name"), m.group("rest")
            pending_speaker = _normalize_name_input(raw_name)

            # Timestamp in rest?
            anym = _TS_RE_ANYWHERE.search(rest)
            if anym:
                if anym.group(3):
                    mm, ss = int(anym.group(2)), int(anym.group(3))
                else:
                    mm, ss = int(anym.group(1)), int(anym.group(2))
                pending_ts = _to_mmss(mm, ss, shift_seconds)
                s, e = anym.span()
                rest = (rest[:s] + " " + rest[e:]).strip()
                log(f"[L{idx}] Name: with inline ts → pending TS={pending_ts}")

            q = _clean_quote(rest)
            if _is_meaningful(q):
                speaker_public = map_public(pending_speaker) or ""
                ts_for_row = "" if speaker_public == "Client" else (pending_ts or "")

                q = _safe_replace_whole_name(q, counselor_name, "Couns")
                q = _safe_replace_whole_name(q, client_name, "Client")

                rows.append({"Timestamp": ts_for_row, "Speaker": speaker_public, "Quote": q})
                log(f"[L{idx}] NEW ROW: {speaker_public} | TS={ts_for_row} | +{len(q)} chars")
                pending_speaker = None
                pending_ts = None
            continue

        # --- Case 4: bare tokens (optional) e.g., "Client 😅 Yeah, ..." or exact names with emojis ---
        if allow_bare_tokens:
            matched_bare = False
            for label, pat in _build_bare_token_patterns(counselor_name, client_name):
                bm = pat.match(line)
                if bm:
                    rest = bm.group("rest")
                    pending_speaker = label
                    # Timestamp in rest?
                    anym = _TS_RE_ANYWHERE.search(rest)
                    if anym:
                        if anym.group(3):
                            mm, ss = int(anym.group(2)), int(anym.group(3))
                        else:
                            mm, ss = int(anym.group(1)), int(anym.group(2))
                        pending_ts = _to_mmss(mm, ss, shift_seconds)
                        s, e = anym.span()
                        rest = (rest[:s] + " " + rest[e:]).strip()
                        log(f"[L{idx}] Bare '{label}' with inline ts → pending TS={pending_ts}")

                    q = _clean_quote(rest)
                    if _is_meaningful(q):
                        speaker_public = map_public(pending_speaker) or ""
                        ts_for_row = "" if speaker_public == "Client" else (pending_ts or "")
                        q = _safe_replace_whole_name(q, counselor_name, "Couns")
                        q = _safe_replace_whole_name(q, client_name, "Client")
                        rows.append({"Timestamp": ts_for_row, "Speaker": speaker_public, "Quote": q})
                        log(f"[L{idx}] NEW ROW (bare): {speaker_public} | TS={ts_for_row} | +{len(q)} chars")
                        pending_speaker = None
                        pending_ts = None
                    matched_bare = True
                    break
            if matched_bare:
                continue

        # --- Case 5: continuation (no explicit speaker here) ---
        q = _clean_quote(line)
        if not _is_meaningful(q):
            continue

        if rows and (pending_speaker is None):
            # Append to the last row
            prev_len = len(rows[-1]["Quote"])
            rows[-1]["Quote"] = (rows[-1]["Quote"] + " " + q).strip()
            if rows[-1]["Speaker"] == "Client":
                rows[-1]["Timestamp"] = ""  # enforce blank TS for Client
            log(f"[L{idx}] APPEND to {rows[-1]['Speaker']} | +{len(rows[-1]['Quote']) - prev_len} chars")
        else:
            # We have a pending speaker with quote on this line
            speaker_public = map_public(pending_speaker) or ""
            ts_for_row = "" if speaker_public == "Client" else (pending_ts or "")
            q = _safe_replace_whole_name(q, counselor_name, "Couns")
            q = _safe_replace_whole_name(q, client_name, "Client")

            rows.append({"Timestamp": ts_for_row, "Speaker": speaker_public, "Quote": q})
            log(f"[L{idx}] NEW ROW (pending used): {speaker_public} | TS={ts_for_row} | +{len(q)} chars")
            pending_speaker = None
            pending_ts = None

    # Final post-processing: Tag & Client timestamp enforcement
    def _wc(s: str) -> int:
        s = s.strip()
        return 0 if not s else len(re.split(r"\s+", s))

    for r in rows:
        if r["Speaker"] == "Client":
            r["Timestamp"] = ""  # ensure empty TS for all Client rows
        r["Tag"] = "ME" if (r["Speaker"] == "Couns" and _wc(r["Quote"]) <= 3) else ""

    df = pd.DataFrame(rows, columns=["Timestamp", "Speaker", "Quote", "Tag"])
    return df, logs


# =========================
# Streamlit UI
# =========================

st.set_page_config(page_title="Transcript → Counseling Table", page_icon="📝")
st.title("📝 Counseling Transcript Cleaner")
st.caption("This code was generated using ChatGPT by Hunter T. _Last updated February 10, 2026._")

with st.sidebar:
    st.header("Settings")
    counselor_name_input = st.text_input("Counselor name (as it appears in transcript)", placeholder="e.g., Hunter T or Dr. 🐟-Smith, Jr.")
    client_name_input    = st.text_input("Client name (as it appears in transcript)", placeholder="e.g., Renée B ✨")
    # Normalize immediately to avoid trailing space issues
    counselor_name = _normalize_name_input(counselor_name_input)
    client_name    = _normalize_name_input(client_name_input)

    shift_seconds = st.number_input(
        "Seconds to subtract", min_value=0.0, value=0.0, step=0.5,
        help="Subtract before formatting timestamps to MM:SS"
    )

    allow_bare = st.checkbox(
        "Treat 'Couns'/'Client' (or the names) at start as speaker even without ':'",
        value=True
    )

    # Debug checkbox defaults from DEBUG_ON
    show_trace = st.checkbox("Show parser trace (debug)", value=DEBUG_ON)

    st.markdown("---")
    input_method = st.radio("Input method", ["Upload .txt", "Paste text"], index=0)

uploaded_text = None
uploaded_file = None

if input_method == "Upload .txt":
    uploaded_file = st.file_uploader("Upload transcript (.txt)", type=["txt"])
    if uploaded_file is not None:
        uploaded_text = uploaded_file.read().decode("utf-8", errors="ignore")
else:
    uploaded_text = st.text_area("Paste transcript text here", height=260, placeholder="Paste your transcript…")

if st.button("Parse & Generate"):
    if not uploaded_text:
        st.error("Please upload a .txt file or paste the transcript text.")
    elif not counselor_name or not client_name:
        st.error("Please enter both Counselor and Client names in the sidebar.")
    else:
        df, logs = parse_dialogue_text(
            uploaded_text,
            counselor_name=counselor_name,
            client_name=client_name,
            shift_seconds=shift_seconds,
            allow_bare_tokens=allow_bare,
            trace=show_trace
        )

        if df.empty:
            st.warning("No dialogue rows were parsed. Check your input and name settings.")
        else:
            st.subheader("Preview")
            st.dataframe(df, use_container_width=True, height=360)

            # Write to XLSX (in-memory) with blue font for Client rows
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Dialogue")
                ws = writer.sheets["Dialogue"]
                blue_font = Font(color="0000FF")
                for i in range(2, len(df) + 2):  # row 1 is header
                    if ws.cell(row=i, column=2).value == "Client":
                        for col in range(1, 5):  # 4 columns
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

            # Also offer a clean CSV export
            st.download_button(
                label="⬇️ Download CSV",
                data=df.to_csv(index=False).encode("utf-8"),
                file_name=suggested_name.replace(".xlsx", ".csv"),
                mime="text/csv",
            )

            # Quick stats
            total = len(df)
            n_client = int((df["Speaker"] == "Client").sum())
            n_couns  = int((df["Speaker"] == "Couns").sum())
            st.info(f"Rows: {total} • Client rows: {n_client} • Couns rows: {n_couns} • Tags (ME): {int((df['Tag']=='ME').sum())}")

        if show_trace and logs:
            st.subheader("Parser Trace (debug)")
            st.code("\n".join(logs)[:100000], language="text")
