from sqlalchemy import Column, Integer, String, ForeignKey, Date, Float, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy import Boolean
from .database import Base
from typing import Optional
from sqlalchemy import String
from sqlalchemy import Float

sick_flag = Column(Boolean, default=False)
sick_flag_reason = Column(String, nullable=True)

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    code = Column(String, unique=True, nullable=False)

class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    user_id = Column(Integer, nullable=True)        # ← old column, keep it
    manager_id = Column(Integer, nullable=True)     # ← old column, keep it
    employee_code = Column(String(50), unique=True, nullable=True)  # ← NEW
    annual_leave_days = Column(Integer, default=15, nullable=False)  # ← 15, 17 or 20
    accrual_rate      = Column(Float, default=1.25, nullable=True)   # ← HR inputs e.g. 1.5 days/month
    max_entitled      = Column(Float, default=15.0, nullable=True)   # ← HR inputs e.g. 15 days max
    sick_cycle_start  = Column(Date, nullable=True)   # ← start of 3-year sick leave cycle
    sick_cycle_end    = Column(Date, nullable=True)   # ← end of 3-year sick leave cycle
    name = Column(String, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    department = Column(String)
    start_date = Column(Date)
    end_date = Column(Date, nullable=True)
    role = Column(String(20), default="employee")
    reports_to = Column(Integer, ForeignKey("employees.id"), nullable=True)


    leave_requests = relationship(
        "LeaveRequest",
        back_populates="employee",
        foreign_keys="[LeaveRequest.employee_id]"
    )
    company = relationship("Company", foreign_keys=[company_id])

class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"))
    approver_id = Column(Integer, ForeignKey("employees.id"), nullable=True)

    leave_code = Column(String(4), unique=True, nullable=True)

    leave_type = Column(String, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    days = Column(Float, nullable=False)

    status = Column(String, default="PENDING")
    doctor_note_path = Column(String, nullable=True)

    reason = Column(String, nullable=True)
    sick_flag = Column(Boolean, default=False)
    sick_flag_reason = Column(String, nullable=True)

    employee = relationship(
        "Employee",
        back_populates="leave_requests",
        foreign_keys=[employee_id]
    )

class LeaveBalance(Base):
    __tablename__ = "leave_balances"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    employee_id = Column(Integer, ForeignKey("employees.id"))
    leave_type = Column(String, nullable=False)
    allocated = Column(Float, default=0)
    used = Column(Float, default=0)
    remaining = Column(Float, default=0)

class EmployeeStandIn(Base):
    __tablename__ = "employee_standins"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    standin_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    order_no = Column(Integer, nullable=False)  # 1, 2, 3

class LeaveStandInRequest(Base):
    __tablename__ = "leave_standin_requests"

    id = Column(Integer, primary_key=True, index=True)

    leave_request_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    standin_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    order_no = Column(Integer, nullable=False)
    status = Column(String, default="WAITING")  # WAITING, NOTIFIED, ACCEPTED, DECLINED
    responded_at = Column(DateTime, nullable=True)

class OvertimeOffBalance(Base):
    __tablename__ = "overtime_off_balances"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    total_hours = Column(Float, default=0)
    hours_taken = Column(Float, default=0)