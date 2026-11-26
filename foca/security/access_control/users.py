"""Users named in policy rules, and whether they apply to anybody."""

import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from foca.security.access_control.constants import (
    ACCESS_CONTROL_BASE_PATH,
    DEFAULT_MODEL_FILE,
    GRANTED_RIGHTS_MODEL_FILE,
    RESOURCE_ROLE_MODEL_FILE,
)
from foca.security.access_control.foca_casbin_adapter.adapter import (
    unknown_user_pattern,
)

if TYPE_CHECKING:  # the config models import access control
    from foca.models.config import AccessControlConfig, AuthConfig


def _model_tokens(text: str) -> list[str]:
    """Get the tokens of a Casbin model, without whitespace and comments.

    Args:
        text: Text of the model; comments start with `#` and run to the end
            of the line.

    Returns:
        Tokens of the definitions, split at whitespace.
    """
    return [
        token for line in text.splitlines() for token in line.split("#", 1)[0].split()
    ]


@lru_cache(maxsize=1)
def _packaged_model_tokens() -> tuple[list[str], ...]:
    """Get the tokens of the models that FOCA ships (cf. :py:func:`_model_tokens`).

    Returns:
        Tokens of each model.
    """
    packaged = files(ACCESS_CONTROL_BASE_PATH)
    return tuple(
        _model_tokens(packaged.joinpath(name).read_text(encoding="utf-8"))
        for name in (
            DEFAULT_MODEL_FILE,
            RESOURCE_ROLE_MODEL_FILE,
            GRANTED_RIGHTS_MODEL_FILE,
        )
    )


def _is_packaged_model(model: str | None) -> bool:
    """Check whether a model is one that FOCA ships, or a copy of one.

    Copies count, too (e.g., of apps that copied a model before
    `foca:<file>` existed), as they match subjects in the same way. Files
    are compared ignoring whitespace and comments (cf.
    :py:func:`_model_tokens`).

    Args:
        model: Path of the model (`access_control.model`, resolved); `None`
            for the default model.

    Returns:
        Whether `model` is the default model, or a file with the definitions
        of a model that FOCA ships.
    """
    if model is None:
        return True
    path = Path(model)
    if not path.is_file():
        return False
    return _model_tokens(path.read_text(encoding="utf-8")) in _packaged_model_tokens()


def unknown_user_matcher(
    access_control_config: "AccessControlConfig", auth_config: "AuthConfig"
) -> re.Pattern[str] | None:
    """Get a pattern of rule values naming users that apply to nobody.

    Users are named in rules as `<subject_prefix><namespace>|<subject>`, or
    without namespace for an issuer that keeps its subjects bare (cf.
    :py:func:`foca.security.auth.namespace_subject`). Under the models that
    FOCA ships, rules naming other users (e.g., bare users of an app that
    stored them up to FOCA 0.22) apply to nobody; custom models may match
    users in other ways (e.g., `user:*` with `keyMatch`).

    Args:
        access_control_config: Access control configuration.
        auth_config: Authentication configuration, for the namespaces of the
            trusted issuers.

    Returns:
        Pattern matching the rule values (cf.
        :py:func:`foca.security.access_control.foca_casbin_adapter.adapter.unknown_user_pattern`)
        that name users of no trusted issuer, or `None` if any user may apply:
        no issuer namespaces its subjects (e.g., none is trusted), or the
        model is not one that FOCA ships.
    """
    namespaces = list(auth_config.get_namespaces().values())
    if not any(namespaces) or not _is_packaged_model(access_control_config.model):
        return None
    return re.compile(
        unknown_user_pattern(access_control_config.subject_prefix, namespaces)
    )
