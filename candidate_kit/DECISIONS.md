# DECISIONS.md

I create a unique `employees.emp_code` index to enforce the business identifier even during concurrent creates, and a unique `(emp_code, date)` attendance index so duplicate punch-ins cannot create two records. The date/employee sort index supports attendance listing; employee/date/status and date/status/late indexes support monthly reports and late ranking. I avoid indexing every field because extra indexes slow writes and increase storage.

For two simultaneous punch-ins, both requests may reach the insert, but MongoDB's unique compound index allows only one document for that employee and attendance date. The winner returns 201; the duplicate-key loser returns 409. Punch-out similarly uses a conditional update requiring `punch_out` to still be null, so only one request can close a record.

The leaderboard uses MongoDB `$rank`, not row positions. If two employees tie at the cutoff rank, both are returned because the limit applies to rank, not result count; ties share a rank and the next rank is skipped.

The department summary starts from eligible employee records and looks up attendance logs, so employees with zero logs still contribute to headcount. For 100x the data, I would add workload-based index review and benchmark aggregation stages, consider pre-aggregated daily summaries, and partition/report by bounded date ranges.
