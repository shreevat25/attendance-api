"""End-to-end tests against a real MongoDB (uses MONGO_URI and a throwaway database).

    pip install -r requirements-dev.txt
    pytest -q
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_pytest")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h=0, mi=0, s=0, ms=0) -> int:
    """Epoch milliseconds of an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + ms


@pytest.fixture()
def c():
    m.client.drop_database(m.db.name)
    with TestClient(m.app) as client:
        yield client
    m.client.drop_database(m.db.name)


def make_emp(c, code, dept="Eng", start="09:30", end="18:30", joined="2026-01-01", name=None):
    r = c.post("/employees", json={"emp_code": code, "name": name or code, "email": f"{code.lower()}@x.com",
                                   "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def seed_log(code, day, status="PRESENT", late=0, ot=0, wh=None, half=None, **extra):
    doc = {"emp_code": code, "date": day, "status": status, "late_minutes": late, "overtime_minutes": ot,
           "work_hours": wh, "punch_in": None, "punch_out": None, **extra}
    if half is not None:
        doc["half_day"] = half
    m.logs.insert_one(doc)


# --------------------------------------------------------------------------- employees
def test_health(c):
    assert c.get("/health").json() == {"status": "ok"}


def test_create_employee_and_validation(c):
    e = make_emp(c, "EMP0001")
    assert isinstance(e["created_at"], int) and e["created_at"] > 100_000_000_000
    assert "_id" not in e and "id" not in e
    assert c.post("/employees", json={**e, "created_at": None}).status_code == 409
    bad = {"emp_code": "EMP0002", "name": "x", "email": "x@x.com", "department": "Eng", "joined_on": "2026-01-01"}
    assert c.post("/employees", json={**bad, "emp_code": "E1"}).status_code == 422
    assert c.post("/employees", json={**bad, "email": "nope"}).status_code == 422
    assert c.post("/employees", json={**bad, "joined_on": "2026-02-30"}).status_code == 422
    assert c.post("/employees", json={**bad, "shift_start": "09:00", "shift_end": "09:00"}).status_code == 422
    assert c.post("/employees", json={**bad, "shift_start": "24:00"}).status_code == 422


def test_concurrent_employee_create(c):
    body = {"emp_code": "EMP0009", "name": "x", "email": "x@x.com", "department": "Eng", "joined_on": "2026-01-01"}
    with ThreadPoolExecutor(8) as ex:
        codes = sorted(ex.map(lambda _: c.post("/employees", json=body).status_code, range(8)))
    assert codes == [201] + [409] * 7


def test_list_employees_pagination(c):
    for i in (3, 1, 2):
        make_emp(c, f"EMP000{i}", dept="A" if i != 2 else "B")
    r = c.get("/employees", params={"department": "A", "page": 1, "page_size": 1}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0001"]
    r = c.get("/employees", params={"department": "A", "page": 2, "page_size": 1}).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP0003"]
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"page_size": 101}).status_code == 422


# --------------------------------------------------------------------------- punch-in (R1, R2)
@pytest.mark.parametrize("hms, ms, late", [((9, 40, 0), 900, 0), ((9, 40, 1), 0, 10), ((10, 15, 59), 0, 45),
                                           ((9, 0, 0), 0, 0)])
def test_late_minutes(c, hms, ms, late):
    make_emp(c, "EMP0001")
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, *hms, ms=ms)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["late_minutes"] == late
    assert body["punch_in"] % 1000 == 0  # truncated to whole seconds
    assert body["date"] == "2026-07-06" and body["history"] == [] and body["work_hours"] is None


def test_ist_date_not_utc_date(c):
    make_emp(c, "EMP0001")
    # 2026-07-07 01:00 IST is still 2026-07-06 in UTC
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 1, 0)})
    assert r.json()["date"] == "2026-07-07"


def test_overnight_shift_date_late_and_overtime(c):
    make_emp(c, "EMP0005", start="22:00", end="06:00")
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist(2026, 7, 7, 0, 30)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 150
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ist(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 6.17
    # a punch-in at exactly shift_end is not "earlier than" it -> same calendar day
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist(2026, 7, 8, 6, 0)})
    assert r.json()["date"] == "2026-07-08"


