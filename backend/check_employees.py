from leave_app.database import SessionLocal
from leave_app.models import Employee

db = SessionLocal()

employees = db.query(Employee).all()

print("Total employees:", len(employees))

for e in employees[:10]:
    print("EMAIL IN DB:", e.email)