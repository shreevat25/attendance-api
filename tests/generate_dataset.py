"""Fills MONGO_DB with ~100,000 attendance records for load and explain checks.

    MONGO_DB=attendance_load python tests/generate_dataset.py

400 employees in 8 departments, 250 days each (weekends included), random statuses, late minutes,
half days and overnight shifts; a few employees have no logs at all and some join mid-year.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))[os.getenv("MONGO_DB", "attendance_load")]
IST = timezone(timedelta(hours=5, minutes=30))
rng = random.Random(42)

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Ops", "Legal", "Marketing"]
START = date(2026, 1, 1)
DAYS = 250

emps, logs = [], []
for i in range(1, 401):
    code = f"EMP{i:04d}"
    overnight = i % 10 == 0
    joined = START + timedelta(days=rng.choice([0, 0, 0, 30, 95, 190]))
    emps.append({"emp_code": code, "name": f"Employee {i}", "email": f"e{i}@example.com",
                 "department": DEPARTMENTS[i % len(DEPARTMENTS)],
                 "shift_start": "22:00" if overnight else "09:30", "shift_end": "06:00" if overnight else "18:30",
                 "joined_on": joined.isoformat(), "created_at": datetime.now(timezone.utc)})
    if i % 50 == 0:
        continue  # employees with no logs
    for d in range(DAYS):
        day = START + timedelta(days=d)
        status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [70, 15, 5, 5, 5])[0]
        doc = {"emp_code": code, "date": day.isoformat(), "status": status, "punch_in": None, "punch_out": None,
               "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        if status in ("PRESENT", "WFH", "ON_DUTY"):
            start = datetime(day.year, day.month, day.day, 22 if overnight else 9, 30, tzinfo=IST)
            pin = start + timedelta(minutes=rng.randint(-20, 60))
            hours = rng.choice([3.5, 8, 8.5, 9, 9.25, 10])
            late = int((pin - start).total_seconds() // 60)
            doc.update(punch_in=pin, punch_out=pin + timedelta(hours=hours), work_hours=hours,
                       late_minutes=late if late * 60 > 600 else 0, half_day=hours < 4.5,
                       overtime_minutes=rng.choice([0, 0, 0, 35, 60]))
        logs.append(doc)

db.employees.drop()
db.attendance_logs.drop()
db.employees.insert_many(emps)
for k in range(0, len(logs), 10_000):
    db.attendance_logs.insert_many(logs[k:k + 10_000])
print(f"{len(emps)} employees, {len(logs)} attendance records in '{db.name}'")