def test_punch_in_errors(c):
    make_emp(c, "EMP0001")
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    for bad in (1783312500, 1783312500000.0, "1783312500000", True, 5_000_000_000_000):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    ok = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 0)}
    assert c.post("/attendance/punch-in", json=ok).status_code == 201
    assert c.post("/attendance/punch-in", json={**ok, "punched_at": ist(2026, 7, 6, 11, 0)}).status_code == 409


def test_concurrent_punch_in(c):
    make_emp(c, "EMP0001")
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 0)}
    with ThreadPoolExecutor(10) as ex:
        codes = sorted(ex.map(lambda _: c.post("/attendance/punch-in", json=body).status_code, range(10)))
    assert codes == [201] + [409] * 9


# --------------------------------------------------------------------------- punch-out (R3 - R5)
@pytest.mark.parametrize("seconds, hours, half", [(16218, 4.51, False), (16182, 4.5, False), (16181, 4.49, True)])
def test_work_hours_half_up_and_half_day(c, seconds, hours, half):
    make_emp(c, "EMP0001")
    start = ist(2026, 7, 6, 9, 0)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": start})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": start + seconds * 1000})
    assert r.json()["work_hours"] == hours and r.json()["half_day"] is half


@pytest.mark.parametrize("out, ot", [((18, 59, 59), 0), ((19, 0, 0), 30), ((19, 10, 30), 40)])
def test_overtime_threshold(c, out, ot):
    make_emp(c, "EMP0001")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 9, 0)})
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, *out)})
    assert r.json()["overtime_minutes"] == ot


def test_punch_out_errors_and_race(c):
    make_emp(c, "EMP0001")
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    start = ist(2026, 7, 6, 9, 0)
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": start})
    too_late = start + 24 * 3600 * 1000 + 1000
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": too_late}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": start}).status_code == 422
    body = {"emp_code": "EMP0001", "punched_at": ist(2026, 7, 6, 18, 0)}
    with ThreadPoolExecutor(8) as ex:
        codes = sorted(ex.map(lambda _: c.post("/attendance/punch-out", json=body).status_code, range(8)))
    assert codes == [200] + [409] * 7


# --------------------------------------------------------------------------- list
def test_list_attendance(c):
    make_emp(c, "EMP0001")
    make_emp(c, "EMP0002")
    for d in ("2026-07-06", "2026-07-07"):
        seed_log("EMP0002", d)
        seed_log("EMP0001", d, status="WFH")
    r = c.get("/attendance", params={"page_size": 3}).json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    assert r["total"] == 4
    assert c.get("/attendance", params={"status": "WFH"}).json()["total"] == 2
    assert c.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()["total"] == 2
    assert c.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert c.get("/attendance", params={"date_from": "07/07/2026"}).status_code == 422
    assert c.get("/attendance", params={"status": "LATE"}).status_code == 422
    assert c.get("/attendance", params={"page_size": 0}).status_code == 422


# --------------------------------------------------------------------------- regularize
def test_regularize_flow(c):
    make_emp(c, "EMP0001")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 10, 5)})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 18, 30)})
    url = "/attendance/EMP0001/2026-07-07"
    who = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = c.patch(url, json={**who, "punch_in": ist(2026, 7, 7, 9, 28), "late_minutes": 99})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["late_minutes"] == 0 and rec["work_hours"] == 9.03
    (h,) = rec["history"]
    assert h["by"] == "hr.admin" and isinstance(h["at"], int)
    assert h["changes"] == {"punch_in": {"from": ist(2026, 7, 7, 10, 5), "to": ist(2026, 7, 7, 9, 28)},
                            "late_minutes": {"from": 35, "to": 0}, "work_hours": {"from": 8.42, "to": 9.03}}
    # same values again -> nothing changes -> 422
    assert c.patch(url, json={**who, "punch_in": ist(2026, 7, 7, 9, 28)}).status_code == 422
    # ABSENT with a punch time -> 422; ABSENT alone clears everything
    assert c.patch(url, json={**who, "status": "ABSENT", "punch_out": ist(2026, 7, 7, 18, 0)}).status_code == 422
    r = c.patch(url, json={**who, "status": "ABSENT"}).json()
    assert r["punch_in"] is None and r["work_hours"] is None and len(r["history"]) == 2
    assert r["history"][1]["changes"]["punch_out"] == {"from": ist(2026, 7, 7, 18, 30), "to": None}
    # back to PRESENT needs a punch_in
    assert c.patch(url, json={**who, "status": "PRESENT"}).status_code == 422
    # punch_in must stay on the attendance date
    assert c.patch(url, json={**who, "status": "PRESENT", "punch_in": ist(2026, 7, 8, 9, 0)}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-01", json={**who, "status": "LEAVE"}).status_code == 404
    assert c.patch("/attendance/EMP9999/2026-07-07", json={**who, "status": "LEAVE"}).status_code == 404
    assert c.patch(url, json={"status": "LEAVE", "reason": "x", "regularized_by": "a"}).status_code == 422
    assert c.patch(url, json={**who, "punch_in": None}).status_code == 422


def test_concurrent_regularize_never_loses_history(c):
    make_emp(c, "EMP0001")
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist(2026, 7, 7, 9, 0)})
    url = "/attendance/EMP0001/2026-07-07"
    bodies = [{"reason": f"fix {i:03d}", "regularized_by": "hr", "punch_in": ist(2026, 7, 7, 9, 0, i + 1)}
              for i in range(10)]
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda b: c.patch(url, json=b).status_code, bodies))
    assert set(codes) <= {200, 409}
    history = c.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]["history"]
    assert len(history) == codes.count(200)


