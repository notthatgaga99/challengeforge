from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return render(request, "hackathons.html", page="hackathons")


@router.get("/hackathons/{hackathon_id}", response_class=HTMLResponse)
async def hackathon_page(request: Request, hackathon_id: str) -> HTMLResponse:
    return render(request, "hackathon.html", page="hackathon", hackathon_id=hackathon_id)


@router.get("/challenges/{challenge_id}", response_class=HTMLResponse)
async def challenge_page(request: Request, challenge_id: str) -> HTMLResponse:
    return render(request, "challenge.html", page="challenge", challenge_id=challenge_id)


@router.get("/submissions/{submission_id}", response_class=HTMLResponse)
async def submission_page(request: Request, submission_id: str) -> HTMLResponse:
    return render(
        request, "submission.html", page="submission", submission_id=submission_id
    )


@router.get("/organizer", response_class=HTMLResponse)
async def organizer_page(request: Request) -> HTMLResponse:
    return render(request, "organizer.html", page="organizer")


@router.get("/my-submissions", response_class=HTMLResponse)
async def my_submissions_page(request: Request) -> HTMLResponse:
    return render(request, "my_submissions.html", page="my-submissions")
