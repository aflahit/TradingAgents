# app.py
# FastAPI + Redis queue (global=1) + single-flight per ticker + SQLite history
# ---------------------------------------------------------------
# ENV (with sensible defaults):
#   REDIS_URL=redis://localhost:6379/0
#   DB_PATH=./data/stock_history.sqlite
#   RETENTION_DAYS=30
#
# Run:
#   pip install fastapi uvicorn redis "pydantic>=1.10,<3" aiosqlite
#   docker run -d --name redis -p 6379:6379 redis:7
#   mkdir -p ./data
#   uvicorn app:app --reload
#
# Try:
#   curl -s "http://127.0.0.1:8000/analyze?ticker=AAPL"
#   curl -s "http://127.0.0.1:8000/analyze?ticker=AAPL"    # joins same job
#   curl -s "http://127.0.0.1:8000/analyze?ticker=MSFT"    # queued behind AAPL
#   curl -s "http://127.0.0.1:8000/status-by-ticker/AAPL"
#   curl -s "http://127.0.0.1:8000/status/<JOB_ID>"
#   curl -s "http://127.0.0.1:8000/analyze?ticker=AAPL&wait=true"
#   # History (survives Redis TTL):
#   curl -s "http://127.0.0.1:8000/history/latest?ticker=AAPL"
#   curl -s "http://127.0.0.1:8000/history/by-date?date=2025-10-16&ticker=AAPL"
#   curl -s "http://127.0.0.1:8000/history/<JOB_ID>"

import os, json, time, uuid, asyncio
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, date
import threading

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from redis import asyncio as aioredis
import aiosqlite

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
import pandas_market_calendars as mcal

from zoneinfo import ZoneInfo

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")  # NEW: your “day” boundary

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# ---------------- Config ----------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DB_PATH = os.getenv("DB_PATH", "./data/stock_history.sqlite")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))

# Redis keys
KEY_QUEUE_L = "stock.queue"                           # job_ids FIFO
KEY_QUEUE_ORDER_L = "stock.queue_order"               # mirror for human-readable positions
KEY_TICKER_ACTIVE = "stock.ticker:{ticker}:job"       # str ticker -> active job_id
KEY_CONCURRENCY_LOCK = "stock.worker:lock"            # global lock to enforce concurrency=1
KEY_JOB_HASH = "stock.job:{job_id}"                   # per-job hash
KEY_JOB_DONE_CH = "stock.job:{job_id}:done"           # pub/sub channel for completion

CONCURRENCY_TTL_MS = 30_000  # renew every ~10s while running

# ---------------- App ----------------
app = FastAPI(title="Stock Analyzer (Redis queued, single-flight, SQLite history)")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Models ----------------
@dataclass
class Job:
    id: str
    ticker: str
    status: str = "queued"  # queued | running | succeeded | failed
    enqueued_at: float = field(default_factory=lambda: time.time())
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress: float = 0.0  # 0..1
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

class EnqueueResponse(BaseModel):
    job_id: str
    ticker: str
    status: str
    queue_position: int
    queue_size: int
    message: str
    result: Optional[dict] = None   # NEW: return cached result when applicable

class StatusResponse(BaseModel):
    id: str
    ticker: str
    status: str
    progress: float
    queue_position: int
    queue_size: int
    enqueued_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    result: Optional[dict]
    error: Optional[str]

# ---------------- Globals ----------------
redis_api: aioredis.Redis  # API loop client (used by endpoints & retention loop)

# ---------------- DB (SQLite) ----------------
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            enqueued_at REAL,
            started_at REAL,
            finished_at REAL,
            progress REAL,
            result_json TEXT,
            error TEXT
        );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ticker_finished ON jobs(ticker, finished_at);")
        await db.commit()

