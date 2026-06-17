"""One-off script: populate the "Subbox Wishlist" template Google Sheet.

Targets an existing spreadsheet (shared with the service account as Editor),
sets up a "Wishlist" tab (column headers + formatting) and an "Instructions"
tab, then makes it readable by anyone with the link so users can hit the
Sheets "make a copy" URL.

Usage:
    python dev_sandbox/create_wishlist_sheet_template.py /path/to/service-account.json
"""

import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = "1AUS_1Y5xo-HUoGxQkVQRsZFQMYZ6w8KsNeI4VOP9t64"

WISHLIST_HEADERS = ["Raw Note", "Artist", "Title", "YouTube URL", "Status", "Added"]

# Column index constants
COL_RAW_NOTE = 0
COL_ARTIST = 1
COL_TITLE = 2
COL_YOUTUBE = 3
COL_STATUS = 4
COL_ADDED = 5

# Colour palette
# Header row (darker, white text)
H_NOTE   = {"red": 0.576, "green": 0.439, "blue": 0.859}   # purple
H_INPUT  = {"red": 0.267, "green": 0.549, "blue": 0.792}   # blue  (Artist + Title)
H_URL    = {"red": 0.204, "green": 0.659, "blue": 0.325}   # green (YouTube URL)
H_SUBBOX = {"red": 0.400, "green": 0.400, "blue": 0.400}   # dark grey (Status + Added)

# Data rows (very light tint)
D_NOTE   = {"red": 0.949, "green": 0.906, "blue": 0.988}
D_INPUT  = {"red": 0.882, "green": 0.929, "blue": 0.980}
D_URL    = {"red": 0.851, "green": 0.957, "blue": 0.890}
D_SUBBOX = {"red": 0.941, "green": 0.941, "blue": 0.941}

WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

HEADER_NOTES = {
    COL_RAW_NOTE: (
        "Quick capture — write anything here when you don't have time to look up "
        "the full details.\n\n"
        "Example: \"track from black mirror s3 ep1\" or \"that song from the coffee shop\"\n\n"
        "Subbox will add it to your wishlist as a 'needs info' item. You can fill in "
        "Artist + Title later from inside Subbox."
    ),
    COL_ARTIST: (
        "Artist name.\n\n"
        "Fill this in together with Title for a structured entry. "
        "Subbox will search YouTube to find the best match.\n\n"
        "Leave blank if you're using Raw Note or YouTube URL instead."
    ),
    COL_TITLE: (
        "Track title.\n\n"
        "Fill this in together with Artist. "
        "Subbox will search YouTube to find the best match.\n\n"
        "Leave blank if you're using Raw Note or YouTube URL instead."
    ),
    COL_YOUTUBE: (
        "YouTube URL — paste a direct link to the video.\n\n"
        "Subbox will use this exact video, no matching needed. "
        "Supports youtube.com/watch, youtu.be, and Shorts links.\n\n"
        "Leave blank if you're using Artist + Title or Raw Note instead."
    ),
    COL_STATUS: (
        "⚠️ Written by Subbox — do not edit.\n\n"
        "Subbox writes 'Added' here once it has picked up and saved this row. "
        "If something goes wrong, an error message appears here instead."
    ),
    COL_ADDED: (
        "⚠️ Written by Subbox — do not edit.\n\n"
        "Subbox writes the date and time it processed this row."
    ),
}

