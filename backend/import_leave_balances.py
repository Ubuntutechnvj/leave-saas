import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from leave_app.database import SessionLocal
from leave_app.models import Employee, LeaveBalance


# ── Helpers ─────────────────────────────────────────

def parse_days(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(value), 2)
    except:
        return None


def parse_cycle_date(value):
    from datetime import date, datetime

    if value is None or str(value).strip() == "":
        return None

    if hasattr(value, "year"):
        return date(value.year, value.month, 1)

    raw = str(value).strip()

    for fmt in ("%m-%Y", "%Y-%m", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return date(parsed.year, parsed.month, 1)
        except:
            pass

    print(f"⚠️ Could not parse cycle date: {raw}")
    return None


def read_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".csv":
        import csv
        with open(filepath, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    elif ext in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(filepath, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))

        headers = [str(h).strip() for h in rows[0]]

        data = []
        for r in rows[1:]:
            data.append({headers[i]: r[i] for i in range(len(headers))})

        return data

    else:
        print("❌ Unsupported file type")
        sys.exit(1)


def update_balance(db, employee, leave_type, allocated, used, remaining):
    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee.id,
        LeaveBalance.leave_type == leave_type
    ).first()

    if balance:
        balance.allocated = allocated
        balance.used = used
        balance.remaining = remaining
    else:
        db.add(
            LeaveBalance(
                company_id=employee.company_id,
                employee_id=employee.id,
                leave_type=leave_type,
                allocated=allocated,
                used=used,
                remaining=remaining
            )
        )


# ── MAIN ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = read_file(args.file)
    db = SessionLocal()

    for row in rows:
        code = str(row.get("employee_code", "")).strip()

        if not code:
            continue

        employee = db.query(Employee).filter(
            Employee.employee_code == code
        ).first()

        if not employee:
            print(f"❌ Employee not found: {code}")
            continue

        # ── Parse values ──
        annual_alloc = parse_days(row.get("annual_allocated"))
        annual_used = parse_days(row.get("annual_used"))
        annual_rem = parse_days(row.get("annual_remaining"))

        sick_alloc = parse_days(row.get("sick_allocated"))
        sick_used = parse_days(row.get("sick_used"))
        sick_rem = parse_days(row.get("sick_remaining"))

        family_alloc = parse_days(row.get("family_allocated"))
        family_used = parse_days(row.get("family_used"))
        family_rem = parse_days(row.get("family_remaining"))

        sick_start = parse_cycle_date(row.get("siek_start_cycle"))
        sick_end = parse_cycle_date(row.get("siek_end_cycle"))

        changes = []

        # ── Sick cycle ──
        if sick_start:
            employee.sick_cycle_start = sick_start
            changes.append(f"Sick start={sick_start}")

        if sick_end:
            employee.sick_cycle_end = sick_end
            changes.append(f"Sick end={sick_end}")

        # ── Annual ──
        if annual_rem is not None:
            allocated = annual_alloc if annual_alloc is not None else 0
            used = annual_used if annual_used is not None else max(allocated - annual_rem, 0)

            update_balance(db, employee, "Annual", allocated, used, annual_rem)

            changes.append(f"Annual → alloc:{allocated}, used:{used}, rem:{annual_rem}")

        # ── Sick ──
        if sick_rem is not None:
            allocated = sick_alloc if sick_alloc is not None else 0
            used = sick_used if sick_used is not None else max(allocated - sick_rem, 0)

            update_balance(db, employee, "Sick", allocated, used, sick_rem)

            changes.append(f"Sick → {sick_rem}")

        # ── Family ──
        if family_rem is not None:
            allocated = family_alloc if family_alloc is not None else 0
            used = family_used if family_used is not None else max(allocated - family_rem, 0)

            update_balance(db, employee, "Family Responsibility", allocated, used, family_rem)

            changes.append(f"Family → {family_rem}")

        if changes:
            print(f"✅ {employee.name}: {', '.join(changes)}")

    if not args.dry_run:
        db.commit()
        print("\n🎉 Leave balances updated!")

    db.close()


if __name__ == "__main__":
    main()