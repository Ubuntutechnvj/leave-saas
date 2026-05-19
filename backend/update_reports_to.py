import csv
from leave_app.database import SessionLocal
from leave_app.models import Employee

db = SessionLocal()

# Build name → id map
employees = db.query(Employee).all()
name_to_id = {e.name.strip(): e.id for e in employees}

with open("employees.csv", newline='', encoding="utf-8") as file:
    reader = csv.reader(file)
    next(reader)

    for row in reader:
        employee_name = row[4].strip()
        reports_to_name = row[8].strip()

        if not reports_to_name:
            continue

        if not manager_id:
           print(f"❌ Could not find manager for {employee_name} → '{reports_to_name}'")

        employee = db.query(Employee).filter(Employee.name == employee_name).first()
        manager_id = None 
        for name, id in name_to_id.items():
            if reports_to_name.lower() in name.lower():
                manager_id = id
                break

        if employee and manager_id:
            employee.reports_to = manager_id
            print(f"✅ {employee_name} now reports to {reports_to_name}")

db.commit()

print("🎉 Reports_to updated!")