INSTRUCTIONS_TEXT = [
    ["Subbox Offline Wishlist — Instructions"],
    [""],
    ["This sheet is your offline wishlist capture. Switch to the 'Wishlist' tab"],
    ["and add a row whenever you think of a track you want — even offline."],
    ["Google Sheets queues your edits and syncs them once you're back online."],
    [""],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    ["HOW TO ADD A TRACK"],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    [""],
    ["For each row, fill in exactly ONE of the following options:"],
    [""],
    ["  🟣  OPTION 1 — Raw Note (fastest, no internet needed)"],
    ["     Column: Raw Note"],
    ["     Jot down anything — even vague. Subbox adds it as a 'needs info' item"],
    ["     for you to fill in properly later from inside the app."],
    ["     Example: \"track from black mirror s3 ep1\""],
    [""],
    ["  🔵  OPTION 2 — Artist + Title (structured entry)"],
    ["     Columns: Artist  +  Title"],
    ["     Subbox fuzzy-matches this against YouTube to find the right video."],
    ["     Example: Artist = \"Bicep\",  Title = \"Glue\""],
    [""],
    ["  🟢  OPTION 3 — YouTube URL (most precise)"],
    ["     Column: YouTube URL"],
    ["     Paste the link. Subbox uses that exact video, no matching needed."],
    ["     Supports youtube.com/watch, youtu.be, and Shorts links."],
    [""],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    ["STATUS COLUMN"],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    [""],
    ["The grey 'Status' and 'Added' columns are written by Subbox automatically."],
    ["Do not edit them. Once Subbox picks up a row it writes 'Added' in Status."],
    ["If there's an error, a short message appears there instead."],
    [""],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    ["ONE-TIME SETUP (do this once after copying the template)"],
    ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"],
    [""],
    ["  1. Click File > Make a copy to create your own copy of this sheet."],
    ["  2. In your copy, click Share and add the Subbox service account email"],
    ["     shown in the app as an Editor."],
    ["  3. Back in Subbox, open the 'Offline Wishlist' dialog, paste your"],
    ["     copy's URL, and click Save."],
    [""],
    ["Subbox will poll your sheet every few minutes and import new rows."],
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell_range(sheet_id, r0, r1, c0, c1):
    return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def _repeat_bg(sheet_id, r0, r1, c0, c1, bg):
    return {"repeatCell": {
        "range": _cell_range(sheet_id, r0, r1, c0, c1),
        "cell": {"userEnteredFormat": {"backgroundColor": bg}},
        "fields": "userEnteredFormat.backgroundColor",
    }}


def _note_request(sheet_id, row, col, note):
    return {"updateCells": {
        "rows": [{"values": [{"note": note}]}],
        "fields": "note",
        "range": _cell_range(sheet_id, row, row + 1, col, col + 1),
    }}


def _col_width(sheet_id, c0, c1, px):
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                  "startIndex": c0, "endIndex": c1},
        "properties": {"pixelSize": px},
        "fields": "pixelSize",
    }}


# ---------------------------------------------------------------------------
# Sheet setup helpers
# ---------------------------------------------------------------------------

def _ensure_sheets(sheets_svc, spreadsheet_id):
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    add_requests = []
    for title in ("Wishlist", "Instructions"):
        if title not in existing:
            add_requests.append({"addSheet": {"properties": {"title": title}}})

    if add_requests:
        result = sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body={"requests": add_requests}
        ).execute()
        for reply in result.get("replies", []):
            props = reply.get("addSheet", {}).get("properties", {})
            if props:
                existing[props["title"]] = props["sheetId"]

    # Rename default "Sheet1" to "Wishlist" if needed
    if "Sheet1" in existing and "Wishlist" not in existing:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": existing["Sheet1"], "title": "Wishlist"},
                "fields": "title",
            }}]},
        ).execute()
        existing["Wishlist"] = existing.pop("Sheet1")

    return existing


def _format_wishlist_tab(wid, service_account_email):
    """Return all batchUpdate requests for the Wishlist tab."""
    DATA_ROWS = 1000  # apply tints this far down

    requests = []

    # ---- Header row backgrounds (per column) ----
    requests += [
        _repeat_bg(wid, 0, 1, COL_RAW_NOTE, COL_RAW_NOTE + 1, H_NOTE),
        _repeat_bg(wid, 0, 1, COL_ARTIST,   COL_ARTIST + 1,   H_INPUT),
        _repeat_bg(wid, 0, 1, COL_TITLE,    COL_TITLE + 1,    H_INPUT),
        _repeat_bg(wid, 0, 1, COL_YOUTUBE,  COL_YOUTUBE + 1,  H_URL),
        _repeat_bg(wid, 0, 1, COL_STATUS,   COL_STATUS + 1,   H_SUBBOX),
        _repeat_bg(wid, 0, 1, COL_ADDED,    COL_ADDED + 1,    H_SUBBOX),
    ]

    # ---- Header row text: bold + white ----
    requests.append({"repeatCell": {
        "range": _cell_range(wid, 0, 1, 0, 6),
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP",
        }},
        "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
    }})

    # ---- Header row height ----
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": wid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 40},
        "fields": "pixelSize",
    }})

    # ---- Freeze header row ----
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": wid, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})

    # ---- Data row tints (rows 2 … DATA_ROWS+1) ----
    requests += [
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_RAW_NOTE, COL_RAW_NOTE + 1, D_NOTE),
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_ARTIST,   COL_ARTIST + 1,   D_INPUT),
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_TITLE,    COL_TITLE + 1,    D_INPUT),
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_YOUTUBE,  COL_YOUTUBE + 1,  D_URL),
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_STATUS,   COL_STATUS + 1,   D_SUBBOX),
        _repeat_bg(wid, 1, DATA_ROWS + 1, COL_ADDED,    COL_ADDED + 1,    D_SUBBOX),
    ]

    # ---- Data row text: wrap + top-align ----
    requests.append({"repeatCell": {
        "range": _cell_range(wid, 1, DATA_ROWS + 1, 0, 6),
        "cell": {"userEnteredFormat": {
            "wrapStrategy": "WRAP",
            "verticalAlignment": "TOP",
        }},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
    }})

    # ---- Column widths ----
    requests += [
        _col_width(wid, COL_RAW_NOTE, COL_RAW_NOTE + 1, 280),
        _col_width(wid, COL_ARTIST,   COL_ARTIST + 1,   180),
        _col_width(wid, COL_TITLE,    COL_TITLE + 1,    180),
        _col_width(wid, COL_YOUTUBE,  COL_YOUTUBE + 1,  280),
        _col_width(wid, COL_STATUS,   COL_STATUS + 1,   130),
        _col_width(wid, COL_ADDED,    COL_ADDED + 1,    190),
    ]

    # ---- Header cell notes (hover tooltips) ----
    for col, note in HEADER_NOTES.items():
        requests.append(_note_request(wid, 0, col, note))

    # ---- Protected range: Status + Added ----
    # Enforced: only Subbox's service account may edit. The spreadsheet owner
    # can still lift the restriction via Data > Protected sheets and ranges.
    requests.append({"addProtectedRange": {
        "protectedRange": {
            "range": _cell_range(wid, 0, DATA_ROWS + 1, COL_STATUS, COL_ADDED + 1),
            "description": "Written by Subbox automatically — do not edit",
            "warningOnly": False,
            "editors": {"users": [service_account_email]},
        }
    }})

    return requests


