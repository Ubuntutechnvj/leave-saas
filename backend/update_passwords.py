import csv
from leave_app.database import SessionLocal
from leave_app.models import Employee
from leave_app.auth import hash_password

db = SessionLocal()

with open("employees.csv", newline='', encoding="utf-8") as file:
    reader = csv.reader(file)
    next(reader)

    for row in reader:
        employee_code = row[2].strip()
        password_raw = row[7].strip()

        # Skip empty passwords
        if not password_raw:
            continue

        employee = db.query(Employee).filter(
            Employee.employee_code == employee_code
        ).first()

        if not employee:
            print(f"❌ Employee not found: {employee_code}")
            continue

        try:
            employee.password = hash_password(password_raw)
            db.commit()
            print(f"✅ Password updated for {employee.name}")

        except Exception as e:
            db.rollback()
            print(f"❌ Error updating {employee_code}: {e}")

print("🎉 Password update complete!")