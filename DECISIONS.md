# DECISIONS.md

**1. Indexes**

employees: unique emp_code (no duplicates), (department, emp_code) for the department filter, and joined_on for the summary headcount.

attendance_logs: unique (emp_code, date). It allows one record per day and is used by the monthly summary, punch-out and every $lookup. (date desc, emp_code) handles the default list order and month ranges. (status, date desc, emp_code) handles status filters.

I thought about (emp_code, punch_in) for punch-out, but the unique index already finds the latest record, so I dropped it.

**2. Punch-in race**

Both requests work out the same emp_code + date and call insert_one straight away, with no find first. The unique index lets only one through. The other gets DuplicateKeyError and I return 409.

**3. Ties**

I use $rank, so ties share a rank and the next one is skipped (1, 2, 2, 4). The limit is applied after ranking (rank <= limit), so everyone tied at the cutoff comes back, even if that's more than limit rows. Ties are sorted by emp_code.

**4. Headcount**

The summary starts from employees (joined_on <= month end) and pulls logs in with $lookup. $unwind keeps empty arrays, so someone with no logs still counts.

**5. At 100x**

I'd keep a per-employee monthly summary collection updated with $merge on every write, and run analytics on that instead of the raw logs.
