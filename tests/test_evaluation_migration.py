"""Alembic upgrade / downgrade / upgrade for the evaluation migration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_evaluation_migration_upgrade_downgrade_upgrade(database_url: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    except Exception as exc:
        pytest.skip(f"Could not reset schema for alembic test: {exc}")
    finally:
        await engine.dispose()

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    steps = [
        ("upgrade", "head"),
        ("downgrade", "0003_evaluation_pipeline"),
        ("upgrade", "head"),
    ]
    for action, target in steps:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", action, target],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(
                f"alembic {action} {target} failed:\n"
                f"{completed.stdout}\n{completed.stderr}"
            )

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.tables
                        WHERE table_name = 'evaluations'
                        """
                    )
                )
            ).scalar()
            assert exists == 1
    finally:
        await engine.dispose()
