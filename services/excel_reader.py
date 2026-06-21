"""Excel-to-invoice conversion."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def read_invoices_excel(file_path: str | Path) -> list[dict[str, str | int]]:
    """Read every sheet using the shared A-I invoice layout."""
    sheets = pd.read_excel(
        file_path,
        sheet_name=None,
        dtype=str,
        keep_default_na=False,
        engine="openpyxl",
    )
    invoices: list[dict[str, str | int]] = []
    for sheet_name, dataframe in sheets.items():
        if len(dataframe.columns) < 8:
            raise ValueError(f"Sheet '{sheet_name}' needs at least 8 columns (A-H).")

        for index, values in dataframe.iterrows():
            # A thang, B a, C khmhd, D hoa_don, E date, F customer name,
            # G dia_chi, H mst.  Some source files add I mst2.
            invoice_number = _text(values.iloc[3])
            # Ignore blank rows and accidental repeated header rows.
            if not invoice_number or invoice_number.casefold() in {"hóa đơn", "hoa don", "hđơn", "hdon"}:
                continue
            invoices.append(
                {
                    "thang": _text(values.iloc[0]),
                    "a": _text(values.iloc[1]),
                    "khmhd": _text(values.iloc[2]),
                    "hoa_don": invoice_number,
                    "date": _text(values.iloc[4]),
                    "ten_khach_hang": _text(values.iloc[5]),
                    "dia_chi": _text(values.iloc[6]),
                    # In the common 8-column layout, H is the customer tax ID
                    # used by the automation (MST2).  The optional 9-column
                    # layout keeps H as MST1 and reads I as MST2.
                    "mst1": _identifier_text(values.iloc[7]) if len(dataframe.columns) >= 9 else "",
                    "mst2": _identifier_text(values.iloc[8]) if len(dataframe.columns) >= 9 else _identifier_text(values.iloc[7]),
                    "row": f"{sheet_name}:{index + 2}",
                }
            )
    return invoices


def _text(value: object) -> str:
    """Normalize Excel values for SQLite text fields."""
    if pd.isna(value):
        return ""
    return str(value).strip()


def _identifier_text(value: object) -> str:
    """Remove Excel's visual dot separators from numeric tax/phone identifiers."""
    text = _text(value)
    compact = text.replace(".", "")
    return compact if text and compact.isdecimal() else text
