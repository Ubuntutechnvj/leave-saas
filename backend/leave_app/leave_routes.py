from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from datetime import date, datetime, timedelta
from typing import Optional
from calendar import monthrange
from pydantic import BaseModel, EmailStr
from dateutil.relativedelta import relativedelta
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

import holidays
from datetime import timedelta
from fastapi import HTTPException

import os
import shutil
import uuid
 
from fastapi.responses import StreamingResponse
import io
import csv

from .database import get_db
from .models import Employee, LeaveRequest, LeaveBalance, Company, EmployeeStandIn, LeaveStandInRequest, OvertimeOffBalance
from .auth import authenticate_user, create_access_token, hash_password
from .dependencies import (
    get_current_active_user,
    require_admin,
    require_hr_or_admin,
    require_manager_level,
)


router = APIRouter()

# =====================================================
# CONFIG
# =====================================================

UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "uploads",
    "doctor_notes"
)

os.makedirs(UPLOAD_DIR, exist_ok=True)

email_config = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
    MAIL_FROM=os.getenv("MAIL_FROM", "noreply@example.com"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
    MAIL_SERVER=os.getenv("MAIL_SERVER", "smtp.office365.com"),
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=bool(os.getenv("MAIL_USERNAME") and os.getenv("MAIL_PASSWORD")),
)

# =====================================================
# REQUEST SCHEMAS
# =====================================================

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LeaveRequestCreate(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    day_portion: float = 1
    reason: Optional[str] = None

class ApproveRejectRequest(BaseModel):
    leave_id: int


class AmendLeaveRequest(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    day_portion: float = 1

class CompanyCreate(BaseModel):
    name: str
    code: str


class EmployeeCreateRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    department: str
    start_date: date
    end_date: Optional[date] = None
    role: str
    company_id: int
    reports_to: Optional[int] = None
    employee_code: Optional[str] = None
    annual_leave_days: int = 15
    accrual_rate: float = 1.25
    max_entitled: float = 15.0


class EmployeeUpdateRequest(BaseModel):
    name: str
    email: EmailStr
    department: str
    role: str
    start_date: date
    end_date: Optional[date] = None
    company_id: int
    reports_to: Optional[int] = None
    employee_code: Optional[str] = None
    new_password: Optional[str] = None
    annual_leave_days: int = 15
    accrual_rate: float = 1.25
    max_entitled: float = 15.0

class StandInItem(BaseModel):
    standin_employee_id: int
    order_no: int


class EmployeeStandInRequest(BaseModel):
    standins: list[StandInItem]

class AddOvertimeRequest(BaseModel):
    employee_id: int
    hours_worked: float


class TakeOvertimeRequest(BaseModel):
    employee_id: int
    hours_taken: float

# =====================================================
# EMAIL HELPERS
# =====================================================

async def send_email(to: str, subject: str, body: str):
    try:
        message = MessageSchema(
            subject=subject,
            recipients=[to],
            body=body,
            subtype="html"
        )
        fm = FastMail(email_config)
        await fm.send_message(message)
    except Exception as e:
        print(f"Email failed: {e}")


def send_email_background(to: str, subject: str, body: str):
    import threading
    import asyncio

    def run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(send_email(to, subject, body))
            loop.close()
        except Exception as e:
            print(f"Background email failed: {e}")

    threading.Thread(target=run, daemon=True).start()


# =====================================================
# Holidays
# =====================================================

def get_public_holidays_between(start_date, end_date):
    years = list(range(start_date.year, end_date.year + 1))

    za_holidays = holidays.country_holidays(
        "ZA",
        years=years,
        observed=True
    )

    public_holidays_found = []

    current = start_date
    while current <= end_date:
        if current in za_holidays:
            public_holidays_found.append({
                "date": current,
                "name": za_holidays.get(current)
            })
        current += timedelta(days=1)

    return public_holidays_found

# =====================================================
# UTILITY HELPERS
# =====================================================

def count_workdays(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def get_annual_entitlement(start_date, stored_days=None):
    today = date.today()

    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()

    years_worked = today.year - start_date.year
    if (today.month, today.day) < (start_date.month, start_date.day):
        years_worked -= 1
    years_worked = max(years_worked, 0)

    auto_entitlement = min(15 + years_worked, 20)

    if stored_days is not None and stored_days > auto_entitlement:
        return stored_days

    return auto_entitlement


def calculate_accrued_annual(start_date, accrual_rate=1.25, max_entitled=None):
    today = date.today()

    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()

    years_worked = today.year - start_date.year
    if (today.month, today.day) < (start_date.month, start_date.day):
        years_worked -= 1
    years_worked = max(years_worked, 0)

    auto_cap = min(15 + years_worked, 20)
    effective_cap = max(auto_cap, float(max_entitled)) if max_entitled else auto_cap

    months_worked = (
        (today.year - start_date.year) * 12 +
        (today.month - start_date.month)
    )

    if today.day < start_date.day:
        months_worked -= 1

    months_worked = max(months_worked, 0)

    accrued = round(months_worked * accrual_rate, 2)
    return min(accrued, effective_cap)


def get_or_create_leave_balance(db: Session, employee_id: int, company_id: int, leave_type: str) -> LeaveBalance:
    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee_id,
        LeaveBalance.leave_type == leave_type
    ).first()

    if balance:
        return balance

    defaults = {
        "Annual": {"allocated": 0, "used": 0, "remaining": 0},
        "Sick": {"allocated": 30, "used": 0, "remaining": 30},
        "Family Responsibility": {"allocated": 3, "used": 0, "remaining": 3},
        "Study": {"allocated": 0, "used": 0, "remaining": 0},
        "Unpaid": {"allocated": 0, "used": 0, "remaining": 0},
    }

    values = defaults.get(leave_type, {"allocated": 0, "used": 0, "remaining": 0})

    balance = LeaveBalance(
        company_id=company_id,
        employee_id=employee_id,
        leave_type=leave_type,
        allocated=values["allocated"],
        used=values["used"],
        remaining=values["remaining"],
    )
    db.add(balance)
    db.flush()
    return balance


def reset_sick_leave_if_needed(db: Session, employee: Employee):
    today = date.today()

    if (
        employee.sick_cycle_end is not None and
        today.year == employee.sick_cycle_end.year and
        today.month >= employee.sick_cycle_end.month
    ):
        sick_balance = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == employee.id,
            LeaveBalance.leave_type == "Sick"
        ).first()

        if sick_balance:
            sick_balance.allocated = 30
            sick_balance.used = 0
            sick_balance.remaining = 30

        new_start = employee.sick_cycle_end
        new_end = employee.sick_cycle_end + relativedelta(years=3)
        employee.sick_cycle_start = new_start
        employee.sick_cycle_end = new_end
        db.commit()


def generate_leave_code(db: Session) -> str:
    last_code = db.query(func.max(LeaveRequest.leave_code)).scalar()
    next_code = int(last_code) + 1 if last_code and str(last_code).isdigit() else 1
    return str(next_code).zfill(4)


def can_view_employee(current_user: Employee, target: Employee) -> bool:
    if current_user.role in ["admin", "hr"]:
        return True
    if current_user.id == target.id:
        return True
    if current_user.role in ["manager", "supervisor", "director"] and target.reports_to == current_user.id:
        return True
    return False


def can_view_leave(current_user: Employee, leave: LeaveRequest) -> bool:
    if current_user.role in ["admin", "hr"]:
        return True
    if leave.employee_id == current_user.id:
        return True
    if current_user.role in ["manager", "supervisor", "director"]:
        if leave.approver_id == current_user.id:
            return True
        if leave.employee and leave.employee.reports_to == current_user.id:
            return True
    return False


def ensure_valid_role(role: str):
    if role not in ["employee", "supervisor", "manager", "director", "hr", "admin"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid role. Must be admin, hr, manager, supervisor, director, or employee"
        )


def ensure_manager_role(employee: Employee):
    if employee.role not in ["manager", "supervisor", "director"]:
        raise HTTPException(
            status_code=400,
            detail="Selected employee is not a manager or supervisor"
        )


def get_effective_manager_id(current_user: Employee, manager_id: Optional[int]) -> int:
    if current_user.role in ["manager", "supervisor", "director"]:
        return current_user.id

    if manager_id is None:
        raise HTTPException(
            status_code=400,
            detail="manager_id is required for admin/hr"
        )

    return manager_id


def calculate_leave_days_for_request(start_date, end_date, day_portion=1):
    current = start_date
    days = 0.0
    portion = float(day_portion or 1)

    while current <= end_date:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)

    if start_date == end_date:
        days = portion

    return round(days, 2)

