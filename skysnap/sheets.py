from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from skysnap.sheet_rows import normalize_header


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

@dataclass(frozen=True)
class SheetAppendResult:
    updated_range: str | None


def _activation_url_from_http_error(err: HttpError) -> str | None:
    content = err.content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not content:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for detail in payload.get("error", {}).get("details", []):
            if not isinstance(detail, dict):
                continue
            meta = detail.get("metadata") or {}
            url = meta.get("activationUrl")
            if url:
                return str(url)
    match = re.search(r"https://console\.developers\.google\.com/apis/api/[^\s\"]+", content)
    return match.group(0) if match else None


def _column_letter(col: int) -> str:
    """Convert 1-based column index to A1 notation (1=A, 26=Z, 27=AA)."""
    if col < 1:
        raise ValueError("column index must be >= 1")
    letters = ""
    n = col
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _http_error_reason(err: HttpError) -> str | None:
    content = err.content
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    try:
        payload = json.loads(content) if content else {}
        if isinstance(payload, dict):
            err_obj = payload.get("error") or {}
            return str(err_obj.get("status") or err_obj.get("message") or "")
    except json.JSONDecodeError:
        pass
    return None


def _raise_friendly_sheets_error(err: HttpError, *, service_account_email: str | None = None) -> None:
    status = getattr(err.resp, "status", None)
    reason = _http_error_reason(err)
    activation = _activation_url_from_http_error(err)
    if status == 403 and activation:
        raise ValueError(
            "Google Sheets API is disabled for your service account's GCP project. "
            f"Enable it here, wait 1-2 minutes, then retry: {activation}"
        ) from err
    if status == 403 and reason == "PERMISSION_DENIED":
        email = service_account_email or "your service account email"
        raise ValueError(
            f"Google Sheets write access denied. Share the spreadsheet with {email} "
            "as Editor (Viewer is not enough). In Google Sheets: Share → add that email → Editor."
        ) from err
    if status == 403:
        raise ValueError(
            "Google Sheets access denied (403). Enable the Sheets API in Google Cloud Console, "
            "and share the spreadsheet with the service account email (Editor)."
        ) from err
    if status == 404:
        raise ValueError(
            "Spreadsheet or tab not found (404). Check GOOGLE_SHEET_ID and GOOGLE_SHEET_TAB_NAME."
        ) from err
    if status == 400 and reason and "exceeds grid limits" in reason.lower():
        raise ValueError(
            f"Sheet range out of bounds (400): {reason}. "
            "Check GOOGLE_SHEET_TAB_NAME matches an existing tab."
        ) from err
    raise err


