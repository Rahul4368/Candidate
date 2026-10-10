"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import ObjectId, json_util

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: Optional[int]) -> datetime:
    if value is None:
        return now_utc()
    if isinstance(value, bool) or not isinstance(value, int) or value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(422, "invalid timestamp")


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        d = date.fromisoformat(value)
        if d.isoformat() != value:
            raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start.isoformat(), end.isoformat(), start, end


def shift_dt(day: date, shift: str) -> datetime:
    h, m = map(int, shift.split(":"))
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def attendance_day(punch: datetime, emp: dict) -> date:
    local = punch.astimezone(IST)
    start = emp["shift_start"]
    end = emp["shift_end"]
    if end <= start and local.strftime("%H:%M") < end:
        return local.date() - timedelta(days=1)
    return local.date()


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    local = punch_in.astimezone(IST)
    shift_day = local.date()
    if shift_end and shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        shift_day -= timedelta(days=1)
    h, m = map(int, shift_start.split(":"))
    scheduled = datetime.combine(shift_day, time(h, m), IST)
    elapsed_seconds = (local - scheduled).total_seconds()
    return max(0, int(elapsed_seconds // 60)) if elapsed_seconds > 600 else 0


def shift_end_instant(attendance_date: str, emp: dict) -> datetime:
    d = parse_date(attendance_date)
    end = emp["shift_end"]
    if end <= emp["shift_start"]:
        d += timedelta(days=1)
    return shift_dt(d, end)


def compute_overtime(punch_out: datetime, attendance_date: str, emp: dict) -> int:
    delta = int((punch_out - shift_end_instant(attendance_date, emp)).total_seconds() // 60)
    return delta if delta >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff_sec = Decimal(str(int((punch_out - punch_in).total_seconds())))
    return rounded(diff_sec / Decimal(3600), 2)


def public_record(doc):
    out = {k: v for k, v in doc.items() if k != "_id"}
    for field in ("punch_in", "punch_out"):
        out[field] = to_ms(out.get(field))
    history = []
    for entry in out.get("history", []) or []:
        item = dict(entry)
        item["at"] = to_ms(item.get("at"))
        changes = {}
        for key, pair in (item.get("changes") or {}).items():
            changes[key] = {side: to_ms(val) if key in ("punch_in", "punch_out") else val
                            for side, val in pair.items()}
        item["changes"] = changes
        history.append(item)
    out["history"] = history
    out.setdefault("history", [])
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("work_hours", None)
    return out


def safe_employee(doc):
    if not doc:
        return None
    return {k: v for k, v in doc.items() if k != "_id"}


def validate_shift(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ValueError("time must be HH:MM")
    return value


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_time(cls, v):
        return validate_shift(v)

    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v):
        try:
            if date.fromisoformat(v).isoformat() != v:
                raise ValueError()
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def distinct_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            from_ms(v)
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            from_ms(v)
        return v


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            from_ms(v)
        return v


def init_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="employee_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="attendance_date_status_late")
    logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)], name="attendance_date_late")


@app.on_event("startup")
def create_indexes():
    try:
        init_indexes()
    except Exception as e:
        print(f"Startup warning: could not connect to MongoDB yet: {e}")