def apply_leave_balance_effect(db: Session, leave: LeaveRequest):
    if leave.leave_type == "Unpaid":
        return

    balance = get_or_create_leave_balance(db, leave.employee_id, leave.company_id, leave.leave_type)
    available = round(float(balance.remaining or 0), 2)

    if available < float(leave.days or 0):
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient {leave.leave_type.lower()} leave balance"
        )

    balance.used = round(float(balance.used or 0) + float(leave.days or 0), 2)

    if leave.leave_type == "Annual":
        balance.remaining = max(round(float(balance.remaining or 0) - float(leave.days or 0), 2), 0)
    else:
        balance.remaining = round(float(balance.remaining or 0) - float(leave.days or 0), 2)


def reverse_leave_balance_effect(db: Session, leave: LeaveRequest):
    if leave.leave_type == "Unpaid":
        return

    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == leave.employee_id,
        LeaveBalance.leave_type == leave.leave_type
    ).first()

    if not balance:
        return

    balance.used = max(round(float(balance.used or 0) - float(leave.days or 0), 2), 0)

    if leave.leave_type == "Annual":
        balance.remaining = max(round(float(balance.remaining or 0) + float(leave.days or 0), 2), 0)
    else:
        balance.remaining = round(float(balance.remaining or 0) + float(leave.days or 0), 2)


def get_annual_used_this_year(db: Session, employee_id: int) -> float:
    annual_used = db.query(func.coalesce(func.sum(LeaveRequest.days), 0)).filter(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.leave_type == "Annual",
        LeaveRequest.status == "APPROVED",
        func.extract("year", LeaveRequest.start_date) == date.today().year
    ).scalar()

    return round(float(annual_used or 0), 2)

# =====================================================
# AUTH
# =====================================================

