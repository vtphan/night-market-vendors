import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Override env vars BEFORE importing app modules
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only"
os.environ["ADMIN_EMAILS"] = "admin@test.com"
os.environ["DEBUG"] = "true"
os.environ["RESEND_API_KEY"] = "re_test_fake"
os.environ["EMAIL_FROM"] = "test@test.com"
os.environ["APP_URL"] = "http://localhost:8000"

from app.database import Base, get_db
from app.main import app

# Create a test engine — StaticPool ensures all connections share the same
# in-memory database (required for SQLite :memory: with multiple threads).
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def setup_db():
    """Create tables before each test, drop after."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db():
    """Provide a test database session."""
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