# --------------------------------------------------------------------------- analytics
def test_employee_monthly(c):
    make_emp(c, "EMP0001", joined="2026-07-13")  # mid-month joiner: 15 weekdays from Mon 13th
    seed_log("EMP0001", "2026-07-13", wh=9.0)
    seed_log("EMP0001", "2026-07-14", status="WFH", half=True, wh=3.0)
    seed_log("EMP0001", "2026-07-15", status="LEAVE")
    seed_log("EMP0001", "2026-07-18", late=20, ot=45)  # Saturday: no presence, but late/OT count
    seed_log("EMP0001", "2026-07-20", late=11)  # legacy: no half_day/history
    r = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 15, "present_days": 2.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 31, "total_overtime_minutes": 45,
                 "attendance_pct": 16.67}
    r = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-06"}).json()
    assert r["working_days"] == 0 and r["attendance_pct"] is None
    r = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-08"}).json()
    assert r["working_days"] == 21 and r["attendance_pct"] == 0.0 and r["present_days"] == 0
    assert c.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(c):
    make_emp(c, "EMP0001", dept="Eng")
    make_emp(c, "EMP0002", dept="Eng")              # no logs at all, still counted
    make_emp(c, "EMP0003", dept="Eng", joined="2026-08-01")  # joins later: not counted
    make_emp(c, "EMP0004", dept="Ops", joined="2026-08-01")  # whole dept has headcount 0 -> omitted
    make_emp(c, "EMP0005", dept="Sales")
    seed_log("EMP0001", "2026-07-06", wh=9.0, late=15)
    seed_log("EMP0001", "2026-07-07", wh=8.0)
    seed_log("EMP0001", "2026-07-08", status="ON_DUTY", wh=4.005, half=True)
    seed_log("EMP0001", "2026-07-09", status="LEAVE")
    seed_log("EMP0005", "2026-07-11", wh=5.0, late=40)  # Saturday
    seed_log("EMP9999", "2026-07-06", wh=1.0)  # orphan log, no employee
    r = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert r["items"] == [
        {"department": "Eng", "headcount": 2, "present_days": 2.5, "avg_work_hours": 7.0, "late_count": 1,
         "total_late_minutes": 15, "leave_count": 1, "on_duty_count": 1},
        {"department": "Sales", "headcount": 1, "present_days": 0.0, "avg_work_hours": 5.0, "late_count": 1,
         "total_late_minutes": 40, "leave_count": 0, "on_duty_count": 0},
    ]
    r = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in r["items"]] == ["Sales"]


def test_department_summary_avg_is_over_records(c):
    make_emp(c, "EMP0001")
    make_emp(c, "EMP0002")
    seed_log("EMP0001", "2026-07-06", wh=8.0)
    seed_log("EMP0001", "2026-07-07", wh=8.0)
    seed_log("EMP0001", "2026-07-08", wh=8.0)
    seed_log("EMP0002", "2026-07-06", wh=4.0)
    seed_log("EMP0002", "2026-07-07", status="ABSENT", wh=None)
    item = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert item["avg_work_hours"] == 7.0  # (8+8+8+4)/4, not (8+4)/2