@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, payload.email, payload.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    if user.end_date and user.end_date <= date.today():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Please contact HR."
        )

    token = create_access_token({"sub": str(user.id), "role": user.role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "employee_id": user.id,
        "name": user.name,
        "role": user.role
    }

# =====================================================
# Temp pass
# =====================================================

from .auth import verify_password

@router.get("/test-password")
def test_password():
    hashed = "$2b$12$c2aVtzs5HNxhzIwTobdJA.8YgZwZpvKAFSgg9owZcdieImOPIwQnC"

    return {
        "Password123!": verify_password("Password123!", hashed),
        "admin123": verify_password("admin123", hashed),
        "password": verify_password("password", hashed),
        "123456": verify_password("123456", hashed),
    }

@router.get("/overtime-off")
def get_overtime_off(
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    balances = db.query(OvertimeOffBalance).all()

    result = []

    for balance in balances:
        emp = db.query(Employee).filter(
            Employee.id == balance.employee_id
        ).first()

        if not emp:
            continue

        result.append({
            "employee_id": emp.id,
            "employee_name": emp.name,
            "total_hours": balance.total_hours,
            "hours_taken": balance.hours_taken,
            "balance": balance.total_hours - balance.hours_taken
        })

    return result

# =====================================================
# OVERTIME OFF
# =====================================================

@router.post("/overtime-off/add")
def add_overtime_off(
    payload: AddOvertimeRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    emp = db.query(Employee).filter(Employee.id == payload.employee_id).first()

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    balance = db.query(OvertimeOffBalance).filter(
        OvertimeOffBalance.employee_id == payload.employee_id
    ).first()

    if not balance:
        balance = OvertimeOffBalance(
            employee_id=payload.employee_id,
            total_hours=0,
            hours_taken=0
        )
        db.add(balance)

    balance.total_hours += payload.hours_worked

    db.commit()

    return {"message": "Overtime hours added successfully"}


@router.post("/overtime-off/take")
def take_overtime_off(
    payload: TakeOvertimeRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    balance = db.query(OvertimeOffBalance).filter(
        OvertimeOffBalance.employee_id == payload.employee_id
    ).first()

    if not balance:
        raise HTTPException(status_code=400, detail="No overtime balance found for employee")

    available = balance.total_hours - balance.hours_taken

    if payload.hours_taken > available:
        raise HTTPException(
            status_code=400,
            detail=f"Not enough overtime hours available. Available: {available}"
        )

    balance.hours_taken += payload.hours_taken

    db.commit()

    return {"message": "Overtime off submitted successfully"}

# =====================================================
# LEAVE BALANCE / MY ROUTES
# =====================================================

@router.get("/my-leave-balance")
def get_my_leave_balance(
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == current_user.id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    reset_sick_leave_if_needed(db, employee)

    balances = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee.id
    ).all()

    entitlement = get_annual_entitlement(
        employee.start_date,
        employee.max_entitled
    )

    annual_used = get_annual_used_this_year(db, employee.id)

    result = []
    for b in balances:
        if b.leave_type == "Annual":
            result.append({
                "leave_type": "Annual",
                "allocated": round(float(b.allocated or employee.max_entitled or employee.annual_leave_days or 15), 2),
                "used": annual_used,
                "remaining": round(float(b.remaining or 0), 2),
                "entitlement": entitlement,
                "accrual_rate": employee.accrual_rate or 1.25,
                "max_entitled": employee.max_entitled or 15.0
            })
        else:
            result.append({
                "leave_type": b.leave_type,
                "allocated": b.allocated,
                "used": b.used,
                "remaining": b.remaining
            })

    return result


@router.get("/leave-balance/{employee_id}")
def get_employee_leave_balance(
    employee_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not can_view_employee(current_user, employee):
        raise HTTPException(status_code=403, detail="Not authorized to view this employee")

    reset_sick_leave_if_needed(db, employee)

    balances = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee.id
    ).all()

    entitlement = get_annual_entitlement(
        employee.start_date,
        employee.max_entitled
    )

    annual_used = get_annual_used_this_year(db, employee.id)

    result = []
    for b in balances:
        if b.leave_type == "Annual":
            result.append({
                "leave_type": "Annual",
                "allocated": round(float(b.allocated or employee.max_entitled or employee.annual_leave_days or 15), 2),
                "used": annual_used,
                "remaining": round(float(b.remaining or 0), 2),
                "entitlement": entitlement,
                "accrual_rate": employee.accrual_rate or 1.25,
                "max_entitled": employee.max_entitled or 15.0
            })
        else:
            result.append({
                "leave_type": b.leave_type,
                "allocated": b.allocated,
                "used": b.used,
                "remaining": b.remaining
            })

    return result


@router.get("/my-leave-requests")
def get_my_leave_requests(
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    requests = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == current_user.id
    ).order_by(LeaveRequest.id.desc()).all()

    return [
        {
            "id": r.id,
            "leave_code": r.leave_code,
            "leave_type": r.leave_type,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "days": r.days,
            "status": r.status,
            "doctor_note_path": r.doctor_note_path or None,
            "reason": r.reason,
        }
        for r in requests
    ]

def create_first_standin_request(db: Session, leave: LeaveRequest):
    standins = db.query(EmployeeStandIn).filter(
        EmployeeStandIn.employee_id == leave.employee_id
    ).order_by(EmployeeStandIn.order_no.asc()).all()

    if not standins:
        return

    for index, s in enumerate(standins):
        status = "NOTIFIED" if index == 0 else "WAITING"

        db.add(LeaveStandInRequest(
            leave_request_id=leave.id,
            employee_id=leave.employee_id,
            standin_employee_id=s.standin_employee_id,
            order_no=s.order_no,
            status=status
        ))

@router.get("/leave-requests/{employee_id}")
def get_leave_requests_for_employee(
    employee_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not can_view_employee(current_user, employee):
        raise HTTPException(status_code=403, detail="Not authorized to view this employee")

    requests = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == employee_id
    ).order_by(LeaveRequest.id.desc()).all()

    return [
        {
            "id": r.id,
            "leave_code": r.leave_code,
            "leave_type": r.leave_type,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "days": r.days,
            "status": r.status,
            "reason": r.reason,
            "doctor_note_path": r.doctor_note_path or None
        }
        for r in requests
    ]

# =====================================================
# CREATE LEAVE REQUEST
# =====================================================

@router.post("/leave-request")
def create_leave_request(
    payload: LeaveRequestCreate,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    leave_type = payload.leave_type.strip()

    valid_types = ["Annual", "Sick", "Family Responsibility", "Study", "Unpaid"]
    if leave_type not in valid_types:
        raise HTTPException(status_code=400, detail="Invalid leave type")

    public_holidays = get_public_holidays_between(payload.start_date, payload.end_date)

    if public_holidays:
        holiday_text = ", ".join(
            f"{h['date']} ({h['name']})" for h in public_holidays
        )
        raise HTTPException(
            status_code=400,
            detail=f"Leave cannot include public holidays: {holiday_text}"
        )

    sick_flag = False
    sick_flag_reason = None

    if leave_type == "Sick":
        eight_weeks_ago = payload.start_date - timedelta(days=56)

        previous_sick_leave = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == current_user.id,
            LeaveRequest.leave_type == "Sick",
            LeaveRequest.status != "REJECTED",
            LeaveRequest.start_date >= eight_weeks_ago,
            LeaveRequest.start_date < payload.start_date
        ).first()

        if previous_sick_leave:
            sick_flag = True
            sick_flag_reason = "Sick leave taken within the last 8 weeks"

    overlapping = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == current_user.id,
        LeaveRequest.status.in_(["PENDING", "APPROVED"]),
        LeaveRequest.start_date <= payload.end_date,
        LeaveRequest.end_date >= payload.start_date
    ).first()

    if overlapping:
        raise HTTPException(status_code=400, detail="You already have an overlapping leave request")

    days = calculate_leave_days_for_request(
        payload.start_date,
        payload.end_date,
        payload.day_portion
    )

    approver_id = current_user.reports_to
    leave_code = generate_leave_code(db)

    if leave_type != "Unpaid":
        balance = get_or_create_leave_balance(db, current_user.id, current_user.company_id, leave_type)
        available = round(float(balance.remaining or 0), 2)

        if leave_type != "Sick" and available < days:
            raise HTTPException(status_code=400, detail=f"Insufficient {leave_type.lower()} leave balance")

    leave = LeaveRequest(
        company_id=current_user.company_id,
        employee_id=current_user.id,
        approver_id=approver_id,
        leave_code=leave_code,
        leave_type=leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        days=days,
        status="PENDING",
        sick_flag=sick_flag,
        sick_flag_reason=sick_flag_reason,
        reason=payload.reason 
    )

    db.add(leave)
    db.commit()
    db.refresh(leave)

    create_first_standin_request(db, leave)
    db.commit()

    return {
        "message": "Leave request submitted successfully",
        "leave_id": leave.id,
        "leave_code": leave.leave_code
    }

# =====================================================
# APPROVALS
# =====================================================

@router.get("/pending-approvals")
def get_pending_approvals(
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    query = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.status == "PENDING"
    )

    if current_user.role in ["manager", "supervisor", "director"]:
        query = query.filter(LeaveRequest.approver_id == current_user.id)

    requests = query.order_by(LeaveRequest.id.desc()).all()

    return [
        {
            "id": r.id,
            "employee_id": r.employee_id,
            "employee_name": r.employee.name if r.employee else "Unknown",
            "leave_code": r.leave_code,
            "leave_type": r.leave_type,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "days": r.days,
            "status": r.status,
            "doctor_note_path": r.doctor_note_path or None,
            "reason": r.reason,
            "sick_flag": r.sick_flag,
            "sick_flag_reason": r.sick_flag_reason
        }
        for r in requests
    ]


@router.get("/all-leave-requests")
def get_all_leave_requests(
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):

    leaves = db.query(LeaveRequest).all()

    result = []

    for leave in leaves:
        employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
        approver = db.query(Employee).filter(Employee.id == leave.approver_id).first() if leave.approver_id else None
        company = db.query(Company).filter(Company.id == leave.company_id).first()

        result.append({
            "id": leave.id,
            "leave_code": leave.leave_code,
            "employee_id": leave.employee_id,
            "employee_name": employee.name if employee else None,
            "employee_code": employee.employee_code if employee else None,
            "company_name": company.name if company else None,
            "department": employee.department if employee else None,
            "leave_type": leave.leave_type,
            "start_date": str(leave.start_date),
            "end_date": str(leave.end_date),
            "days": leave.days,
            "status": leave.status,
            "approver_name": approver.name if approver else None,
            "doctor_note_path": leave.doctor_note_path,
            "reason": leave.reason
        })

    return result

@router.post("/approve-leave")
def approve_leave(
    payload: ApproveRejectRequest,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.id == payload.leave_id
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.status != "PENDING":
        raise HTTPException(status_code=400, detail="Leave has already been processed")

    if current_user.role in ["manager", "supervisor", "director"] and leave.approver_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the approver for this request")

    apply_leave_balance_effect(db, leave)

    leave.status = "APPROVED"
    leave.approver_id = current_user.id
    db.commit()

    employee = leave.employee
    if employee and employee.email:
        send_email_background(
            to=employee.email,
            subject="Leave Request Approved",
            body=f"""
            <h3>Your Leave Request Has Been Approved</h3>
            <p>Dear {employee.name},</p>
            <p>Your <b>{leave.leave_type}</b> leave request from
            <b>{leave.start_date}</b> to <b>{leave.end_date}</b>
            ({leave.days} days) has been <b style='color:green'>APPROVED</b>.</p>
            <p>Approved by: {current_user.name}</p>
            <br>
            <p>Leave Management System</p>
            """
        )

    return {"message": "Leave approved successfully"}


@router.post("/reject-leave")
def reject_leave(
    payload: ApproveRejectRequest,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == payload.leave_id).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.status != "PENDING":
        raise HTTPException(status_code=400, detail="Leave has already been processed")

    if current_user.role in ["manager", "supervisor", "director"] and leave.approver_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the approver for this request")

    leave.status = "REJECTED"
    leave.approver_id = current_user.id
    db.commit()

    employee = db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if employee and employee.email:
        send_email_background(
            to=employee.email,
            subject="Leave Request Rejected",
            body=f"""
            <h3>Your Leave Request Has Been Rejected</h3>
            <p>Dear {employee.name},</p>
            <p>Your <b>{leave.leave_type}</b> leave request from
            <b>{leave.start_date}</b> to <b>{leave.end_date}</b>
            ({leave.days} days) has been <b style='color:red'>REJECTED</b>.</p>
            <p>Rejected by: {current_user.name}</p>
            <p>Please contact your manager for more information.</p>
            <br>
            <p>Leave Management System</p>
            """
        )

    return {"message": "Leave rejected successfully"}


@router.put("/cancel-leave/{leave_id}")
def cancel_leave(
    leave_id: int,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.id == leave_id
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.status == "CANCELLED":
        raise HTTPException(status_code=400, detail="Leave request has already been cancelled")

    if leave.status == "REJECTED":
        raise HTTPException(status_code=400, detail="Rejected leave cannot be cancelled")

    try:
        if leave.status == "APPROVED":
            reverse_leave_balance_effect(db, leave)

        leave.status = "CANCELLED"
        db.commit()
        db.refresh(leave)
    except Exception:
        db.rollback()
        raise

    employee = leave.employee or db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if employee and employee.email:
        send_email_background(
            to=employee.email,
            subject="Leave Request Cancelled",
            body=f"""
            <h3>Your Leave Request Has Been Cancelled</h3>
            <p>Dear {employee.name},</p>
            <p>Your <b>{leave.leave_type}</b> leave request from
            <b>{leave.start_date}</b> to <b>{leave.end_date}</b>
            ({leave.days} days) has been <b style='color:orange'>CANCELLED</b> by HR/Admin.</p>
            <br>
            <p>Leave Management System</p>
            """
        )

    return {"message": "Leave cancelled successfully"}


@router.put("/amend-leave/{leave_id}")
def amend_leave(
    leave_id: int,
    payload: AmendLeaveRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.id == leave_id
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.status in ["REJECTED", "CANCELLED"]:
        raise HTTPException(status_code=400, detail=f"Cannot amend a {leave.status.lower()} leave request")

    leave_type = payload.leave_type.strip()
    valid_types = ["Annual", "Sick", "Family Responsibility", "Study", "Unpaid"]
    if leave_type not in valid_types:
        raise HTTPException(status_code=400, detail="Invalid leave type")

    overlapping = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == leave.employee_id,
        LeaveRequest.id != leave.id,
        LeaveRequest.status.in_(["PENDING", "APPROVED"]),
        LeaveRequest.start_date <= payload.end_date,
        LeaveRequest.end_date >= payload.start_date
    ).first()

    if overlapping:
        raise HTTPException(status_code=400, detail="Employee already has an overlapping leave request")

    new_days = calculate_leave_days_for_request(
        payload.start_date,
        payload.end_date,
        payload.day_portion
    )

    old_leave_type = leave.leave_type
    old_start_date = leave.start_date
    old_end_date = leave.end_date
    old_days = float(leave.days or 0)
    old_status = leave.status

    try:
        if old_status == "APPROVED":
            reverse_leave_balance_effect(db, leave)

        leave.leave_type = leave_type
        leave.start_date = payload.start_date
        leave.end_date = payload.end_date
        leave.days = new_days

        if old_status == "APPROVED":
            apply_leave_balance_effect(db, leave)

        db.commit()
        db.refresh(leave)
    except Exception:
        db.rollback()
        raise

    employee = leave.employee or db.query(Employee).filter(Employee.id == leave.employee_id).first()
    if employee and employee.email:
        send_email_background(
            to=employee.email,
            subject="Leave Request Amended",
            body=f"""
            <h3>Your Leave Request Has Been Amended</h3>
            <p>Dear {employee.name},</p>
            <p>Your leave request has been updated by HR/Admin.</p>
            <ul>
                <li><b>Old:</b> {old_leave_type} | {old_start_date} to {old_end_date} | {old_days} day(s)</li>
                <li><b>New:</b> {leave.leave_type} | {leave.start_date} to {leave.end_date} | {leave.days} day(s)</li>
                <li><b>Status:</b> {leave.status}</li>
            </ul>
            <br>
            <p>Leave Management System</p>
            """
        )

    return {
        "message": "Leave amended successfully",
        "leave_id": leave.id,
        "status": leave.status,
        "days": leave.days
    }


@router.delete("/leave-request/{leave_id}")
def delete_leave_request(
    leave_id: int,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.status == "APPROVED":
        reverse_leave_balance_effect(db, leave)

    if leave.doctor_note_path:
        file_path = os.path.join(UPLOAD_DIR, os.path.basename(leave.doctor_note_path))
        if os.path.exists(file_path):
            os.remove(file_path)

    db.delete(leave)
    db.commit()

    return {"message": "Leave request deleted successfully"}

# =====================================================
# EMPLOYEES
# =====================================================

@router.get("/all-employees")
def get_all_employees(
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    query = db.query(Employee)

    if current_user.role in ["manager", "supervisor", "director"]:
        query = query.filter(Employee.reports_to == current_user.id)

    employees = query.order_by(Employee.name.asc()).all()
    today = date.today()

    return [
        {
            "id": e.id,
            "employee_code": e.employee_code or "N/A",
            "name": e.name,
            "email": e.email,
            "role": e.role,
            "department": e.department,
            "start_date": e.start_date,
            "end_date": e.end_date,
            "reports_to": e.reports_to,
            "company_id": e.company_id,
            "annual_leave_days": e.annual_leave_days or 15,
            "accrual_rate": getattr(e, "accrual_rate", 1.25) or 1.25,
            "max_entitled": getattr(e, "max_entitled", 15.0) or 15.0,
            "active": e.end_date is None or e.end_date > today
        }
        for e in employees
    ]


@router.post("/create-employee")
def create_employee(
    payload: EmployeeCreateRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    ensure_valid_role(payload.role)

    existing = db.query(Employee).filter(Employee.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="An employee with this email already exists")

    if payload.employee_code:
        code_exists = db.query(Employee).filter(Employee.employee_code == payload.employee_code).first()
        if code_exists:
            raise HTTPException(status_code=409, detail="An employee with this code already exists")

    company = db.query(Company).filter(Company.id == payload.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if payload.reports_to:
        manager = db.query(Employee).filter(Employee.id == payload.reports_to).first()
        if not manager:
            raise HTTPException(status_code=404, detail="The reports_to employee was not found")
        ensure_manager_role(manager)

    if payload.annual_leave_days not in range(1, 366):
        raise HTTPException(status_code=400, detail="Annual leave days must be between 1 and 365")

    sick_cycle_start = payload.start_date
    sick_cycle_end = payload.start_date + relativedelta(years=3)

    employee = Employee(
        company_id=payload.company_id,
        name=payload.name,
        email=payload.email,
        password=hash_password(payload.password),
        department=payload.department,
        start_date=payload.start_date,
        end_date=payload.end_date,
        role=payload.role,
        reports_to=payload.reports_to,
        employee_code=payload.employee_code or None,
        annual_leave_days=payload.annual_leave_days,
        accrual_rate=payload.accrual_rate,
        max_entitled=payload.max_entitled,
        sick_cycle_start=sick_cycle_start,
        sick_cycle_end=sick_cycle_end
    )

    db.add(employee)
    db.commit()
    db.refresh(employee)

    initial_annual = calculate_accrued_annual(
        payload.start_date,
        payload.accrual_rate,
        payload.max_entitled
    )

    balances = [
        LeaveBalance(
            company_id=payload.company_id,
            employee_id=employee.id,
            leave_type="Annual",
            allocated=initial_annual,
            used=0,
            remaining=initial_annual
        ),
        LeaveBalance(
            company_id=payload.company_id,
            employee_id=employee.id,
            leave_type="Sick",
            allocated=30,
            used=0,
            remaining=30
        ),
        LeaveBalance(
            company_id=payload.company_id,
            employee_id=employee.id,
            leave_type="Family Responsibility",
            allocated=3,
            used=0,
            remaining=3
        ),
        LeaveBalance(
            company_id=payload.company_id,
            employee_id=employee.id,
            leave_type="Study",
            allocated=0,
            used=0,
            remaining=0
        ),
        LeaveBalance(
            company_id=payload.company_id,
            employee_id=employee.id,
            leave_type="Unpaid",
            allocated=0,
            used=0,
            remaining=0
        ),
    ]

    db.add_all(balances)
    db.commit()

    return {"message": "Employee created successfully", "employee_id": employee.id}


@router.get("/employee/{employee_id}")
def get_employee(
    employee_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not can_view_employee(current_user, employee):
        raise HTTPException(status_code=403, detail="Not authorized to view this employee")

    return {
        "id": employee.id,
        "name": employee.name,
        "email": employee.email,
        "department": employee.department,
        "employee_code": employee.employee_code,
        "role": employee.role,
        "start_date": employee.start_date,
        "end_date": employee.end_date,
        "company_id": employee.company_id,
        "reports_to": employee.reports_to,
        "annual_leave_days": employee.annual_leave_days,
        "accrual_rate": getattr(employee, "accrual_rate", 1.25) or 1.25,
        "max_entitled": getattr(employee, "max_entitled", 15.0) or 15.0,
        "active": employee.end_date is None or employee.end_date > date.today()
    }


@router.post("/employee/{employee_id}/update")
def edit_employee(
    employee_id: int,
    payload: EmployeeUpdateRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    ensure_valid_role(payload.role)

    existing = db.query(Employee).filter(
        Employee.email == payload.email,
        Employee.id != employee_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use by another employee")

    if payload.employee_code:
        code_exists = db.query(Employee).filter(
            Employee.employee_code == payload.employee_code,
            Employee.id != employee_id
        ).first()
        if code_exists:
            raise HTTPException(status_code=409, detail="Employee code already in use by another employee")

    company = db.query(Company).filter(Company.id == payload.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if payload.reports_to:
        manager = db.query(Employee).filter(Employee.id == payload.reports_to).first()
        if not manager:
            raise HTTPException(status_code=404, detail="The reports_to employee was not found")
        ensure_manager_role(manager)

    employee.name = payload.name
    employee.email = payload.email
    employee.department = payload.department
    employee.role = payload.role
    employee.start_date = payload.start_date
    employee.end_date = payload.end_date
    employee.company_id = payload.company_id
    employee.reports_to = payload.reports_to
    employee.employee_code = payload.employee_code or None
    employee.annual_leave_days = payload.annual_leave_days
    employee.accrual_rate = payload.accrual_rate
    employee.max_entitled = payload.max_entitled

    if payload.new_password and payload.new_password.strip():
        employee.password = hash_password(payload.new_password.strip())

    db.commit()

    return {"message": "Employee updated successfully"}


@router.post("/employee/{employee_id}/reactivate")
def reactivate_employee(
    employee_id: int,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    employee.end_date = None
    db.commit()

    return {"message": "Employee reactivated successfully"}


@router.post("/employee/{employee_id}/deactivate")
def deactivate_employee(
    employee_id: int,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if employee.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")

    employee.end_date = date.today()
    db.commit()

    return {"message": "Employee deactivated successfully"}


@router.delete("/employee/{employee_id}")
def delete_employee(
    employee_id: int,
    current_user: Employee = Depends(require_admin),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if employee.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")

    db.query(LeaveBalance).filter(LeaveBalance.employee_id == employee_id).delete()
    db.query(LeaveRequest).filter(LeaveRequest.employee_id == employee_id).delete()

    db.delete(employee)
    db.commit()

    return {"message": "Employee deleted successfully"}

# =====================================================
# EMPLOYEE STAND-INS
# =====================================================

@router.get("/employee/{employee_id}/standins")
def get_employee_standins(
    employee_id: int,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    standins = db.query(EmployeeStandIn).filter(
        EmployeeStandIn.employee_id == employee_id
    ).order_by(EmployeeStandIn.order_no.asc()).all()

    result = []

    for s in standins:
        standin_emp = db.query(Employee).filter(
            Employee.id == s.standin_employee_id
        ).first()

        result.append({
            "id": s.id,
            "standin_employee_id": s.standin_employee_id,
            "standin_name": standin_emp.name if standin_emp else "Unknown",
            "order_no": s.order_no
        })

    return result


@router.post("/employee/{employee_id}/standins")
def save_employee_standins(
    employee_id: int,
    payload: EmployeeStandInRequest,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Remove old stand-ins
    db.query(EmployeeStandIn).filter(
        EmployeeStandIn.employee_id == employee_id
    ).delete()

    # Add new stand-ins
    for item in payload.standins:

        if item.standin_employee_id == employee_id:
            raise HTTPException(
                status_code=400,
                detail="Employee cannot stand in for themselves"
            )

        standin_employee = db.query(Employee).filter(
            Employee.id == item.standin_employee_id
        ).first()

        if not standin_employee:
            raise HTTPException(
                status_code=404,
                detail=f"Stand-in employee {item.standin_employee_id} not found"
            )

        standin = EmployeeStandIn(
            employee_id=employee_id,
            standin_employee_id=item.standin_employee_id,
            order_no=item.order_no
        )

        db.add(standin)

    db.commit()

    return {"message": "Stand-ins saved successfully"}

@router.get("/my-standin-requests")
def get_my_standin_requests(
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    requests = db.query(LeaveStandInRequest).filter(
        LeaveStandInRequest.standin_employee_id == current_user.id,
        LeaveStandInRequest.status == "NOTIFIED"
    ).all()

    result = []

    for r in requests:
        leave = db.query(LeaveRequest).filter(
            LeaveRequest.id == r.leave_request_id
        ).first()

        employee = db.query(Employee).filter(
            Employee.id == r.employee_id
        ).first()

        result.append({
            "id": r.id,
            "leave_request_id": r.leave_request_id,
            "employee_name": employee.name if employee else "Unknown",
            "leave_type": leave.leave_type if leave else "",
            "start_date": leave.start_date if leave else "",
            "end_date": leave.end_date if leave else "",
            "days": leave.days if leave else 0,
            "order_no": r.order_no,
            "status": r.status
        })

    return result


@router.post("/standin-request/{request_id}/accept")
def accept_standin_request(
    request_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    req = db.query(LeaveStandInRequest).filter(
        LeaveStandInRequest.id == request_id,
        LeaveStandInRequest.standin_employee_id == current_user.id
    ).first()

    if not req:
        raise HTTPException(status_code=404, detail="Stand-in request not found")

    req.status = "ACCEPTED"
    req.responded_at = datetime.now()

    db.commit()

    return {"message": "Stand-in accepted successfully"}


@router.post("/standin-request/{request_id}/decline")
def decline_standin_request(
    request_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    req = db.query(LeaveStandInRequest).filter(
        LeaveStandInRequest.id == request_id,
        LeaveStandInRequest.standin_employee_id == current_user.id
    ).first()

    if not req:
        raise HTTPException(status_code=404, detail="Stand-in request not found")

    req.status = "DECLINED"
    req.responded_at = datetime.now()

    next_req = db.query(LeaveStandInRequest).filter(
        LeaveStandInRequest.leave_request_id == req.leave_request_id,
        LeaveStandInRequest.status == "WAITING"
    ).order_by(LeaveStandInRequest.order_no.asc()).first()

    if next_req:
        next_req.status = "NOTIFIED"

    db.commit()

    return {"message": "Stand-in declined successfully"}

# =====================================================
# COMPANIES
# =====================================================

@router.get("/companies")
def get_companies(
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    companies = db.query(Company).order_by(Company.name.asc()).all()
    return [{"id": c.id, "name": c.name, "code": c.code} for c in companies]


@router.post("/companies")
def create_company(
    payload: CompanyCreate,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    existing_name = db.query(Company).filter(Company.name == payload.name).first()
    if existing_name:
        raise HTTPException(status_code=409, detail="A company with this name already exists")

    existing_code = db.query(Company).filter(Company.code == payload.code).first()
    if existing_code:
        raise HTTPException(status_code=409, detail="A company with this code already exists")

    company = Company(name=payload.name, code=payload.code)
    db.add(company)
    db.commit()
    db.refresh(company)

    return {"message": "Company created successfully", "company_id": company.id}


@router.delete("/companies/{company_id}")
def delete_company(
    company_id: int,
    current_user: Employee = Depends(require_admin),
    db: Session = Depends(get_db)
):
    company = db.query(Company).filter(Company.id == company_id).first()

    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    employees_exist = db.query(Employee).filter(Employee.company_id == company_id).first()
    if employees_exist:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete company while employees are still linked to it"
        )

    db.delete(company)
    db.commit()

    return {"message": "Company deleted successfully"}

# =====================================================
# DOCTOR NOTES
# =====================================================

@router.post("/upload-doctor-note/{leave_id}")
def upload_doctor_note(
    leave_id: int,
    file: UploadFile = File(...),
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.id == leave_id
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if not can_view_leave(current_user, leave):
        raise HTTPException(status_code=403, detail="Not authorized to upload for this leave request")

    if leave.employee_id != current_user.id and current_user.role not in ["admin", "hr"]:
        raise HTTPException(status_code=403, detail="Only the employee, HR, or Admin may upload doctor notes")

    ext = os.path.splitext(file.filename)[1].lower()
    allowed_exts = [".pdf", ".jpg", ".jpeg", ".png"]
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Only PDF, JPG, JPEG, and PNG files are allowed")

    unique_name = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    if leave.doctor_note_path:
        old_path = os.path.join(UPLOAD_DIR, os.path.basename(leave.doctor_note_path))
        if os.path.exists(old_path):
            os.remove(old_path)

    leave.doctor_note_path = file_path
    db.commit()

    return {"message": "Doctor note uploaded successfully"
}


@router.get("/doctor-note/{leave_id}")
def get_doctor_note(
    leave_id: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    leave = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.id == leave_id
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if not can_view_leave(current_user, leave):
        raise HTTPException(status_code=403, detail="Not authorized to view this doctor note")

    if not leave.doctor_note_path:
        raise HTTPException(status_code=404, detail="No doctor note uploaded for this leave request")

    if not os.path.exists(leave.doctor_note_path):
        raise HTTPException(status_code=404, detail="Doctor note file not found on server")

    return FileResponse(
        path=leave.doctor_note_path,
        filename=os.path.basename(leave.doctor_note_path)
    )


def calculate_overlap_days(leave: LeaveRequest, start: date, end: date) -> float:
    """
    Calculates how many days of a leave fall within a selected period.
    """

    if leave.status != "APPROVED":
        return 0.0

    if leave.end_date < start or leave.start_date > end:
        return 0.0

    overlap_start = max(leave.start_date, start)
    overlap_end = min(leave.end_date, end)

    # If single-day fractional leave
    if overlap_start == overlap_end and float(leave.days) in [0.25, 0.5, 1.0]:
        return float(leave.days)

    return float(count_workdays(overlap_start, overlap_end))

# =====================================================
# TEAM CALENDAR
# =====================================================

@router.get("/team-calendar")
def get_team_calendar(
    year: int,
    month: int,
    manager_id: Optional[int] = None,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be between 1 and 12")

    effective_manager_id = get_effective_manager_id(current_user, manager_id)

    manager = db.query(Employee).filter(Employee.id == effective_manager_id).first()
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    ensure_manager_role(manager)

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

   
    team = db.query(Employee).filter(Employee.reports_to == effective_manager_id).all()
    team_ids = [e.id for e in team]

    if not team_ids:
        return []

    leaves = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.employee_id.in_(team_ids),
        LeaveRequest.status == "APPROVED",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()

    return [
        {
            "id": l.id,
            "employee_id": l.employee_id,
            "employee_name": l.employee.name if l.employee else "Unknown",
            "leave_type": l.leave_type,
            "start_date": l.start_date,
            "end_date": l.end_date,
            "days": l.days,
            "leave_code": l.leave_code,
        }
        for l in leaves
    ]

@router.get("/team-calendar/summary")
def get_team_calendar_summary(
    year: int,
    month: int,
    manager_id: Optional[int] = None,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be between 1 and 12")

    effective_manager_id = get_effective_manager_id(current_user, manager_id)

    manager = db.query(Employee).filter(Employee.id == effective_manager_id).first()
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    ensure_manager_role(manager)

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    team = db.query(Employee).filter(Employee.reports_to == effective_manager_id).all()
    team_ids = [e.id for e in team]

    if not team_ids:
        return {
            "total_days_off": 0,
            "staff_on_leave": 0,
            "annual_days": 0,
            "sick_days": 0
        }

    leaves = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id.in_(team_ids),
        LeaveRequest.status == "APPROVED",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()

    def clipped_workdays(l):
        s = max(l.start_date, first_day)
        e = min(l.end_date, last_day)

        if s == e and float(l.days) in [0.25, 0.5, 1.0]:
            return float(l.days)

        return count_workdays(s, e)

    total_days = sum(clipped_workdays(l) for l in leaves)
    annual_days = sum(clipped_workdays(l) for l in leaves if l.leave_type == "Annual")
    sick_days = sum(clipped_workdays(l) for l in leaves if l.leave_type == "Sick")
    staff_count = len(set(l.employee_id for l in leaves))

    return {
        "total_days_off": total_days,
        "staff_on_leave": staff_count,
        "annual_days": annual_days,
        "sick_days": sick_days
    }


@router.get("/team-calendar/today")
def get_team_on_leave_today(
    manager_id: Optional[int] = None,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    effective_manager_id = get_effective_manager_id(current_user, manager_id)

    today = date.today()

    manager = db.query(Employee).filter(Employee.id == effective_manager_id).first()
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    ensure_manager_role(manager)

    team = db.query(Employee).filter(Employee.reports_to == effective_manager_id).all()
    team_ids = [e.id for e in team]

    if not team_ids:
        return []

    leaves = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.employee_id.in_(team_ids),
        LeaveRequest.status == "APPROVED",
        LeaveRequest.start_date <= today,
        LeaveRequest.end_date >= today
    ).all()

    return [
        {
            "employee_name": l.employee.name if l.employee else "Unknown",
            "initials": "".join(p[0].upper() for p in (l.employee.name or "??").split()[:2]),
            "leave_type": l.leave_type
        }
        for l in leaves
    ]


@router.get("/team-calendar/balances")
def get_team_leave_balances(
    manager_id: Optional[int] = None,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    effective_manager_id = get_effective_manager_id(current_user, manager_id)

    manager = db.query(Employee).filter(Employee.id == effective_manager_id).first()
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")
    ensure_manager_role(manager)

    team = db.query(Employee).filter(Employee.reports_to == effective_manager_id).all()

    result = []

    
    for emp in team:
        annual_balance = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id,
            LeaveBalance.leave_type == "Annual"
        ).first()

        sick_balance = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id,
            LeaveBalance.leave_type == "Sick"
        ).first()

        family_balance = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id,
            LeaveBalance.leave_type == "Family Responsibility"
        ).first()

        study_balance = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id,
            LeaveBalance.leave_type == "Study"
        ).first()

        annual_allocated = (
            float(annual_balance.allocated or emp.max_entitled or emp.annual_leave_days or 15)
            if annual_balance else float(emp.max_entitled or emp.annual_leave_days or 15)
        )

        annual_used = get_annual_used_this_year(db, emp.id)
        annual_remaining = float(annual_balance.remaining or 0) if annual_balance else 0

        result.append({
            "employee_id": emp.id,
            "employee_name": emp.name,
            "employee_code": emp.employee_code or "—",
            "annual_allocated": round(annual_allocated, 2),
            "annual_used": round(annual_used, 2),
            "annual_remaining": round(annual_remaining, 2),
            "sick_remaining": float(sick_balance.remaining or 0) if sick_balance else 0,
            "family_remaining": float(family_balance.remaining or 0) if family_balance else 0,
            "study_remaining": float(study_balance.remaining or 0) if study_balance else 0,
        })

    return result

@router.get("/team/leave-report/download")
def download_team_leave_report(
    start_date: date,
    end_date: date,
    manager_id: Optional[int] = None,
    current_user: Employee = Depends(require_manager_level),
    db: Session = Depends(get_db)
):
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    effective_manager_id = get_effective_manager_id(current_user, manager_id)

    manager = db.query(Employee).filter(Employee.id == effective_manager_id).first()
    if not manager:
        raise HTTPException(status_code=404, detail="Manager not found")

    ensure_manager_role(manager)

    # 🔥 ONLY DIRECT REPORTS
    team = db.query(Employee).filter(Employee.reports_to == effective_manager_id).all()

    if not team:
        raise HTTPException(status_code=404, detail="No team members found")

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code",
        "Employee Name",
        "Department",
        "Leave Type",
        "Start Date",
        "End Date",
        "Days",
        "Status"
    ])

    for emp in team:
        leaves = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date
        ).all()

        for l in leaves:
            writer.writerow([
                emp.employee_code or "",
                emp.name,
                emp.department,
                l.leave_type,
                l.start_date,
                l.end_date,
                l.days,
                l.status
            ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=team_leave_report_{start_date}_to_{end_date}.csv"
        }
    )

@router.get("/company-calendar")
def get_company_calendar(
    year: int,
    month: int,
    current_user: Employee = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be between 1 and 12")

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    leaves = db.query(LeaveRequest).options(joinedload(LeaveRequest.employee)).filter(
        LeaveRequest.status == "APPROVED",
        LeaveRequest.start_date <= last_day,
        LeaveRequest.end_date >= first_day
    ).all()

    return [
        {
            "id": l.id,
            "employee_id": l.employee_id,
            "employee_name": l.employee.name if l.employee else "Unknown",
            "leave_type": l.leave_type,
            "start_date": str(l.start_date),
            "end_date": str(l.end_date),
            "days": l.days,
            "leave_code": l.leave_code,
        }
        for l in leaves
    ]

@router.put("/leave-request/{request_id}/amend")
def amend_pending_leave(
    request_id: int,
    data: LeaveRequestCreate,
    db: Session = Depends(get_db),
    current_user: Employee = Depends(get_current_active_user)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == request_id).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.employee_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only amend your own leave")

    if leave.status != "PENDING":
        raise HTTPException(status_code=400, detail="Only pending leave can be amended")

    leave.leave_type = data.leave_type
    leave.start_date = data.start_date
    leave.end_date = data.end_date
    leave.days = calculate_leave_days_for_request(
         data.start_date,
         data.end_date,
         data.day_portion
    )
    leave.reason = data.reason

    db.commit()
    db.refresh(leave)

    return {"message": "Leave request amended successfully", "leave": leave}

@router.delete("/leave-request/{request_id}/cancel")
def cancel_pending_leave(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: Employee = Depends(get_current_active_user)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == request_id).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.employee_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only cancel your own leave")

    if leave.status != "PENDING":
        raise HTTPException(status_code=400, detail="Only pending leave can be cancelled")

    db.delete(leave)
    db.commit()

    return {"message": "Pending leave request cancelled successfully"}

@router.get("/hr/leave-report/download")
def download_leave_report(
    start_date: date,
    end_date: date,
    current_user: Employee = Depends(require_hr_or_admin),
    db: Session = Depends(get_db)
):
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="End date cannot be before start date")

    employees = db.query(Employee).options(joinedload(Employee.company)).all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Employee Code",
        "Employee Name",
        "Department",
        "Company",
        "Annual Allocated",
        "Annual Taken",
        "Annual Remaining",
        "Sick Taken",
        "Sick Remaining",
        "Family Taken",
        "Family Remaining",
        "Total Taken",
        "Start Date",
        "End Date"
    ])

    for emp in employees:

        balances = db.query(LeaveBalance).filter(
            LeaveBalance.employee_id == emp.id
        ).all()

        balance_map = {b.leave_type: b for b in balances}

        annual = balance_map.get("Annual")
        sick = balance_map.get("Sick")
        family = balance_map.get("Family Responsibility")

        leaves = db.query(LeaveRequest).filter(
            LeaveRequest.employee_id == emp.id,
            LeaveRequest.status == "APPROVED",
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date
        ).all()

        annual_taken = 0
        sick_taken = 0
        family_taken = 0

        for l in leaves:
            days = calculate_overlap_days(l, start_date, end_date)

            if l.leave_type == "Annual":
                annual_taken += days
            elif l.leave_type == "Sick":
                sick_taken += days
            elif l.leave_type == "Family Responsibility":
                family_taken += days

        total_taken = annual_taken + sick_taken + family_taken

        writer.writerow([
            emp.employee_code or "",
            emp.name,
            emp.department,
            emp.company.name if emp.company else "",
            round(float(annual.allocated if annual else 0), 2),
            round(annual_taken, 2),
            round(float(annual.remaining if annual else 0), 2),
            round(sick_taken, 2),
            round(float(sick.remaining if sick else 0), 2),
            round(family_taken, 2),
            round(float(family.remaining if family else 0), 2),
            round(total_taken, 2),
            start_date,
            end_date
        ])

    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=leave_report_{start_date}_to_{end_date}.csv"
        }
    )


    return result