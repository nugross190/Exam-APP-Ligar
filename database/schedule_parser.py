import pandas as pd
import re

# Load the Excel file (ensure 'openpyxl' is installed: pip install openpyxl pandas)
df = pd.read_excel('schedule_sample.xlsx', header=None)

# Mapping for Indonesian months to convert to ISO format
MONTH_MAP = {
    'Januari': '01', 'Februari': '02', 'Maret': '03', 'April': '04',
    'Mei': '05', 'Juni': '06', 'Juli': '07', 'Agustus': '08',
    'September': '09', 'Oktober': '10', 'November': '11', 'Desember': '12'
}

def parse_indo_date(date_str):
    if pd.isna(date_str): return None
    s = str(date_str).strip()
    # Remove day name (e.g., "Senin, ")
    s = re.sub(r'^.*?,\s*', '', s)
    # Replace Indonesian month names with numbers
    for id_m, num_m in MONTH_MAP.items():
        s = s.replace(id_m, num_m)
    # Expected format: DD MM YYYY -> convert to YYYY-MM-DD
    parts = s.split()
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None

def parse_time(time_str):
    if pd.isna(time_str): return None, None
    s = str(time_str).strip().replace('.', ':')
    if '-' in s:
        start, end = s.split('-', 1)
        return start.strip(), end.strip()
    return None, None

# Extract header rows (Row 0 = Dates, Row 1 = Times)
dates_row = df.iloc[0].tolist()
times_row = df.iloc[1].tolist()

# Map each column index to (date, start_time, end_time)
col_metadata = []
current_date = None
for i in range(1, len(dates_row)):  # Skip column 0 ('kelas')
    d = dates_row[i]
    t = times_row[i]
    
    if pd.notna(d):
        current_date = parse_indo_date(d)
        
    if pd.notna(t):
        t_start, t_end = parse_time(t)
    else:
        t_start, t_end = None, None
        
    col_metadata.append((current_date, t_start, t_end))

# Parse data rows
parsed_data = []
for idx in range(2, len(df)):
    row = df.iloc[idx]
    kelas = str(row[0]).strip()
    
    # Filter only valid class rows (e.g., "XI - A", "X - B")
    if not re.match(r'^(XI|X)\s*-', kelas):
        continue
        
    for col_idx, (date, t_start, t_end) in enumerate(col_metadata):
        subject = str(row[col_idx + 1]).strip()
        
        # Skip placeholders, empty cells, or NaN
        if subject and subject not in ['nan', ''] and not subject.startswith('Column'):
            if date and t_start:
                parsed_data.append({
                    'kelas': kelas,
                    'subject': subject,
                    'date': date,
                    'time_start': t_start,
                    'time_end': t_end
                })

# Create DataFrame and export
result_df = pd.DataFrame(parsed_data)
result_df.to_csv('schedule_parsed.csv', index=False)

print(f"✅ Successfully parsed {len(result_df)} schedule entries.")
print(result_df.head(10))