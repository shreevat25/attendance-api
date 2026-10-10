"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Layout of this file:
  1. Configuration and MongoDB client
  2. Time helpers (epoch ms <-> datetime, IST, R1 attendance date)
  3. Business-rule helpers (R2 - R5)
  4. Request models and validation
  5. Serialisation (MongoDB document -> API shape)
  6. Query / pipeline builders (shared by the endpoints and by /admin/explain)
  7. Endpoints
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration and MongoDB client
# --------------------------------------------------------------------------- #
load_dotenv(override=False)  # never overrides variables that are already set

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ABSENCE_STATUSES = ["ABSENT", "LEAVE"]
GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when an identical index already exists."""
    # Uniqueness of emp_code is a database guarantee (race-safe POST /employees).
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    # GET /employees?department=..  (filter + sort by emp_code); trend's $match on department.
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    # Department summary: employees with joined_on <= end of month (R9).
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")

    # One record per (emp_code, date): makes punch-in race-safe, addresses PATCH, and serves every
    # per-employee date-range query (monthly summary, punch-out lookup, $lookup from employees).
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_code_date")
    # GET /attendance default sort (date desc, emp_code asc) and month-wide date ranges (leaderboard).
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")
    # GET /attendance?status=..  filter + same sort, so total/page never scan the collection.
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_desc_emp_code"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def unprocessable(msg: str, loc: tuple = ("body",)) -> RequestValidationError:
    """A 422 with FastAPI's default validation body: {"detail": [ {loc, msg, type} ]}."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(h, m)


def is_overnight(emp: dict) -> bool:
    return parse_hhmm(emp["shift_end"]) <= parse_hhmm(emp["shift_start"])


def at_ist(day: date, hhmm: str) -> datetime:
    """The instant of HH:MM IST on a given calendar day."""
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: the IST calendar date of the punch-in, except that on an overnight shift a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_end_instant(record_date: date, emp: dict) -> datetime:
    """Shift end for the shift that started on record_date (next day for overnight shifts)."""
    end_day = record_date + timedelta(days=1) if is_overnight(emp) else record_date
    return at_ist(end_day, emp["shift_end"])


