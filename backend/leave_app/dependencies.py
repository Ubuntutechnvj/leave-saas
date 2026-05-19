from typing import Callable
from datetime import date

from fastapi import Depends, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from .auth import oauth2_scheme, SECRET_KEY, ALGORITHM
from .database import get_db
from .models import Employee


VALID_ROLES = {"admin", "hr", "director", "manager", "supervisor", "employee"}


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> Employee:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        employee_id = payload.get("sub")
        token_role = payload.get("role")

        if employee_id is None:
            raise credentials_exception

        try:
            employee_id = int(employee_id)
        except (TypeError, ValueError):
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    user = db.query(Employee).filter(Employee.id == employee_id).first()
    if user is None:
        raise credentials_exception

    if token_role and (user.role or "").lower() != str(token_role).lower():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your permissions changed. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if (user.role or "").lower() not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid user role"
        )

    return user


def get_current_active_user(
    current_user: Employee = Depends(get_current_user)
) -> Employee:
    if current_user.end_date and current_user.end_date <= date.today():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is inactive. Please contact HR."
        )
    return current_user


def require_roles(*allowed_roles: str) -> Callable:
    allowed = {role.lower() for role in allowed_roles}

    def role_checker(
        current_user: Employee = Depends(get_current_active_user)
    ) -> Employee:
        user_role = (current_user.role or "").lower()

        if user_role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action"
            )
        return current_user

    return role_checker


def require_admin(
    current_user: Employee = Depends(require_roles("admin"))
) -> Employee:
    return current_user


def require_hr_or_admin(
    current_user: Employee = Depends(require_roles("hr", "admin"))
) -> Employee:
    return current_user


def require_manager_level(
    current_user: Employee = Depends(require_roles("manager", "supervisor", "hr", "director", "admin"))
) -> Employee:
    return current_user