def test_avg_work_hours_rounds_half_up(c):
    make_emp(c, "EMP0001")
    seed_log("EMP0001", "2026-07-06", wh=1.01)
    seed_log("EMP0001", "2026-07-07", wh=1.0)  # mean 1.005 -> 1.01 (float/banker's would give 1.0)
    item = c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert item["avg_work_hours"] == 1.01


def test_leaderboard_ties(c):
    for code, dept, total in [("EMP0001", "A", 50), ("EMP0002", "A", 40), ("EMP0003", "B", 40),
                              ("EMP0004", "A", 30), ("EMP0005", "A", 30), ("EMP0006", "A", 0)]:
        make_emp(c, code, dept=dept)
        seed_log(code, "2026-07-06", late=total)
    seed_log("EMP0001", "2026-07-11", late=0)  # Saturday, not late
    seed_log("EMP9999", "2026-07-06", late=999)  # orphan: ignored
    seed_log("EMP0001", "2026-08-03", late=500)  # other month
    r = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in r] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003")]
    r = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in r] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003"),
                                                         (4, "EMP0004"), (4, "EMP0005")]
    assert r[0] == {"rank": 1, "emp_code": "EMP0001", "name": "EMP0001", "department": "A",
                    "total_late_minutes": 50, "late_count": 1}
    r = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2, "department": "A"}).json()
    assert [(i["rank"], i["emp_code"]) for i in r["items"]] == [(1, "EMP0001"), (2, "EMP0002")]
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_department_trend(c):
    make_emp(c, "EMP0001", dept="Eng", joined="2026-01-01")
    make_emp(c, "EMP0002", dept="Eng", joined="2026-07-08")  # joins mid-range (Wed)
    make_emp(c, "EMP0003", dept="Eng", joined="2026-12-01")  # joins after the range
    seed_log("EMP0001", "2026-07-03", status="PRESENT")  # before the range: must not leak into the window
    seed_log("EMP0001", "2026-07-06", late=12)          # Mon
    seed_log("EMP0001", "2026-07-07", status="WFH", half=True)
    seed_log("EMP0002", "2026-07-08")
    seed_log("EMP0001", "2026-07-08", status="ABSENT")
    seed_log("EMP0001", "2026-07-11")                    # Sat: counted, but rate is null
    r = c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-06", "to": "2026-07-14"})
    items = r.json()["items"]
    assert [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(6, 15)]
    rows = {i["date"][-2:]: i for i in items}
    assert rows["06"] == {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 1,
                          "late_count": 1, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert rows["07"]["attendance_rate"] == 0.5 and rows["07"]["moving_avg_7d"] == 0.75
    assert rows["08"]["headcount"] == 2 and rows["08"]["attendance_rate"] == 0.5
    assert rows["08"]["moving_avg_7d"] == 0.6667
    assert rows["11"]["present_count"] == 1 and rows["11"]["attendance_rate"] is None
    assert rows["09"]["attendance_rate"] == 0.0  # working day, no records
    # 14th window = 8th..14th: rates 0.5, 0, 0, 0, 0 (+ two weekend nulls) -> 0.1
    assert rows["14"]["moving_avg_7d"] == 0.1
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-06", "to": "2026-07-14"}).status_code == 404
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-14", "to": "2026-07-06"}).status_code == 422
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422
    # department whose only employee joins after the range: every row present, headcount 0, rates null
    make_emp(c, "EMP0009", dept="Late", joined="2027-01-01")
    items = c.get("/analytics/departments/Late/trend", params={"from": "2026-07-06", "to": "2026-07-08"}).json()["items"]
    assert [(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in items] == [(0, None, None)] * 3


# --------------------------------------------------------------------------- explain
@pytest.mark.parametrize("endpoint, params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(c, endpoint, params):
    make_emp(c, "EMP0001")
    seed_log("EMP0001", "2026-07-06")
    r = c.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    text = r.text
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_explain_requires_params(c):
    assert c.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert c.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
    assert c.get("/admin/explain/nope").status_code == 422