def _format_instructions_tab(iid):
    """Return all batchUpdate requests for the Instructions tab."""
    requests = []

    # Title row: large bold white text on dark background
    requests.append({"repeatCell": {
        "range": _cell_range(iid, 0, 1, 0, 1),
        "cell": {"userEnteredFormat": {
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
            "textFormat": {"bold": True, "fontSize": 14, "foregroundColor": WHITE},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
    }})

    # Title row height
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": iid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 50},
        "fields": "pixelSize",
    }})

    # Section headings (rows containing ━━━ separators): bold
    separator_rows = [r for r, row in enumerate(INSTRUCTIONS_TEXT) if row and "━" in row[0]]
    heading_rows   = [r + 1 for r in separator_rows]  # line after each separator
    for row_idx in heading_rows:
        if row_idx < len(INSTRUCTIONS_TEXT):
            requests.append({"repeatCell": {
                "range": _cell_range(iid, row_idx, row_idx + 1, 0, 1),
                "cell": {"userEnteredFormat": {
                    "textFormat": {"bold": True, "fontSize": 11},
                }},
                "fields": "userEnteredFormat.textFormat",
            }})

    # Body: wrap + readable font size
    requests.append({"repeatCell": {
        "range": _cell_range(iid, 0, len(INSTRUCTIONS_TEXT) + 1, 0, 1),
        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
    }})

    # Wide column
    requests.append(_col_width(iid, 0, 1, 640))

    return requests


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(credentials_path: str) -> None:
    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    sheets = build("sheets", "v4", credentials=credentials)
    drive  = build("drive",  "v3", credentials=credentials)

    spreadsheet_id = SPREADSHEET_ID
    sheet_ids = _ensure_sheets(sheets, spreadsheet_id)

    wishlist_id     = sheet_ids["Wishlist"]
    instructions_id = sheet_ids["Instructions"]

    # Write cell values
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": "Wishlist!A1:F1",  "values": [WISHLIST_HEADERS]},
            {"range": "Instructions!A1", "values": INSTRUCTIONS_TEXT},
        ]},
    ).execute()

    # Apply all formatting in one batchUpdate
    all_requests = (
        _format_wishlist_tab(wishlist_id, credentials.service_account_email)
        + _format_instructions_tab(instructions_id)
    )
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": all_requests}
    ).execute()

    # Make publicly readable (so /copy URL works for any user)
    drive.permissions().create(
        fileId=spreadsheet_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()

    print("Done.")
    print(f"Spreadsheet ID: {spreadsheet_id}")
    print(f"URL:      https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
    print(f"Copy URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/copy")
    print()
    print("Service account email (this is the email users share their copy with):")
    print(f"  {credentials.service_account_email}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: create_wishlist_sheet_template.py <path-to-service-account.json>")
        sys.exit(1)
    main(sys.argv[1])
