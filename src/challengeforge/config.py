from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://challengeforge:challengeforge@localhost:5432/challengeforge"
    )
    artifact_root: Path = Path("./data/artifacts")
    log_level: str = "INFO"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    metadata_max_bytes: int = 32 * 1024
    artifact_max_bytes: int = 5 * 1024 * 1024
    # Evaluation worker (async pipeline)
    evaluation_stale_after_seconds: int = 30
    evaluation_max_attempts: int = 3
    evaluation_poll_interval_seconds: float = 0.25
    evaluation_fake_work_ms: int = 50
    # Environment-specific operating envelope from the laptop experiment.
    # These are explicit limits, not an auto-scaling or resource prediction model.
    evaluation_max_workers: int = Field(default=1, ge=1)
    evaluation_max_concurrent_heavy: int = Field(default=1, ge=1)
    evaluation_light_bypass_limit: int = Field(default=2, ge=0)
    evaluation_scheduling_policy: Literal["fifo", "bounded_light_bypass"] = (
        "bounded_light_bypass"
    )
    # Backlog health thresholds (observability / messaging — not submission rejection)
    evaluation_busy_queue_depth: int = 20
    evaluation_busy_oldest_age_seconds: float = 5.0
    evaluation_saturated_queue_depth: int = 100
    evaluation_saturated_oldest_age_seconds: float = 30.0
    evaluation_critical_queue_depth: int = 500
    evaluation_service_rate_window_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