@app.get("/health")
def health():
    try:
        db.command("ping")
        try:
            init_indexes()
        except Exception:
            pass
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    out = safe_employee(doc)
    out["created_at"] = to_ms(out.get("created_at"))
    return out


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    items = list(employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = to_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at)
    d = attendance_day(ts, emp).isoformat()
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at)
    candidate = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                              sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    if ts <= candidate["punch_in"] or ts > candidate["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(candidate["punch_in"], ts)
    overtime = compute_overtime(ts, candidate["date"], emp)
    update = {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.5}
    result = logs.update_one({"_id": candidate["_id"], "punch_out": None}, {"$set": update})
    if result.modified_count != 1:
        raise HTTPException(409, "record already punched out")
    candidate.update(update)
    return public_record(candidate)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None: q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    total = logs.count_documents(q)
    docs = list(logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date).isoformat()
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = logs.find_one({"emp_code": emp_code, "date": d})
    if not old:
        raise HTTPException(404, "attendance record not found")
    proposed = {k: old.get(k) for k in ("status", "punch_in", "punch_out")}
    sent = body.model_fields_set
    for key in ("status", "punch_in", "punch_out"):
        if key in sent:
            val = getattr(body, key)
            proposed[key] = from_ms(val) if key in ("punch_in", "punch_out") and val is not None else val
    status = proposed["status"]
    pi, po = proposed["punch_in"], proposed["punch_out"]
    if status in ("ABSENT", "LEAVE"):
        if ("punch_in" in sent and body.punch_in is not None) or ("punch_out" in sent and body.punch_out is not None):
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pi = po = None
    elif pi is None:
        raise HTTPException(422, "presence status requires punch_in")
    if pi is not None:
        if attendance_day(pi, emp).isoformat() != d:
            raise HTTPException(422, "punch_in must belong to attendance date")
        if po is not None and (po <= pi or po > pi + timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    elif po is not None:
        raise HTTPException(422, "punch_out requires punch_in")
    derived = {
        "status": status, "punch_in": pi, "punch_out": po,
        "late_minutes": compute_late_minutes(pi, emp["shift_start"], emp["shift_end"]) if pi and status in PRESENCE else 0,
        "work_hours": compute_work_hours(pi, po) if pi and po and status in PRESENCE else None,
        "overtime_minutes": compute_overtime(po, d, emp) if pi and po and status in PRESENCE else 0,
        "half_day": (compute_work_hours(pi, po) < 4.5) if pi and po and status in PRESENCE else False,
    }
    old_norm = dict(old)
    for k in ("late_minutes", "overtime_minutes", "half_day", "work_hours"):
        old_norm.setdefault(k, 0 if k in ("late_minutes", "overtime_minutes") else (False if k == "half_day" else None))
    if status in ("ABSENT", "LEAVE"):
        derived.update({"punch_in": None, "punch_out": None, "late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False})
    changes = {}
    for key, val in derived.items():
        before = old_norm.get(key)
        if before != val:
            changes[key] = {"from": before, "to": val}
    if not changes:
        raise HTTPException(422, "request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Compare-and-swap avoids lost updates to history. A concurrent loser receives 409.
    query = {"_id": old["_id"]}
    for key in ("status", "punch_in", "punch_out"):
        query[key] = old.get(key, None)
    result = logs.update_one(query, {"$set": derived, "$push": {"history": entry}})
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry")
    old.update(derived)
    if not isinstance(old.get("history"), list):
        old["history"] = []
    old["history"].append(entry)
    return public_record(old)


def month_match(start, end, emp_code=None):
    q = {"date": {"$gte": start, "$lte": end}}
    if emp_code:
        q["emp_code"] = emp_code
    return q


def working_days(start: date, end: date):
    return sum(1 for n in range((end-start).days+1) if (start + timedelta(days=n)).weekday() < 5)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_s, end_s, start, end = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    start = max(start, parse_date(emp["joined_on"]))
    wd = working_days(start, end) if start <= end else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    result = list(logs.aggregate(pipeline))
    r = result[0] if result else {}
    present = rounded(r.get("present_days", 0))
    pct = rounded(present / wd * 100, 4) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": r.get("leave_days", 0), "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "total_overtime_minutes": r.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_s, end_s, start, end = month_bounds(month)
    emp_match = {"joined_on": {"$lte": end_s}}
    if department is not None: emp_match["department"] = department
    pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "let": {"codes": "$codes"}, "pipeline": [
            {"$match": {"date": {"$gte": start_s, "$lte": end_s}}},
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                    {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},
                    {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                    {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                    {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$ifNull": ["$stats.present_days", 0]},
            "avg_work_hours": {"$cond": [{"$gt": [{"$ifNull": ["$stats.work_hours_count", 0]}, 0]},
                {"$divide": ["$stats.work_hours_sum", "$stats.work_hours_count"]}, None]},
            "late_count": {"$ifNull": ["$stats.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$stats.total_late_minutes", 0]},
            "leave_count": {"$ifNull": ["$stats.leave_count", 0]},
            "on_duty_count": {"$ifNull": ["$stats.on_duty_count", 0]}}},
        {"$sort": {"department": 1}}
    ]
    rows = list(employees.aggregate(pipeline, allowDiskUse=True))
    for r in rows:
        r["present_days"] = rounded(r["present_days"])
        r["avg_work_hours"] = rounded(r["avg_work_hours"]) if r["avg_work_hours"] is not None else None
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start_s, end_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$emp.name", "department": "$emp.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                      "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date)
    end = parse_date(to_date)
    if end < start:
        raise HTTPException(422, "to must be >= from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    if not employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    # A database-side date spine and window pipeline; dates are converted from the model's ISO strings.
    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
        }},
        {"$set": {"date_dt": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day",
            "bounds": [datetime.combine(start, time.min, UTC), datetime.combine(end + timedelta(days=1), time.min, UTC)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d", "timezone": "UTC"}},
                  "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department}},
            {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                  "is_working_day": {"$and": [
                      {"$gte": [{"$dayOfWeek": "$date_dt"}, 2]},
                      {"$lte": [{"$dayOfWeek": "$date_dt"}, 6]}
                  ]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date_dt": 1}, "output": {
            "moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$ne": ["$attendance_rate", None]},
                {"$round": ["$attendance_rate", 4]}, None]},
            "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_raw", None]},
                {"$round": ["$moving_avg_raw", 4]}, None]}}},
        {"$sort": {"date": 1}}
    ]
    rows = list(logs.aggregate(pipeline, allowDiskUse=True))
    # When there are no logs at all, seed the date spine from employees through $documents is not available from a collection.
    # Ensure exact range via a tiny database-generated calendar fallback for the empty aggregate case.
    if not rows:
        dates = [{"date": (start + timedelta(days=i)).isoformat()} for i in range((end-start).days+1)]
        for row in dates:
            row.update({"is_working_day": (parse_date(row["date"]).weekday() < 5),
                        "headcount": employees.count_documents({"department": department, "joined_on": {"$lte": row["date"]}}),
                        "present_count": 0, "late_count": 0, "attendance_rate": None})
            if row["is_working_day"] and row["headcount"] > 0:
                row["attendance_rate"] = 0.0
        for i, row in enumerate(dates):
            vals = [r["attendance_rate"] for r in dates[max(0, i-6):i+1] if r["attendance_rate"] is not None]
            row["moving_avg_7d"] = rounded(sum(vals)/len(vals), 4) if vals else None
        rows = dates
    else:
        for row in rows:
            if row["attendance_rate"] is not None: row["attendance_rate"] = rounded(row["attendance_rate"], 4)
            if row["moving_avg_7d"] is not None: row["moving_avg_7d"] = rounded(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


def find_stage(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("stage", "plan") and isinstance(v, str) and v in ("COLLSCAN", "IXSCAN"):
                yield v
            yield from find_stage(v)
    elif isinstance(value, list):
        for v in value:
            yield from find_stage(v)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from)
        if date_to: parse_date(date_to)
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        ex = logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}
    if endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        start_s, end_s, _, _ = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}}]
        ex = logs.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start_s, end_s, start, end = month_bounds(month)
        q = {"joined_on": {"$lte": end_s}}
        if department is not None: q["department"] = department
        ex = employees.find(q).explain()
        return {"endpoint": endpoint, "collection": "employees", "explain": ex}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start_s, end_s, _, _ = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        ex = logs.database.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}
    if not department or not from_date or not to_date:
        raise HTTPException(422, "department, from and to are required")
    parse_date(from_date); parse_date(to_date)
    if to_date < from_date or (parse_date(to_date)-parse_date(from_date)).days >= 92:
        raise HTTPException(422, "invalid trend range")
    q = {"department": department}
    ex = employees.find(q).explain()
    return {"endpoint": endpoint, "collection": "employees", "explain": ex}
