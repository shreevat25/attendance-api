# REVIEW.md

Defects found in the starter `app/main.py`. Line numbers refer to the starter file. Every fix is in the current `app/main.py` and most are covered by `tests/test_api.py`.

## Helpers

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (l.35) | `punch_in.replace(hour=h, minute=m)` puts the shift start in whatever time zone the datetime happens to be in (naive local time or UTC), not IST. | Punch-in at 10:05 IST is 04:35 UTC. Replacing the hour gives 09:30 **UTC**, the result is negative, and `late_minutes` is 0 instead of 35. | Build the shift start explicitly as `datetime.combine(record_date, shift_start, tzinfo=IST)` (`at_ist`). |
| 2 | `compute_late_minutes` (l.37) | The grace check uses the floored minute count (`minutes > 10`), so anything from 10:01 to 10:59 after shift start counts as on time. R2 says late means strictly more than 10:00, so the check must use seconds. | Shift 09:30, punch-in 09:40:01. Expected 10, got 0. | Compare elapsed **seconds** with 600, then floor to minutes. |
| 3 | `compute_late_minutes` (l.35) | It uses the punch-in's own calendar day. For an overnight shift (22:00 - 06:00), a punch-in at 00:30 belongs to the shift that started the day before (R1). | Shift 22:00, punch-in 00:30. Expected 150, got 0, because the punch is compared with 22:00 of the same day. | Late minutes are measured from the shift start on the record's attendance `date`, which R1 already computed. |
| 4 | `compute_work_hours` (l.41) | `round()` on a float rounds the binary value (effectively round-half-even), but R4 requires decimal half-up. | 4 h 30 min 18 s = 4.505 h. `round(4.505, 2)` returns `4.5`, expected `4.51`. | `Decimal(seconds) / 3600`, quantized with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` (l.47) | There is no 30-minute minimum (R3). | Punch-out 18:45 on an 18:30 shift returns 15 instead of 0. | Return 0 unless the minutes are `>= 30`. |
| 6 | `compute_overtime` (l.46) | The shift end is always on the record's `date`. For an overnight shift it is on the next day. | Shift 22:00 - 06:00, out at 06:40 the next morning. Expected 40, got about 1480 (24 h 40 m). | `shift_end_instant` adds a day when `shift_end <= shift_start`. |
| 7 | All helpers | No truncation to whole seconds (R1). `punched_at / 1000` keeps the milliseconds. | 09:40:00.900 should count as 09:40:00. Milliseconds are also stored and returned. | `ms_to_dt` uses integer `ms // 1000`. `now_utc()` drops microseconds. |