def parse_day(value: str, loc: tuple) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise unprocessable("must be a date in YYYY-MM-DD format", loc)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise unprocessable("is not a valid calendar date", loc)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_weekdays(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] (R7). 0 when the range is empty."""
    if last < first:
        return 0
    days = (last - first).days + 1
    full_weeks, rest = divmod(days, 7)
    extra = sum(1 for i in range(rest) if (first + timedelta(days=i)).weekday() < 5)
    return full_weeks * 5 + extra


# --------------------------------------------------------------------------- #
# 3. Business-rule helpers (R2 - R5)
# --------------------------------------------------------------------------- #
def round_half_up(value, places: int = 2) -> float:
    """R4/R8: decimal half-up rounding (Python's round() is banker's rounding on a binary float)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, record_date: date, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; minutes counted from shift start."""
    elapsed = (punch_in - at_ist(record_date, emp["shift_start"])).total_seconds()
    return int(elapsed // 60) if elapsed > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up. Done in Decimal so 4.125 -> 4.13, not 4.12."""
    seconds = Decimal(int((punch_out - punch_in).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, record_date: date, emp: dict) -> int:
    """R3: whole minutes after shift end, only when at least 30; otherwise 0."""
    minutes = int((punch_out - shift_end_instant(record_date, emp)).total_seconds() // 60)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derived_fields(status: str, punch_in, punch_out, record_date: date, emp: dict) -> dict:
    """Every derived field for a record, per the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(punch_in, record_date, emp),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = compute_overtime(punch_out, record_date, emp)
        out["half_day"] = Decimal(str(hours)) < HALF_DAY_HOURS  # R5 uses the rounded value
    return out


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
# strict=True: "1783312500000" (string), 1783312500000.0 (float) and true are rejected; ge rejects seconds.
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
MonthParam = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("must be a date in YYYY-MM-DD format")
        date.fromisoformat(v)  # raises ValueError for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    """Derived fields are not declared, so a client that sends them is ignored (pydantic drops extras)."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # The fields are optional but not nullable: {"punch_in": null} is a bad request, not "unchanged".
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} may be omitted but not null")
        return self


# --------------------------------------------------------------------------- #
# 5. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _plain(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entry: dict) -> dict:
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": {
            field: {"from": _plain(c.get("from")), "to": _plain(c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Missing legacy fields get their documented defaults; _id never leaves the server."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 6. Query / pipeline builders
#    Each endpoint and /admin/explain call the same builder, so the explained plan is the real one.
# --------------------------------------------------------------------------- #
D_ONE = Decimal128("1")
D_HALF = Decimal128("0.5")

# Aggregation expressions over one attendance_logs document. Missing fields get their documented defaults.
IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
LATE = {"$ifNull": ["$late_minutes", 0]}
PRESENCE_VALUE = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}  # R5: a half day is 0.5
PRESENT_ANY_DAY = {"$cond": [IS_PRESENT, PRESENCE_VALUE, 0]}
PRESENT_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENCE_VALUE, 0]}  # R6 + R7
IS_LATE = {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


def round_expr(expr, places: int) -> dict:
    """Half-up rounding inside MongoDB ($round is round-half-to-even). Works in Decimal128 so 0.125 is
    exactly 0.125; the result is converted back to a double for JSON. Null in, null out."""
    scale = Decimal128(str(10**places))
    scaled = {"$multiply": [{"$toDecimal": expr}, scale]}
    return {"$toDouble": {"$divide": [{"$floor": {"$add": [scaled, D_HALF]}}, scale]}}


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def employee_monthly_pipeline(emp_code: str, month: str, working_days: int) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_WEEKDAY},
                "leave_days": count_if({"$eq": ["$status", "LEAVE"]}),
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "present_days": round_expr("$present_days", 2),
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": (
                    round_expr({"$divide": [{"$multiply": [{"$toDecimal": "$present_days"}, 100]}, working_days]}, 2)
                    if working_days > 0
                    else None
                ),
            }
        },
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with zero logs still produce a row (R9), then pulls each
    employee's month of logs with an index-backed $lookup on (emp_code, date)."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": PRESENT_WEEKDAY},
                            # Sum and count, not an average: the department average is over records.
                            "wh_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
                            "wh_count": count_if(HAS_WORK_HOURS),
                            "late_count": {"$sum": IS_LATE},
                            "total_late_minutes": {"$sum": LATE},
                            "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
                            "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
                        }
                    },
                ],
                "as": "stats",
            }
        },
        # Employees without logs keep their row (stats missing -> $sum treats it as 0).
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$stats.present_days"},
                "wh_sum": {"$sum": "$stats.wh_sum"},
                "wh_count": {"$sum": "$stats.wh_count"},
                "late_count": {"$sum": "$stats.late_count"},
                "total_late_minutes": {"$sum": "$stats.total_late_minutes"},
                "leave_count": {"$sum": "$stats.leave_count"},
                "on_duty_count": {"$sum": "$stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": round_expr("$present_days", 2),
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_count", 0]},
                        round_expr({"$divide": ["$wh_sum", "$wh_count"]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE}, "late_count": {"$sum": IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}],
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},  # drops logs whose employee does not exist
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties at the cutoff stay
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    """Runs on `employees` of the department. Each employee contributes one "joined" event (+1 headcount
    on max(joined_on, start)) and one event per log in the range. Events are summed per day, $densify
    fills the missing days, and two window functions give the running headcount and the 7-day average."""
    first, last = start.isoformat(), end.isoformat()
    lower = datetime.combine(start, time(), tzinfo=timezone.utc)
    upper = datetime.combine(end + timedelta(days=1), time(), tzinfo=timezone.utc)  # bounds are [lower, upper)
    joined_in_range = {"$lte": ["$joined_on", last]}
    join_event = {
        # Employees who join after the range still emit a zero event on `start`, so the pipeline always
        # has at least one document for $densify to work from.
        "date": {"$cond": [joined_in_range, {"$max": ["$joined_on", first]}, first]},
        "hc": {"$cond": [joined_in_range, 1, 0]},
        "present": 0,
        "late": 0,
    }
    return [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$project": {"_id": 0, "date": 1, "present": PRESENT_ANY_DAY, "late": IS_LATE}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [join_event],
                        {"$map": {"input": "$logs", "as": "l", "in": {
                            "date": "$$l.date", "hc": 0, "present": "$$l.present", "late": "$$l.late"}}},
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {
            "$group": {
                "_id": "$events.date",
                "joined": {"$sum": "$events.hc"},
                "present_count": {"$sum": "$events.present"},
                "late_count": {"$sum": "$events.late"},
            }
        },
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        # Gap filling: one document per calendar day in [from, to], weekends and empty days included.
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        round_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                # $avg skips nulls and returns null when every value in the window is null.
                "output": {"moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": round_expr("$moving_avg", 4),
            }
        },
        {"$sort": {"date": 1}},
    ]


def validate_trend_range(from_: str, to: str) -> tuple[date, date]:
    start = parse_day(from_, ("query", "from"))
    end = parse_day(to, ("query", "to"))
    if end < start:
        raise unprocessable("`to` must not be before `from`", ("query", "to"))
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"the range may not exceed {MAX_TREND_DAYS} days", ("query", "to"))
    return start, end


