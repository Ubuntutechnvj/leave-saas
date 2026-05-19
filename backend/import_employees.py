from dotenv import load_dotenv
load_dotenv()

import csv
from datetime import datetime
from leave_app.database import SessionLocal, engine
from leave_app.models import Employee, Company
from leave_app.auth import hash_password

print("🚀 Script started")
print("SCRIPT DB:", engine.url)

db = SessionLocal()

def parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            print("❌ Invalid date:", date_str)
            return None
        
company = db.query(Company).filter(Company.code == "UB").first()
if not company:
    company = Company(name="Ubuntu Technology", code="UB")
    db.add(company)
    db.commit()
    db.refresh(company)

with open("employees.csv", newline='', encoding="utf-8") as file:
    reader = csv.reader(file)
    next(reader)

    for row in reader:
        print("Reading:", row[4])

        email = row[5].lower()
        emp_code = row[2]

        existing = db.query(Employee).filter(
            (Employee.email == email) | (Employee.employee_code == emp_code)
        ).first()

        if existing:
            print("⏭ Skipping existing:", row[4])
            continue

        try:
            employee = Employee(
                company_id=company.id,
                name=row[4],
                email=email,
                password=hash_password("1234"),
                department="General",
                start_date=parse_date(row[6]) if row[6] else None,
                role=row[3].lower(),
                employee_code=emp_code
            )

            db.add(employee)
            db.commit()   # ✅ commit per row
            print("✅ Added:", row[4])

        except Exception as e:
            db.rollback()
            print("❌ Error:", row[4], "|", e)

employees = db.query(Employee).all()
print("✅ Total employees in DB:", len(employees))