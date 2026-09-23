"""Ingestion package — durable PARSE→NORMALIZE→CHUNK→READY jobs."""

from challengeforge.ingestion.chunking import chunk_hybrid
from challengeforge.ingestion.processor import process_artifact
from challengeforge.ingestion.worker import IngestionWorker

__all__ = ["IngestionWorker", "chunk_hybrid", "process_artifact"]