def validate_list_dates(date_from: Optional[str], date_to: Optional[str]) -> None:
    start = parse_day(date_from, ("query", "date_from")) if date_from else None
    end = parse_day(date_to, ("query", "date_to")) if date_to else None
    if start and end and start > end:
        raise unprocessable("date_from must not be after date_to", ("query", "date_from"))


def monthly_working_days(emp: Optional[dict], month: str) -> int:
    first, last = month_bounds(month)
    if emp is None:
        return 0
    joined = date.fromisoformat(emp["joined_on"])
    return count_weekdays(max(first, joined), last)  # R7: count from joined_on for mid-month joiners


# --------------------------------------------------------------------------- #
# 7. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})
    return {"status": "ok"}


# ---- employees ------------------------------------------------------------ #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        # The unique (emp_code, date) index makes this atomic: of two identical concurrent requests,
        # exactly one insert succeeds and the other raises DuplicateKeyError.
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch-in is at or before the punch-out. Records are ordered by date,
    # which follows punch_in, so the (emp_code, date) index answers this without a sort in memory.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise unprocessable("punched_at must be after punch_in", ("body", "punched_at"))
    if ts - rec["punch_in"] > MAX_SHIFT:
        raise unprocessable("punched_at is more than 24 hours after punch_in", ("body", "punched_at"))

    derived = derived_fields(rec["status"], rec["punch_in"], ts, date.fromisoformat(rec["date"]), emp)
    # Conditional update: only succeeds while punch_out is still null, so of two concurrent
    # punch-outs exactly one wins and the other gets 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    validate_list_dates(date_from, date_to)
    spec = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(spec["filter"])
    cursor = logs.find(spec["filter"]).sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
FIELD_DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, day: Annotated[str, Path(alias="date")], body: RegularizeIn):
    record_day = parse_day(day, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old = {f: rec.get(f, FIELD_DEFAULTS.get(f)) for f in TRACKED_FIELDS}
    status = body.status or old["status"]

    if status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"{status} clears punch times; do not send punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise unprocessable(f"status {status} requires a punch_in", ("body", "punch_in"))
        if attendance_date(new_in, emp) != record_day:
            raise unprocessable("punch_in must stay on the record's attendance date", ("body", "punch_in"))
        if new_out is not None and not (new_in < new_out <= new_in + MAX_SHIFT):
            raise unprocessable("punch_out must be after punch_in and within 24 hours", ("body", "punch_out"))

    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **derived_fields(status, new_in, new_out, record_day, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise unprocessable("the request does not change anything")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only applies if the record is still exactly as we read it
    # (same history length and same punch data). A concurrent edit makes this match nothing -> 409,
    # so no history entry is ever lost.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    updated = logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was changed concurrently; reload and retry")
    return record_out(updated)


# ---- analytics ------------------------------------------------------------ #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    emp = get_employee_or_404(emp_code)
    working_days = monthly_working_days(emp, month)
    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, month, working_days)))
    stats = rows[0] if rows else {
        "present_days": 0.0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0,
        "total_overtime_minutes": 0, "attendance_pct": 0.0 if working_days > 0 else None,
    }
    return {"emp_code": emp_code, "month": month, "working_days": working_days, **stats}


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthParam, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None
):
    items = list(logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    start, end = validate_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(department_trend_pipeline(department, start, end)))
    return {"department": department, "items": items}


# ---- admin ---------------------------------------------------------------- #
def _require(value, name: str):
    if value is None:
        raise unprocessable(f"`{name}` is required for this endpoint", ("query", name))
    return value


def _explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))  # BSON types (dates, Decimal128, Int64) -> plain JSON


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    if endpoint == "attendance_list":
        validate_list_dates(date_from, date_to)
        spec = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection = logs.name
        command = {"find": collection, **spec}
    elif endpoint == "employee_monthly":
        code, mon = _require(emp_code, "emp_code"), _require(month, "month")
        emp = employees.find_one({"emp_code": code})
        collection = logs.name
        command = {"aggregate": collection, "pipeline": employee_monthly_pipeline(
            code, mon, monthly_working_days(emp, mon)), "cursor": {}}
    elif endpoint == "department_summary":
        collection = employees.name
        command = {"aggregate": collection,
                   "pipeline": department_summary_pipeline(_require(month, "month"), department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        collection = logs.name
        command = {"aggregate": collection,
                   "pipeline": late_leaderboard_pipeline(_require(month, "month"), limit, department), "cursor": {}}
    else:  # department_trend
        dept = _require(department, "department")
        start, end = validate_trend_range(_require(from_, "from"), _require(to, "to"))
        collection = employees.name
        command = {"aggregate": collection, "pipeline": department_trend_pipeline(dept, start, end), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}
