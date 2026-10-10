# Employee Attendance & Analytics API

FastAPI + PyMongo implementation for the SDT candidate kit.

## Run locally

1. Install Python 3.11+ and MongoDB 6.0+ (MongoDB 7 recommended).
2. From this directory:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

3. Create `.env` locally (do not commit it):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

4. Load sample records and run:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Open http://127.0.0.1:8000/docs. `GET /health` pings MongoDB and returns 503 if it is unavailable.

## Notes

- All API instants are epoch milliseconds; MongoDB stores BSON dates.
- Attendance calendar dates and shift times use IST.
- Indexes are created idempotently on application startup.
- `REVIEW.md` lists starter defects; `DECISIONS.md` explains implementation choices.
- This is a hiring exercise implementation and should be validated against the full contract and a real MongoDB instance before submission. In particular, run concurrency tests and the trend/explain endpoints against MongoDB 7.
