"""One-off script: bring a personal copy of the "Subbox Wishlist" sheet up to
date with the latest template structure, formatting, and protection logic.

Targets a spreadsheet the user already copied from the template (shared with
the service account as Editor — see the Instructions tab), re-applies headers,
tab formatting, and the Status/Added protected range exactly as
create_wishlist_sheet_template.py does for the master template, and reuses its
helpers so the two scripts can't drift apart. Existing data rows are left
untouched, and unlike the master template script this one does not make the
spreadsheet public.

Usage:
    python dev_sandbox/update_wishlist_sheet_copy.py /path/to/service-account.json <spreadsheet-id-or-url>
"""

import re
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

from create_wishlist_sheet_template import (
    INSTRUCTIONS_TEXT,
    SCOPES,
    WISHLIST_HEADERS,
    _ensure_sheets,
    _existing_protected_range_ids,
    _format_instructions_tab,
    _format_wishlist_tab,
)


def _extract_spreadsheet_id(id_or_url: str) -> str:
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", id_or_url)
    return match.group(1) if match else id_or_url


def main(credentials_path: str, spreadsheet_id_or_url: str) -> None:
    spreadsheet_id = _extract_spreadsheet_id(spreadsheet_id_or_url)

    credentials = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    sheets = build("sheets", "v4", credentials=credentials)

    sheet_ids = _ensure_sheets(sheets, spreadsheet_id)
    wishlist_id = sheet_ids["Wishlist"]
    instructions_id = sheet_ids["Instructions"]

    # Refresh header row + instructions text only — never touches data rows below row 1.
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": "Wishlist!A1:F1", "values": [WISHLIST_HEADERS]},
            {"range": "Instructions!A1", "values": INSTRUCTIONS_TEXT},
        ]},
    ).execute()

    existing_protected_range_ids = _existing_protected_range_ids(sheets, spreadsheet_id, wishlist_id)
    all_requests = (
        _format_wishlist_tab(wishlist_id, existing_protected_range_ids)
        + _format_instructions_tab(instructions_id)
    )
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": all_requests}
    ).execute()

    print("Done.")
    print(f"Spreadsheet ID: {spreadsheet_id}")
    print(f"URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: update_wishlist_sheet_copy.py <path-to-service-account.json> <spreadsheet-id-or-url>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
