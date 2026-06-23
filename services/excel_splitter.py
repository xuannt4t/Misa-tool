"""Split multi-sheet Excel workbooks into manageable import files."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def split_excel_workbook(
    source_path: str | Path,
    output_directory: str | Path,
    rows_per_file: int,
) -> tuple[Path, list[Path], int]:
    """Merge source sheets in workbook order, then split by total data rows."""
    source = Path(source_path)
    if rows_per_file < 1:
        raise ValueError("Rows per file must be greater than zero.")
    sheets = pd.read_excel(source, sheet_name=None, dtype=str, keep_default_na=False, engine="openpyxl")
    sheets_with_data = [
        (name, frame.dropna(how="all"))
        for name, frame in sheets.items()
        if not frame.empty
    ]
    if not sheets_with_data:
        raise ValueError("The Excel file contains no data to split.")

    root = Path(output_directory)
    target = root / f"{source.stem}_split"
    suffix = 2
    while target.exists():
        target = root / f"{source.stem}_split_{suffix}"
        suffix += 1
    target.mkdir(parents=True, exist_ok=False)

    # The source can contain, for example, a 2025 sheet followed by a 2024
    # sheet.  Align columns by position so the data is appended in precisely
    # that tab order, then calculate the limit across the combined data.
    column_count = max(len(frame.columns) for _name, frame in sheets_with_data)
    columns = list(next(frame for _name, frame in sheets_with_data if len(frame.columns) == column_count).columns)
    aligned_frames = []
    for _sheet_name, frame in sheets_with_data:
        aligned = frame.copy()
        aligned.columns = range(len(aligned.columns))
        aligned = aligned.reindex(columns=range(column_count), fill_value="")
        aligned.columns = columns
        aligned_frames.append(aligned)
    data = pd.concat(aligned_frames, ignore_index=True)

    output_files: list[Path] = []
    for part_number, start in enumerate(range(0, len(data), rows_per_file), start=1):
        output_path = target / f"{source.stem}_part_{part_number:03d}.xlsx"
        data.iloc[start:start + rows_per_file].to_excel(
            output_path, sheet_name="Data", index=False, engine="openpyxl"
        )
        output_files.append(output_path)
    return target, output_files, len(data)
