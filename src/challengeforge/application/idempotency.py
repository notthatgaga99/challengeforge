from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from challengeforge.domain.exceptions import ValidationFailed


def submission_request_fingerprint(
    challenge_id: UUID, metadata: dict[str, Any]
) -> str:
    """Hash the stable, meaningful input of submission creation.

    We store only the digest. Later artifact attachment is a separate operation
    and therefore is intentionally excluded.
    """
    try:
        canonical = json.dumps(
            {
                "challenge_id": str(challenge_id),
                "metadata": metadata,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(
            "Submission metadata must have a stable JSON representation."
        ) from exc
    return hashlib.sha256(canonical).hexdigest()
