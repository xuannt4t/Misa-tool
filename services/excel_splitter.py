"""Split multi-sheet Excel workbooks into manageable import files."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def split_excel_workbook(
    source_path: str | Path,
    output_directory: str | Path,
    rows_per_file: int,
) -> tuple[Path, list[Path], int]:
    """Merge source sheets in order, then write files with at most N data rows."""
    source = Path(source_path)
    if rows_per_file < 1:
        raise ValueError("Rows per file must be greater than zero.")
    sheets = pd.read_excel(source, sheet_name=None, dtype=str, keep_default_na=False, engine="openpyxl")
    candidates = [
        (name, frame.dropna(how="all"))
        for name, frame in sheets.items()
        if not frame.empty
    ]
    if not candidates:
        raise ValueError("The Excel file contains no data to split.")

    # Workbooks often contain a supporting sheet (for example a customer list)
    # alongside the invoice sheets. Pick the column width representing the most
    # data rows, then merge all sheets with that layout by column position.
    row_totals_by_width: dict[int, int] = {}
    for _name, frame in candidates:
        row_totals_by_width[len(frame.columns)] = row_totals_by_width.get(len(frame.columns), 0) + len(frame)
    selected_width = max(row_totals_by_width, key=row_totals_by_width.get)
    selected = [frame for _name, frame in candidates if len(frame.columns) == selected_width]
    if not selected_width or not selected:
        raise ValueError("No compatible worksheet layout was found.")

    columns = list(selected[0].columns)
    aligned_frames = []
    for frame in selected:
        aligned = frame.iloc[:, :selected_width].copy()
        aligned.columns = columns
        aligned_frames.append(aligned)
    data = pd.concat(aligned_frames, ignore_index=True)
    root = Path(output_directory)
    target = root / f"{source.stem}_split"
    suffix = 2
    while target.exists():
        target = root / f"{source.stem}_split_{suffix}"
        suffix += 1
    target.mkdir(parents=True, exist_ok=False)

    output_files: list[Path] = []
    for part_number, start in enumerate(range(0, len(data), rows_per_file), start=1):
        output_path = target / f"{source.stem}_part_{part_number:03d}.xlsx"
        data.iloc[start:start + rows_per_file].to_excel(
            output_path, sheet_name="Data", index=False, engine="openpyxl"
        )
        output_files.append(output_path)
    return target, output_files, len(data)
