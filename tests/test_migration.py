import subprocess


def test_postgresql_migration_renders_offline():
    result = subprocess.run(
        ["alembic", "upgrade", "head", "--sql"], capture_output=True, text=True, check=True
    )
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm" in result.stdout
    assert "CREATE TABLE signal_documents" in result.stdout
    assert "UNIQUE (eisgs_id)" in result.stdout
    assert "UNIQUE (dedup_key)" in result.stdout
    assert "TIMESTAMP WITH TIME ZONE" in result.stdout
