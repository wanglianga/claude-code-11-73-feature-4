import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DEFAULT_URL = "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/member_bitemp"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_URL)


def _ensure_database(url: str) -> None:
    """Create the application database if it does not exist (local dev convenience)."""
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("select 1"))
        eng.dispose()
        return
    except Exception:
        pass
    # connect to a maintenance db and create the target database
    head, _, dbname = url.rpartition("/")
    if not dbname:
        return
    for admin_db in ("ccdev", "postgres", "template1"):
        try:
            eng = create_engine(head + "/" + admin_db, isolation_level="AUTOCOMMIT")
            with eng.connect() as conn:
                exists = conn.execute(
                    text("select 1 from pg_database where datname=:d"), {"d": dbname}
                ).scalar()
                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{dbname}"'))
            eng.dispose()
            return
        except Exception:
            continue


_ensure_database(DATABASE_URL)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
