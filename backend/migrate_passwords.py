from leave_app.database import SessionLocal
from leave_app.models import Employee
from leave_app.auth import hash_password

db = SessionLocal()

try:
    employees = db.query(Employee).all()

    for emp in employees:
        if emp.password and not (
            emp.password.startswith("$2a$")
            or emp.password.startswith("$2b$")
            or emp.password.startswith("$2y$")
        ):
            print(f"Hashing password for: {emp.email}")
            emp.password = hash_password(emp.password)

    db.commit()
    print("Password migration completed successfully.")

except Exception as e:
    db.rollback()
    print("Error:", e)

finally:
    db.close()