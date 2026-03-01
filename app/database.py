from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import DATABASE_URL


connect_args = {}
pool_kwargs: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    # Single shared connection — matches our single-request-at-a-time model
    # and eliminates lock contention between pooled connections.
    pool_kwargs["poolclass"] = StaticPool
else:
    # Verify connections are alive before handing them out from the pool.
    # Prevents "server closed the connection unexpectedly" errors after
    # idle periods on PostgreSQL.
    pool_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, connect_args=connect_args, **pool_kwargs)

# SQLite pragmas: WAL mode for concurrent readers, busy_timeout to wait
# instead of raising SQLITE_BUSY immediately on write contention.
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
