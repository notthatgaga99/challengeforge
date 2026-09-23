"""Ingestion package — durable PARSE→NORMALIZE→READY jobs."""

from challengeforge.ingestion.processor import process_artifact
from challengeforge.ingestion.worker import IngestionWorker

__all__ = ["IngestionWorker", "process_artifact"]
