import re
import openpyxl
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
INPUT_FILE  = 'schedule_sample.xlsx'
OUTPUT_FILE = 'schedule_parsed.csv'

# Row indices (0-based) in the sheet
ROW_DATES      = 0   # "Senin, 8 Desember 2025", ...
ROW_TIMES      = 1   # "07.30 - 09.00", ...
ROW_DATA_START = 3   # first class row (row 2 is Column1/Column2/… junk, skip it)

# ── Helpers ───────────────────────────────────────────────────────────────────
MONTH_MAP = {
    'Januari': '01', 'Februari': '02', 'Maret': '03', 'April': '04',
    'Mei': '05', 'Juni': '06', 'Juli': '07', 'Agustus': '08',
    'September': '09', 'Oktober': '10', 'November': '11', 'Desember': '12',
}

def parse_indo_date(val):
    """'Senin, 8 Desember 2025'  →  '2025-12-08'"""
    if val is None:
        return None
    s = re.sub(r"^[^,]+,\s*", '', str(val).strip())   # strip day name + comma
    for id_m, num_m in MONTH_MAP.items():
        s = s.replace(id_m, num_m)
    parts = s.split()
    if len(parts) == 3:
        day, month, year = parts
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return None

def parse_time(val):
    """'07.30 - 09.00'  →  ('07:30', '09:00')"""
    if val is None:
        return None, None
    s = str(val).strip().replace('.', ':')
    if '-' in s:
        start, end = s.split('-', 1)
        return start.strip(), end.strip()
    return None, None

def is_valid_class(val):
    """True for 'X - A', 'XI - B', etc."""
    return val is not None and bool(re.match(r'^X{1,2}I?\s*-\s*\w', str(val).strip()))

def is_junk(val):
    """True for None, 'nan', empty string, or Column-placeholder strings."""
    if val is None:
        return True
    s = str(val).strip()
    return s == '' or s == 'nan' or bool(re.match(r'^Column\d+$', s))

# ── Parse ─────────────────────────────────────────────────────────────────────
wb = openpyxl.load_workbook(INPUT_FILE, read_only=True)
ws = wb.active
rows = [list(r) for r in ws.iter_rows(values_only=True)]

# Build column metadata from header rows
# Column 0 is 'Kelas'; subject data starts at column 1
date_row = rows[ROW_DATES]
time_row = rows[ROW_TIMES]

col_metadata = []          # [(date_iso, time_start, time_end), ...]
current_date = None
for col in range(1, len(date_row)):
    d = date_row[col]
    t = time_row[col] if col < len(time_row) else None
    if d is not None:
        current_date = parse_indo_date(d)
    t_start, t_end = parse_time(t)
    col_metadata.append((current_date, t_start, t_end))

# Walk data rows
records = []
for row in rows[ROW_DATA_START:]:
    if not is_valid_class(row[0]):
        continue

    kelas = str(row[0]).strip()

    for col_idx, (date, t_start, t_end) in enumerate(col_metadata):
        raw = row[col_idx + 1] if (col_idx + 1) < len(row) else None

        if is_junk(raw):
            continue
        if not date or not t_start:
            continue

        records.append({
            'kelas':      kelas,
            'subject':    str(raw).strip(),
            'date':       date,
            'time_start': t_start,
            'time_end':   t_end,
        })

# ── Export ────────────────────────────────────────────────────────────────────
result = pd.DataFrame(records, columns=['kelas', 'subject', 'date', 'time_start', 'time_end'])
result.to_csv(OUTPUT_FILE, index=False)

print(f"✅  {len(result)} entries written to {OUTPUT_FILE}")
print(f"\nClasses found ({result['kelas'].nunique()}):", sorted(result['kelas'].unique()))
print("\nEntries per class:")
print(result.groupby('kelas').size().to_string())
print("\nSample (first 10 rows):")
print(result.head(10).to_string(index=False))