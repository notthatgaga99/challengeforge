"""Stage confidence outcomes for synthetic progressive evaluation."""

from __future__ import annotations

from enum import StrEnum


class StageConfidence(StrEnum):
    PASS_CONFIDENT = "pass_confident"
    FAIL_CONFIDENT = "fail_confident"
    UNCERTAIN = "uncertain"
