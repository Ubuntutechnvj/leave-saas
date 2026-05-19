"""
debug_excel.py
Run this to see exactly what values openpyxl reads from your Excel file.
Usage: python debug_excel.py --file leave_balances.xlsx
"""
import argparse
import openpyxl

parser = argparse.ArgumentParser()
parser.add_argument("--file", required=True)
args = parser.parse_args()

wb = openpyxl.load_workbook(args.file, data_only=True)
ws = wb.active
rows = list(ws.iter_rows(values_only=True))

headers = rows[0]
print(f"Columns: {headers}\n")

# Print first 5 data rows with raw Python types
for row in rows[1:6]:
    for h, v in zip(headers, row):
        print(f"  {h}: {repr(v)}  (type: {type(v).__name__})")
    print()