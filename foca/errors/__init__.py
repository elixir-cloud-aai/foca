"""Error handling and problem responses."""

from foca.errors.classes import (
    AccessDeniedProblem,
    AuthenticationProblem,
    BadRequest,
    Conflict,
    ExtraParameterProblem,
    Forbidden,
    HTTPError,
    InternalServerError,
    InvalidFieldsProblem,
    MethodNotAllowed,
    NotFound,
    PayloadTooLarge,
    RequestValidationProblem,
    ServiceUnavailable,
    Unauthorized,
    UnprocessableContent,
)
from foca.errors.fields import FieldError

__all__ = [
    "AccessDeniedProblem",
    "AuthenticationProblem",
    "BadRequest",
    "Conflict",
    "ExtraParameterProblem",
    "FieldError",
    "Forbidden",
    "HTTPError",
    "InternalServerError",
    "InvalidFieldsProblem",
    "MethodNotAllowed",
    "NotFound",
    "PayloadTooLarge",
    "RequestValidationProblem",
    "ServiceUnavailable",
    "Unauthorized",
    "UnprocessableContent",
]
