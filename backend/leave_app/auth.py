from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .models import Employee

SECRET_KEY = "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Must match your actual login route
# Because main.py uses:
# app.include_router(leave_router, prefix="/api")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    to_encode.update({"exp": expire})

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def authenticate_user(db: Session, email: str, password: str) -> Optional[Employee]:
    employee = db.query(Employee).filter(Employee.email == email).first()

    print("LOGIN ATTEMPT EMAIL:", email)
    print("EMPLOYEE FOUND:", employee.email if employee else None)

    if not employee:
        print("RESULT: user not found")
        return None

    print("STORED HASH:", employee.password)

    if not employee.password:
        print("RESULT: no password stored")
        return None

    is_valid = verify_password(password, employee.password)
    print("PASSWORD MATCH:", is_valid)

    if not is_valid:
        print("RESULT: password check failed")
        return None

    print("RESULT: login success")
    return employee