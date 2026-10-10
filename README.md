# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```

You need MongoDB 6.0 or newer. I developed and tested against 7.0. Indexes are created at startup and the step is idempotent; no manual steps are needed.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                              # uses a throwaway database "attendance_pytest"
MONGO_DB=attendance_load python tests/generate_dataset.py   # ~100k records for load/explain checks
```

The tests cover:
- the R1 - R5 edge cases: grace boundary, IST date, overnight shift, half-up rounding, the 30-minute overtime threshold;
- validation and the 404/409/422 paths;
- concurrent punch-in, punch-out, employee create and PATCH;
- ties at the leaderboard cutoff;
- employees with no logs and employees who join later;
- weekend records and the trend's moving average;
- `IXSCAN` and no `COLLSCAN` for every explain target.

I also cross-checked the four analytics endpoints against an independent plain-Python calculation on the 100k dataset: 0 mismatches. On that dataset every endpoint responds in under 200 ms locally.

## Notes and assumptions

- **Working days** (R7) are pure calendar arithmetic, so I compute them in Python (`count_weekdays`) and pass the number into the monthly pipeline. Everything that reads attendance data runs in MongoDB aggregation.
- **Rounding.** MongoDB's `$round` rounds half to even, so the pipelines round half-up in `Decimal128` (`round_expr`).
- **Trend range.** "Exceeds 92 days" means more than 92 calendar days counted inclusively. A full quarter (for example 1 Jul - 30 Sep) is allowed.
- **Punch-out** closes the employee's latest-dated record whose `punch_in` is at or before `punched_at`.
- **PATCH concurrency** is optimistic. The update only applies if the history length and punch data are still as read; otherwise it returns 409, so no history entry can be lost.

Nothing in the contract is left unimplemented.
