from challengeforge.api.routes.challenges import router as challenge_router
from challengeforge.api.routes.hackathons import router as hackathon_router
from challengeforge.api.routes.health import router as health_router
from challengeforge.api.routes.identity import router as identity_router

__all__ = ["challenge_router", "hackathon_router", "health_router", "identity_router"]