async def save_job_to_db(job: Job):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO jobs (id, ticker, status, enqueued_at, started_at, finished_at, progress, result_json, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              ticker=excluded.ticker,
              status=excluded.status,
              enqueued_at=excluded.enqueued_at,
              started_at=excluded.started_at,
              finished_at=excluded.finished_at,
              progress=excluded.progress,
              result_json=excluded.result_json,
              error=excluded.error
        """, (
            job.id,
            job.ticker,
            job.status,
            job.enqueued_at,
            job.started_at,
            job.finished_at,
            job.progress,
            json.dumps(job.result) if job.result is not None else None,
            job.error
        ))
        await db.commit()

async def load_job_from_db(job_id: str) -> Optional[Job]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return Job(
            id=row["id"],
            ticker=row["ticker"],
            status=row["status"],
            enqueued_at=row["enqueued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            progress=row["progress"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"]
        )

async def latest_job_for_ticker_db(ticker: str) -> Optional[Job]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM jobs
            WHERE ticker = ? AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 1
        """, (ticker,))
        row = await cur.fetchone()
        if not row:
            return None
        return Job(
            id=row["id"],
            ticker=row["ticker"],
            status=row["status"],
            enqueued_at=row["enqueued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            progress=row["progress"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"]
        )

async def latest_job_for_ticker_since_db(ticker: str, since_ts_utc: float) -> Optional[Job]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT * FROM jobs
            WHERE ticker = ?
              AND finished_at IS NOT NULL
              AND finished_at >= ?
            ORDER BY finished_at DESC
            LIMIT 1
        """, (ticker, since_ts_utc))
        row = await cur.fetchone()
        if not row:
            return None
        return Job(
            id=row["id"],
            ticker=row["ticker"],
            status=row["status"],
            enqueued_at=row["enqueued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            progress=row["progress"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"]
        )

async def jobs_by_date_db(day: date, ticker: Optional[str] = None) -> List[Job]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    q = "SELECT * FROM jobs WHERE finished_at BETWEEN ? AND ?"
    params: List[Any] = [start, end]
    if ticker:
        q += " AND ticker = ?"
        params.append(ticker)
    q += " ORDER BY finished_at DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, tuple(params))
        rows = await cur.fetchall()
        out: List[Job] = []
        for row in rows:
            out.append(Job(
                id=row["id"],
                ticker=row["ticker"],
                status=row["status"],
                enqueued_at=row["enqueued_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                progress=row["progress"],
                result=json.loads(row["result_json"]) if row["result_json"] else None,
                error=row["error"]
            ))
        return out

# ---------------- Redis helpers ----------------
def _ticker_key(ticker: str) -> str:
    return KEY_TICKER_ACTIVE.format(ticker=ticker)

def _job_hash_key(job_id: str) -> str:
    return KEY_JOB_HASH.format(job_id=job_id)

def _job_done_channel(job_id: str) -> str:
    return KEY_JOB_DONE_CH.format(job_id=job_id)

async def write_job(job: Job, r: aioredis.Redis | None = None):
    r = r or redis_api
    key = _job_hash_key(job.id)
    payload = asdict(job).copy()
    if payload.get("result") is not None:
        payload["result"] = json.dumps(payload["result"])
    payload = {k: ("" if v is None else str(v)) for k, v in payload.items()}
    await r.hset(key, mapping=payload)

async def read_job(job_id: str, r: aioredis.Redis | None = None) -> Job:
    r = r or redis_api
    data = await r.hgetall(_job_hash_key(job_id))
    if not data:
        raise HTTPException(404, "job not found")
    def ff(x: Optional[str], default=None, cast=float):
        if x is None or x == "":
            return default
        try:
            return cast(x)
        except Exception:
            return default
    result = None
    if "result" in data and data["result"]:
        try:
            result = json.loads(data["result"])
        except Exception:
            result = None
    return Job(
        id=data["id"],
        ticker=data["ticker"],
        status=data["status"],
        enqueued_at=ff(data.get("enqueued_at"), 0.0, float),
        started_at=ff(data.get("started_at"), None, float),
        finished_at=ff(data.get("finished_at"), None, float),
        progress=ff(data.get("progress"), 0.0, float),
        result=result,
        error=(data.get("error") or None),
    )

async def queue_position(job_id: str, r: aioredis.Redis | None = None) -> int:
    r = r or redis_api
    lst = await r.lrange(KEY_QUEUE_ORDER_L, 0, -1)
    try:
        return lst.index(job_id) + 1
    except ValueError:
        return 0

async def current_queue_size(r: aioredis.Redis | None = None) -> int:
    r = r or redis_api
    return await r.llen(KEY_QUEUE_ORDER_L)

# ---------------- Enqueue (single-flight) ----------------
async def enqueue_or_join(ticker: str, r: aioredis.Redis) -> Job:
    r = r or redis_api
    ticker = ticker.upper().strip()
    if not ticker:
        raise HTTPException(400, "ticker is required")

    existing = await r.get(_ticker_key(ticker))
    if existing:
        return await read_job(existing)

    job_id = uuid.uuid4().hex
    job = Job(id=job_id, ticker=ticker, status="queued")
    await write_job(job)

    # Map ticker->job (set TTL > max runtime; worker refreshes)
    await r.set(_ticker_key(ticker), job_id, nx=True, ex=60 * 10)

    # Enqueue FIFO and visible order list
    async with r.pipeline(transaction=True) as p:
        p.rpush(KEY_QUEUE_L, job_id)
        p.rpush(KEY_QUEUE_ORDER_L, job_id)
        await p.execute()
    return job

# ---------------- Worker loop (global concurrency=1) ----------------
async def worker_loop():
    # Worker runs in its own thread/event loop — create a loop-local Redis client
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        while True:
            try:
                item = await r.blpop(KEY_QUEUE_L, timeout=5)
                if not item:
                    await asyncio.sleep(0.1)
                    continue
                _, job_id = item

                job = await read_job(job_id, r=r)
                job.status = "running"
                job.started_at = time.time()
                await write_job(job, r=r)
                await r.lrem(KEY_QUEUE_ORDER_L, 0, job_id)

                # Acquire global lock (use worker's client!)
                lock_id = uuid.uuid4().hex
                got_lock = await r.set(KEY_CONCURRENCY_LOCK, lock_id, nx=True, px=CONCURRENCY_TTL_MS)
                while not got_lock:
                    await asyncio.sleep(0.5)
                    got_lock = await r.set(KEY_CONCURRENCY_LOCK, lock_id, nx=True, px=CONCURRENCY_TTL_MS)

                async def renew_lock():
                    while True:
                        await asyncio.sleep(10)
                        if (await r.get(KEY_CONCURRENCY_LOCK)) != lock_id:
                            break
                        await r.pexpire(KEY_CONCURRENCY_LOCK, CONCURRENCY_TTL_MS)

                renew_task = asyncio.create_task(renew_lock(), name=f"renew-lock-{job_id}")
                try:
                    job.result = await analyze_stock(job.ticker, job, r=r)  # pass r
                    job.status = "succeeded"
                except Exception as e:
                    job.status = "failed"
                    job.error = str(e)
                finally:
                    job.finished_at = time.time()
                    await write_job(job, r=r)
                    await save_job_to_db(job)
                    renew_task.cancel()

                    if (await r.get(KEY_CONCURRENCY_LOCK)) == lock_id:
                        await r.delete(KEY_CONCURRENCY_LOCK)

                    await r.delete(_ticker_key(job.ticker))
                    await r.publish(_job_done_channel(job.id), "done")
            except Exception as e:
                print(e)
                await asyncio.sleep(0.5)
    finally:
        await r.aclose()

def start_worker_thread():
    def _run():
        # the worker gets its own event loop here
        asyncio.run(worker_loop())
    t = threading.Thread(target=_run, name="stock-worker", daemon=True)
    t.start()

# ---------------- The "long" task ----------------
async def analyze_stock(ticker: str, job: Job, r: aioredis.Redis, steps: int = 10) -> Dict[str, Any]:
    """
    Simulate long work (set steps=300 for ~5 minutes).
    Replace this with your real I/O/CPU/ML pipeline.
    """
    current_date = datetime.today().strftime('%Y-%m-%d')
    # check for market open
    # result = {
    #     "decision": "Nothing",
    #     "detail": "Market is closed",
    #     "market_open": False
    # }
    # is_open = market_is_open(datetime.now().strftime("%Y-%m-%d"))
    # if is_open:
    print("processing for")
    print(ticker)
    print(current_date)
    state, decision = ta.propagate(ticker, current_date)
    # deconstruct the messages
    reports = {
        "market_report": state['market_report'],
        "news_report": state['news_report'],
        "sentiment_report": state['sentiment_report'],
        "fundamentals_report": state['fundamentals_report'],
        "investment_plan": state['investment_plan'],
        "trader_investment_plan": state['trader_investment_plan'],
        "final_trade_decision": state['final_trade_decision'],
        "investment_debate_state": state['investment_debate_state']
    }
    result = {
        "decision": decision,
        "detail": reports,
        "market_open": True
    }
    job.progress = 1.0
    await write_job(job, r)
    await r.expire(_ticker_key(ticker), 60 * 10)

    return {
        "ticker": ticker,
        "recommendation": result['decision'],
        "score": 1,
        "notes": result['detail']
    }

# ---------------- Status helpers ----------------
async def serialize_status(job: Job) -> Dict[str, Any]:
    pos = await queue_position(job.id) if job.status == "queued" else 0
    size = await current_queue_size()
    payload = asdict(job)
    payload["queue_position"] = pos
    payload["queue_size"] = size
    return payload

def _parse_ymd(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()

def start_of_today_utc_ts(tz_name: str = TIMEZONE) -> float:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = datetime(now_local.year, now_local.month, now_local.day, tzinfo=tz)
    # convert to UTC timestamp
    return start_local.astimezone(timezone.utc).timestamp()

def market_is_open(date):
    result = mcal.get_calendar("NASDAQ").schedule(start_date=date, end_date=date)
    return result.empty == False

# ---------------- Startup / Shutdown ----------------
@app.on_event("startup")
async def _startup():
    global redis_api
    redis_api = aioredis.from_url(REDIS_URL, decode_responses=True)
    await init_db()
    start_worker_thread()  # was: asyncio.create_task(worker_loop(), ...)
    asyncio.create_task(retention_loop(), name="history-retention-loop")

@app.on_event("shutdown")
async def _shutdown():
    await redis_api.aclose()

# ---------------- Retention (SQLite) ----------------
async def retention_loop():
    while True:
        try:
            cutoff = (datetime.now(timezone.utc).timestamp() - RETENTION_DAYS * 24 * 3600)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM jobs WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,))
                await db.commit()
        except Exception:
            pass
        await asyncio.sleep(6 * 3600)  # every 6 hours

# ---------------- Endpoints ----------------
@app.post("/analyze", response_model=EnqueueResponse)
async def analyze_endpoint(
    ticker: str = Query(..., description="Stock ticker"),
    wait: bool = Query(False, description="Block until finished?"),
    prefer_today_cache: bool = Query(True, description="Return same-day finished result if available"),
    force: bool = Query(False, description="Ignore cache; always enqueue/join")
):
    ticker_norm = ticker.upper().strip()

    # 1) If there's an active job, we keep the original behavior: single-flight join
    active_job_id = await redis_api.get(_ticker_key(ticker_norm))
    if active_job_id:
        # join the active job
        job = await read_job(active_job_id)
        if wait:
            pubsub = redis_api.pubsub()
            await pubsub.subscribe(_job_done_channel(job.id))
            try:
                while True:
                    j2 = await read_job(job.id)
                    if j2.status in ("succeeded", "failed"):
                        job = j2
                        break
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        job = await read_job(job.id)
                        break
            finally:
                await pubsub.close()
        s = await serialize_status(job)
        return EnqueueResponse(
            job_id=job.id,
            ticker=job.ticker,
            status=job.status,
            queue_position=s["queue_position"],
            queue_size=s["queue_size"],
            message="joined existing job",
            result=job.result if job.status in ("succeeded", "failed") else None
        )

    # 2) No active job. If not forcing and prefer_today_cache, try same-day cache from SQLite.
    if prefer_today_cache and not force:
        since_ts = start_of_today_utc_ts(TIMEZONE)
        cached = await latest_job_for_ticker_since_db(ticker_norm, since_ts)
        if cached and cached.status == "succeeded":
            # Return the cached analysis immediately
            return EnqueueResponse(
                job_id=cached.id,
                ticker=cached.ticker,
                status=cached.status,
                queue_position=0,
                queue_size=await current_queue_size(),
                message="returned cached same-day result",
                result=cached.result
            )

    # 3) Enqueue a fresh job (single-flight still enforced inside this call)
    job = await enqueue_or_join(ticker_norm, redis_api)

    if wait:
        # Wait until this new job finishes
        j = await read_job(job.id)
        if j.status in ("queued", "running"):
            pubsub = redis.pubsub()
            await pubsub.subscribe(_job_done_channel(job.id))
            try:
                while True:
                    j = await read_job(job.id)
                    if j.status in ("succeeded", "failed"):
                        break
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        break
            finally:
                await pubsub.close()

    j = await read_job(job.id)
    s = await serialize_status(j)
    message = "job already completed" if j.status in ("succeeded", "failed") else "enqueued"
    return EnqueueResponse(
        job_id=j.id,
        ticker=j.ticker,
        status=j.status,
        queue_position=s["queue_position"],
        queue_size=s["queue_size"],
        message=message,
        result=j.result if j.status in ("succeeded", "failed") else None
    )

@app.get("/status/{job_id}", response_model=StatusResponse)
async def status(job_id: str):
    # Try Redis, else DB (for already-evicted jobs)
    try:
        j = await read_job(job_id)
        s = await serialize_status(j)
    except HTTPException:
        j = await load_job_from_db(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        s = {"queue_position": 0, "queue_size": await current_queue_size()}
    return StatusResponse(
        id=j.id, ticker=j.ticker, status=j.status, progress=j.progress,
        queue_position=s["queue_position"], queue_size=s["queue_size"],
        enqueued_at=j.enqueued_at, started_at=j.started_at, finished_at=j.finished_at,
        result=j.result, error=j.error
    )

@app.get("/status-by-ticker/{ticker}", response_model=StatusResponse)
async def status_by_ticker(ticker: str):
    ticker = ticker.upper().strip()
    job_id = await redis_api.get(_ticker_key(ticker))
    if job_id:
        j = await read_job(job_id)
        s = await serialize_status(j)
    else:
        # latest finished in DB (no active job)
        j = await latest_job_for_ticker_db(ticker)
        if not j:
            raise HTTPException(404, "no job found for ticker")
        s = {"queue_position": 0, "queue_size": await current_queue_size()}
    return StatusResponse(
        id=j.id, ticker=j.ticker, status=j.status, progress=j.progress,
        queue_position=s["queue_position"], queue_size=s["queue_size"],
        enqueued_at=j.enqueued_at, started_at=j.started_at, finished_at=j.finished_at,
        result=j.result, error=j.error
    )

# @app.get("/history/{job_id}", response_model=StatusResponse)
# async def history_by_id(job_id: str):
#     # Prefer Redis if still present, else SQLite
#     try:
#         j = await read_job(job_id)
#         s = await serialize_status(j)
#     except HTTPException:
#         j = await load_job_from_db(job_id)
#         if not j:
#             raise HTTPException(404, "job not found in history")
#         s = {"queue_position": 0, "queue_size": await current_queue_size()}
#     return StatusResponse(
#         id=j.id, ticker=j.ticker, status=j.status, progress=j.progress,
#         queue_position=s["queue_position"], queue_size=s["queue_size"],
#         enqueued_at=j.enqueued_at, started_at=j.started_at, finished_at=j.finished_at,
#         result=j.result, error=j.error
#     )

@app.get("/history/latest", response_model=StatusResponse)
async def history_latest(ticker: str = Query(..., description="Ticker symbol")):
    ticker = ticker.upper().strip()
    job_id = await redis_api.get(_ticker_key(ticker))
    if job_id:
        j = await read_job(job_id)
        s = await serialize_status(j)
    else:
        j = await latest_job_for_ticker_db(ticker)
        if not j:
            raise HTTPException(404, "no history for ticker")
        s = {"queue_position": 0, "queue_size": await current_queue_size()}
    return StatusResponse(
        id=j.id, ticker=j.ticker, status=j.status, progress=j.progress,
        queue_position=s["queue_position"], queue_size=s["queue_size"],
        enqueued_at=j.enqueued_at, started_at=j.started_at, finished_at=j.finished_at,
        result=j.result, error=j.error
    )

@app.get("/history/by-date")
async def history_by_date(
    date: str = Query(..., description="YYYY-MM-DD (UTC)"),
    ticker: Optional[str] = Query(None)
):
    day = _parse_ymd(date)
    t = ticker.upper().strip() if ticker else None
    rows = await jobs_by_date_db(day, t)
    return [
        {
            "id": j.id,
            "ticker": j.ticker,
            "status": j.status,
            "enqueued_at": j.enqueued_at,
            "started_at": j.started_at,
            "finished_at": j.finished_at,
            "progress": j.progress,
            "result": j.result,
            "error": j.error,
        } for j in rows
    ]

# Optional: run directly (useful for local quickstart)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", reload=True)