class GoogleSheetsClient:
    def __init__(self, *, service_account_json_path: str) -> None:
        if not service_account_json_path:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is required")
        path = Path(service_account_json_path)
        if not path.is_file():
            raise ValueError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON file not found: {path.resolve()}. "
                "Download the service account JSON from Google Cloud Console."
            )
        creds = Credentials.from_service_account_file(str(path), scopes=SCOPES)
        self._service_account_email = creds.service_account_email
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    @property
    def service_account_email(self) -> str:
        return self._service_account_email

    def _execute(self, request: Any) -> Any:
        try:
            return request.execute()
        except HttpError as e:
            _raise_friendly_sheets_error(e, service_account_email=self._service_account_email)

    def get_headers(self, *, spreadsheet_id: str, tab_name: str) -> list[str]:
        """Read row 1 using the tab's full column width (preserves empty leading columns)."""
        _rows, cols = self._tab_grid_bounds(spreadsheet_id=spreadsheet_id, tab_name=tab_name)
        end_col = _column_letter(cols)
        res = self._execute(
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1:{end_col}1")
        )
        values = res.get("values") or []
        if not values or not values[0]:
            raise ValueError(
                f"Sheet tab {tab_name!r} has no header row. Add your column headers in row 1 first."
            )
        row = [str(c) if c is not None else "" for c in values[0]]
        while len(row) < cols:
            row.append("")
        return row[:cols]

    def ensure_header(self, *, spreadsheet_id: str, tab_name: str) -> list[str]:
        """Require an existing header row; returns headers for row building."""
        return self.get_headers(spreadsheet_id=spreadsheet_id, tab_name=tab_name)

    @staticmethod
    def _data_column_letters_from_headers(headers: list[str]) -> list[str]:
        """Columns that indicate a real project row (ignore stray data in empty columns)."""
        # Use link + project name only (company may be blank; other cols can have stray values).
        markers = {"orygin link", "nazwa inwestycji"}
        letters: list[str] = []
        for idx, header in enumerate(headers):
            if normalize_header(header) in markers:
                letters.append(_column_letter(idx + 1))
        return letters or ["C", "D", "E"]

    def _column_values(self, *, spreadsheet_id: str, tab_name: str, column_letter: str) -> list[list[str]]:
        res = self._execute(
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!{column_letter}:{column_letter}")
        )
        return res.get("values") or []

    @staticmethod
    def _cell_text(column_values: list[list[str]], row_index: int) -> str:
        if row_index >= len(column_values):
            return ""
        row = column_values[row_index]
        if not row:
            return ""
        return str(row[0]).strip()

    def _row_is_fully_empty(
        self,
        *,
        spreadsheet_id: str,
        tab_name: str,
        row_num: int,
        n_cols: int,
    ) -> bool:
        end_col = _column_letter(max(n_cols, 1))
        res = self._execute(
            self._svc.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A{row_num}:{end_col}{row_num}")
        )
        values = res.get("values") or []
        if not values:
            return True
        return all(not str(cell).strip() for cell in values[0])

    def _next_row_number(
        self,
        *,
        spreadsheet_id: str,
        tab_name: str,
        headers: list[str],
    ) -> int:
        """First row below the header where the ENTIRE row is empty.

        Candidate gaps are pre-filtered by the two project marker columns and
        then verified across all columns: a mid-sheet row with blank markers
        but stray content (a manual note, a leftover space) was previously
        overwritten A→end by the write below.
        """
        data_cols = self._data_column_letters_from_headers(headers)
        if len(data_cols) < 2:
            data_cols = ["C", "D"]
        col_a, col_b = data_cols[0], data_cols[1]
        values_a = self._column_values(
            spreadsheet_id=spreadsheet_id, tab_name=tab_name, column_letter=col_a
        )
        values_b = self._column_values(
            spreadsheet_id=spreadsheet_id, tab_name=tab_name, column_letter=col_b
        )
        scan_until = max(len(values_a), len(values_b))
        for i in range(1, scan_until):
            if not self._cell_text(values_a, i) and not self._cell_text(values_b, i):
                if self._row_is_fully_empty(
                    spreadsheet_id=spreadsheet_id,
                    tab_name=tab_name,
                    row_num=i + 1,
                    n_cols=len(headers),
                ):
                    return i + 1
        return max(scan_until + 1, 2)

    def append_row(
        self,
        *,
        spreadsheet_id: str,
        tab_name: str,
        row_values: list[Any],
        headers: list[str] | None = None,
    ) -> SheetAppendResult:
        """Write one row on the next free line, aligned to column A."""
        if headers is None:
            headers = self.get_headers(spreadsheet_id=spreadsheet_id, tab_name=tab_name)
        row_num = self._next_row_number(
            spreadsheet_id=spreadsheet_id,
            tab_name=tab_name,
            headers=headers,
        )
        end_col = _column_letter(max(len(row_values), 1))
        target_range = f"{tab_name}!A{row_num}:{end_col}{row_num}"
        self._execute(
            self._svc.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=target_range,
                # RAW, not USER_ENTERED: Sheets parses a leading "+" as a unary-plus
                # formula, so E.164 phones ("+48501234567") lost their "+" or became
                # #ERROR! — the legacy rows' manual backtick prefixes were the
                # workaround for exactly this.
                valueInputOption="RAW",
                body={"values": [row_values]},
            )
        )
        return SheetAppendResult(updated_range=target_range)

    def update_cells(
        self,
        *,
        spreadsheet_id: str,
        tab_name: str,
        updates: list[tuple[int, int, Any]],
    ) -> int:
        """Update individual cells. Each item is (row_num, col_index_1based, value)."""
        if not updates:
            return 0
        written = 0
        chunk_size = 100
        for start in range(0, len(updates), chunk_size):
            chunk = updates[start : start + chunk_size]
            data = [
                {
                    "range": f"{tab_name}!{_column_letter(col_idx)}{row_num}",
                    "values": [[value]],
                }
                for row_num, col_idx, value in chunk
            ]
            self._execute(
                self._svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"valueInputOption": "USER_ENTERED", "data": data},
                )
            )
            written += len(chunk)
        return written

    def read_column_text(
        self,
        *,
        spreadsheet_id: str,
        tab_name: str,
        column_letter: str,
    ) -> list[str]:
        """Return one string per sheet row in *column_letter* (index 0 = row 1)."""
        values = self._column_values(
            spreadsheet_id=spreadsheet_id,
            tab_name=tab_name,
            column_letter=column_letter,
        )
        return [self._cell_text(values, i) for i in range(len(values))]

    def _tab_grid_bounds(self, *, spreadsheet_id: str, tab_name: str) -> tuple[int, int]:
        meta = self._execute(
            self._svc.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties",
            )
        )
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties") or {}
            if props.get("title") == tab_name:
                grid = props.get("gridProperties") or {}
                return int(grid.get("rowCount", 1000)), int(grid.get("columnCount", 26))
        raise ValueError(
            f"Tab {tab_name!r} not found in spreadsheet. "
            "Check GOOGLE_SHEET_TAB_NAME (case-sensitive)."
        )

    def verify_write_access(self, *, spreadsheet_id: str, tab_name: str) -> None:
        """Probe write permission using the same append API as run-daily."""
        headers = self.get_headers(spreadsheet_id=spreadsheet_id, tab_name=tab_name)
        probe = [""] * len(headers)
        for i, h in enumerate(headers):
            if normalize_header(h) == "komentarz":
                probe[i] = "__skysnap_write_test__"
                break
        else:
            probe[0] = "__skysnap_write_test__"
        self.append_row(spreadsheet_id=spreadsheet_id, tab_name=tab_name, row_values=probe)
