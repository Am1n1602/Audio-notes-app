"""Fixed throwaway config for every test, so tests never depend on a developer's real .env.

The database is the local docker-compose Postgres (127.0.0.1:5432, dev-only credentials), using a separate
`audio_notes_test` database that the test suite creates and migrates itself. Storage is always faked in tests.
"""

import os

os.environ["DATABASE_URL"] = "postgresql://audio:audio@127.0.0.1:5432/audio_notes_test"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["CORS_ORIGINS"] = "http://localhost:3000"
os.environ["STORAGE_REGION"] = "ap-south-1"
os.environ["STORAGE_BUCKET"] = "test-bucket"
os.environ["STORAGE_ACCESS_KEY_ID"] = "AKIATESTKEY"
os.environ["STORAGE_SECRET_ACCESS_KEY"] = "test-secret-value"
os.environ["STORAGE_ENDPOINT"] = ""
os.environ["LLM_API_KEY"] = "test-llm-key"
os.environ["GNANI_API_KEY"] = "test-gnani-key"
os.environ["GNANI_BASE_URL"] = "https://gnani.test"