## Setup and models

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 8 | `MongoClient(...)` (l.22) | Not `tz_aware`, so PyMongo returns naive datetimes. Any arithmetic or comparison with an aware value raises `TypeError` or silently assumes local time. | Subtracting an aware IST datetime from a stored `punch_in` raises `can't subtract offset-naive and offset-aware datetimes`, which surfaces as a 500. | `MongoClient(..., tz_aware=True, tzinfo=UTC)`. Every instant inside the app is aware UTC. |
| 9 | Startup | No indexes are created, so nothing is unique and every query is a collection scan. | `explain` shows `COLLSCAN`. Duplicates are possible (see #12 and #16). | `ensure_indexes()` runs in the FastAPI lifespan and is idempotent. |
| 10 | `EmployeeIn` (l.53) | No validation: `emp_code` format, email, `HH:MM` shift times, `joined_on` as a real date, and `shift_start != shift_end`. | `{"shift_start": "25:99"}` or `joined_on: "2026-02-30"` is accepted. A later punch-in then crashes in `split(":")` or `fromisoformat`. | Field constraints taken from the OpenAPI schema, plus validators for the date and for `shift_start != shift_end`. |
| 11 | `PunchInIn` (l.63) | `punched_at: Optional[int]` is lax. Pydantic accepts `"1783312500000"` (string), `1783312500000.0` (float) and seconds-looking values. `status: str` accepts anything, including `ABSENT` or `"foo"`. | POST with `punched_at: 1783312500` (seconds) creates a record dated 1970-01-21. | `EpochMillis = int` with `strict=True, ge=1e11, le=4102444800000`. `status` is `Literal["PRESENT","WFH","ON_DUTY"]`. |

## Endpoints

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 12 | `create_employee` (l.79) | Check-then-insert: two concurrent creates both pass `find_one` and both insert. | 8 parallel POSTs with the same `emp_code` return several 201s. | A unique index on `emp_code`. Insert directly and map `DuplicateKeyError` to 409. |
| 13 | `create_employee` (l.82) | `datetime.now()` is naive server-local time. PyMongo stores it as if it were UTC, so on an IST machine `created_at` is 5 h 30 m off. It is also returned as a datetime, not epoch ms. | `created_at` comes back as an ISO string, and its value is wrong on any server not running in UTC. | `datetime.now(timezone.utc)`. Responses go through `employee_out`, which returns epoch ms. |
| 14 | `list_employees` (l.93) | `skip = page * page_size` skips the whole first page. | `page=1` returns employees 21-40. | `(page - 1) * page_size`. |
| 15 | `list_employees` (l.94) | `total` counts the whole collection and ignores the `department` filter (R10). There is no sort, so pages are unstable. `page` and `page_size` are unbounded: `page=0` gives a negative skip, which raises in PyMongo (500). | `?department=Sales` returns `total` equal to all employees. `?page=0` returns 500. | `count_documents(q)`, `.sort("emp_code")`, and `Query(ge=1)` / `Query(ge=1, le=100)`. |
| 16 | `punch_in` (l.104) | Check-then-insert race. Two identical punch-ins both get 201 and create two records for the same day. | Fire 10 parallel punch-ins: several 201s. | A unique `(emp_code, date)` index. Insert directly and map `DuplicateKeyError` to 409. |
| 17 | `punch_in` (l.101) | Unknown employee: `emp` is `None`, and `emp["shift_start"]` raises `TypeError` (500). | `POST {"emp_code": "EMP9999"}` returns 500 instead of 404. | `get_employee_or_404`. |
| 18 | `punch_in` (l.102-103) | `datetime.fromtimestamp(ms / 1000)` converts to the **server's local time zone** (naive), and `date` is that local date. That is not the IST date, and the overnight rule (R1) is ignored. `if body.punched_at` also treats 0 as "missing". | On a UTC server, a punch-in at 01:00 IST on the 7th gets `date` 2026-07-06. A 00:30 punch on a 22:00 shift gets the wrong day. | `attendance_date()` implements R1: IST date, minus one day for an overnight punch before `shift_end`. Uses `is not None`. |
| 19 | `punch_in`, `list_attendance` (l.119, l.149) | Responses leak MongoDB's `_id` as `id`, and return datetimes instead of epoch ms. Legacy records missing `half_day` or `history` are returned without them. | The response contains an `id` field. `punch_in` is a string. Sample record 9 has no `half_day` key. | `record_out()` builds the documented shape: ms values, defaults for missing fields, no `_id`. |
| 20 | `list_attendance` (l.144) | Loads the **whole collection** into Python, then sorts and slices in memory. That is too slow and memory-hungry at 100k documents, and there is no `emp_code` tie-break in the sort. | Latency grows with the collection. Records on the same date come back in arbitrary order. | Database-side `find().sort(date -1, emp_code 1).skip().limit()` backed by indexes, with `count_documents(filter)` for `total`. |
| 21 | `list_attendance` (l.125) | No validation: `page` and `page_size` are unbounded, `status` accepts any string, dates are not checked, and `date_from > date_to` is not a 422. | `?page=0` gives an empty page (slice `[-20:0]`). `?date_from=2026-07-10&date_to=2026-07-01` returns 200. | `Query` bounds, a `Status` literal, and `validate_list_dates()`. |
| 22 | `health` (l.73) | Always returns 200, even when MongoDB is down. | Stop mongod: `/health` still says ok. | Ping MongoDB and return 503 on `PyMongoError`. |

## Looked at and decided were fine

- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env`. I made it explicit.
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct. IST has no daylight saving.
- `compute_overtime` builds the shift end in IST correctly for day shifts. Only the overnight case and the 30-minute rule were wrong.
- `doc.pop("_id", None)` after `insert_one` in `create_employee` is correct, because `insert_one` adds `_id` to the dict it is given.
- Comparing `date` strings with `$gte` / `$lte` is safe, because `YYYY-MM-DD` sorts lexically in date order.
- Sync (`def`) endpoints with PyMongo are fine: FastAPI runs them in a thread pool, and the race safety comes from the database, not from Python.
