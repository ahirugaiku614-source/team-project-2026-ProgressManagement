"""Explicit submission endpoints; existing lab extension is not involved."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session

from app import models
from app.db.database import get_db
from app.submission_provider import get_submission_provider, ProviderUnavailable
from app.submission_targets import TargetForbidden, TargetInvalid, TargetConflict
from app.submissions import issue_permit, submit


class PermitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    editing_session_id: UUID


class SubmissionRequest(PermitRequest):
    request_id: UUID
    permit: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    has_unsaved_changes: StrictBool


class SubmissionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except RequestValidationError:
                raise HTTPException(422, "Invalid submission request") from None
            except TargetForbidden:
                raise HTTPException(403, "Submission is not permitted") from None
            except TargetInvalid:
                raise HTTPException(404, "Submission target not found") from None
            except TargetConflict:
                raise HTTPException(409, "Submission target is not configured") from None
            except ProviderUnavailable:
                raise HTTPException(503, "Submission collector is not connected") from None
        return safe


def create_submission_router(member_resolver):
    router = APIRouter(route_class=SubmissionRoute)
    prefix = "/api/teams/{team_id}/projects/{project_id}/tasks/{task_id}"

    @router.post(prefix + "/submission-permits", status_code=201)
    def permit(team_id: int, project_id: int, task_id: int, payload: PermitRequest,
               user: models.User = Depends(member_resolver), db: Session = Depends(get_db)):
        return issue_permit(db, user, team_id, project_id, task_id, str(payload.editing_session_id))

    @router.post(prefix + "/submissions", status_code=202)
    def submission(team_id: int, project_id: int, task_id: int, payload: SubmissionRequest,
                   response: Response, user: models.User = Depends(member_resolver),
                   db: Session = Depends(get_db), provider=Depends(get_submission_provider)):
        if payload.has_unsaved_changes:
            raise HTTPException(409, "Save all task files before submitting")
        result, created = submit(db, user, team_id, project_id, task_id, payload, provider)
        response.status_code = 202 if created else 200
        return result
    return router
