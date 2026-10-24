"""Configuration models of gateway plugins (FOCA config section `plugins`)."""

import re
from typing import Any, Literal, Self

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator,
)

PLUGIN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*")
"""Pattern of plugin identifiers (entry point names), e.g., `tes.data.crypt4gh`."""

STEP_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
"""Pattern of step identifiers."""

# Keys of steps reserved for later versions (conditions, dependencies)
_RESERVED_STEP_KEYS = ("when", "needs")
# Maximum length of plugin identifiers
_MAX_PLUGIN_ID_LENGTH = 128


class _Model(BaseModel):
    """Base of the plugin configuration models; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


def check_plugin_id(plugin_id: str) -> str:
    """Check a plugin identifier.

    Args:
        plugin_id: Plugin identifier.

    Returns:
        The identifier.

    Raises:
        ValueError: The identifier does not match `PLUGIN_ID_PATTERN` or is
            longer than 128 characters.
    """
    if len(plugin_id) > _MAX_PLUGIN_ID_LENGTH or not PLUGIN_ID_PATTERN.fullmatch(
        plugin_id
    ):
        raise ValueError(f"Invalid plugin identifier: {plugin_id!r}")
    return plugin_id


class AlternativeConfig(_Model):
    """One alternative of a pipeline step: a plugin with parameters.

    Args:
        plugin: Plugin identifier, from `plugins.available`.
        params: Parameters, validated against the plugin's `Params` model.

    Attributes:
        plugin: Plugin identifier.
        params: Parameters.
    """

    plugin: str
    params: dict[str, Any] = {}

    @field_validator("plugin")
    @classmethod
    def _check_plugin(cls, plugin_id: str) -> str:
        """Check the plugin identifier.

        Args:
            plugin_id: Plugin identifier.

        Returns:
            The identifier.
        """
        return check_plugin_id(plugin_id)


class StepConfig(_Model):
    """Step of a pipeline: ordered alternatives (the first that succeeds wins).

    The short forms `<id>` and `{plugin: <id>, params: {...}}` are steps
    with one alternative, whose identifier defaults to the plugin identifier. The
    keys `when` and `needs` are reserved for later versions.

    Args:
        id: Identifier of the step, unique in the pipeline; defaults to the
            plugin of the first alternative.
        alternatives: Alternatives, in order; at least one.
        timeout: Seconds per alternative and call (default:
            `plugins.defaults.timeout`).
        on_failure: What happens when all alternatives failed: `fail` the
            request, or `skip` the step.
        if_missing_phase: Only for steps of `plugins.client_pipelines.append`:
            add the step only if no other step implements this phase (e.g.,
            a default `route` step).

    Attributes:
        id: Identifier of the step.
        alternatives: Alternatives, in order.
        timeout: Seconds per alternative and call, if set.
        on_failure: What happens when all alternatives failed.
        if_missing_phase: Phase that the step is a default for, if any.
    """

    id: str | None = None
    alternatives: list[AlternativeConfig] = Field(min_length=1)
    timeout: PositiveFloat | None = None
    on_failure: Literal["fail", "skip"] = "fail"
    if_missing_phase: Literal["request", "route", "forward", "response"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _short_form(cls, data: Any) -> Any:
        """Expand the short form and reject reserved keys.

        Args:
            data: Step, as configured.

        Returns:
            Step in the long form.

        Raises:
            ValueError: The step uses a reserved key, or both forms.
        """
        if isinstance(data, str):
            data = {"plugin": data}
        if not isinstance(data, dict):
            return data
        reserved = [key for key in _RESERVED_STEP_KEYS if key in data]
        if reserved:
            raise ValueError(
                f"Step keys {reserved} are reserved for a later version of FOCA"
            )
        if "plugin" in data:
            if "alternatives" in data:
                raise ValueError("Steps have either 'plugin' or 'alternatives'")
            data = dict(data)
            alternative = {"plugin": data.pop("plugin")}
            if "params" in data:
                alternative["params"] = data.pop("params")
            data["alternatives"] = [alternative]
        return data

    @model_validator(mode="after")
    def _default_id(self) -> Self:
        """Default the identifier to the first alternative's plugin.

        Returns:
            The step.

        Raises:
            ValueError: The identifier does not match `STEP_ID_PATTERN`.
        """
        if self.id is None:
            self.id = self.alternatives[0].plugin[:64]
        if not STEP_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid step identifier: {self.id!r}")
        return self


def check_pipeline(steps: list[StepConfig]) -> list[StepConfig]:
    """Check that the steps of a pipeline have unique identifiers.

    Args:
        steps: Steps.

    Returns:
        The steps.

    Raises:
        ValueError: Two steps have the same identifier.
    """
    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValueError(f"Duplicate step identifier {step.id!r}: set unique 'id's")
        seen.add(step.id or "")
    return steps


class AvailablePluginConfig(_Model):
    """Entry of the allowlist of plugins (`plugins.available`).

    Args:
        distribution: Name of the distribution that must provide the entry
            point, if pinned.
        version: PEP 440 version specifier that the distribution's version
            must match, if pinned (e.g., `>=1.0,<2`).
        config: Admin settings, validated against the plugin's `Config`.
        client_selectable: Whether clients may choose the plugin in
            per-request pipelines.
        roles: Roles that may choose the plugin (any if empty).
        enabled: Whether the plugin is loaded; if `false`, its alternatives
            are dropped from all pipelines (a kill switch that keeps the
            entry).

    Attributes:
        distribution: Pinned distribution, if any.
        version: Version specifier, if any.
        config: Admin settings.
        client_selectable: Whether clients may choose the plugin.
        roles: Roles that may choose the plugin.
        enabled: Whether the plugin is loaded.
    """

    distribution: str | None = None
    version: str | None = None
    config: dict[str, Any] = {}
    client_selectable: bool = True
    roles: list[str] = []
    enabled: bool = True

    @field_validator("version")
    @classmethod
    def _check_version(cls, version: str | None) -> str | None:
        """Check the version specifier.

        Args:
            version: Version specifier, if any.

        Returns:
            The specifier.

        Raises:
            ValueError: The specifier is invalid as per PEP 440.
        """
        if version is not None:
            try:
                SpecifierSet(version)
            except InvalidSpecifier as exc:
                raise ValueError(f"Invalid version specifier: {version!r}") from exc
        return version


class PluginDefaultsConfig(_Model):
    """Limits of plugin calls and forwarding (`plugins.defaults`).

    Args:
        timeout: Seconds per alternative and call, unless set by the step.
        pipeline_timeout: Seconds for the `request` and `route` phases of a
            request together.
        forward_timeout: Seconds for all forwarding attempts of a request
            together; when exceeded, no further backend is tried (`503`).
        workers: Threads that run hooks (per process); calls beyond fail
            at once, as timed out.

    Attributes:
        timeout: Seconds per alternative and call.
        pipeline_timeout: Seconds for the `request` and `route` phases.
        forward_timeout: Seconds for all forwarding attempts.
        workers: Threads that run hooks.
    """

    timeout: PositiveFloat = 10
    pipeline_timeout: PositiveFloat = 30
    forward_timeout: PositiveFloat = 60
    workers: PositiveInt = 8


class PluginsConfig(_Model):
    """Gateway plugins: allowlist and pipelines (FOCA config section `plugins`).

    Args:
        available: Allowlist of plugins by identifier (entry point name in
            the group `foca.plugins`): only these are loaded, even if more
            are installed.
        defaults: Limits of plugin calls and forwarding.
        pipeline: Default pipeline: steps, in order.
        gateway_id: Name of this gateway in `Via` headers (RFC 9110,
            section 7.6.3), which detect routing loops; defaults to the host
            of `server.public_url`. Without either, loops are not detected.

    Attributes:
        available: Allowlist of plugins.
        defaults: Limits of plugin calls and forwarding.
        pipeline: Default pipeline.
        gateway_id: Name of this gateway in `Via` headers, if set.

    Examples:
        >>> conf = PluginsConfig(
        ...     available={"org.example.tes.policy.images": {}},
        ...     pipeline=[{"plugin": "org.example.tes.policy.images"}],
        ... )
        >>> conf.pipeline[0].id
        'org.example.tes.policy.images'
    """

    available: dict[str, AvailablePluginConfig] = {}
    defaults: PluginDefaultsConfig = PluginDefaultsConfig()
    pipeline: list[StepConfig] = []
    gateway_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,255}$")

    @field_validator("pipeline")
    @classmethod
    def _check_pipeline(cls, steps: list[StepConfig]) -> list[StepConfig]:
        """Check the default pipeline.

        Args:
            steps: Steps.

        Returns:
            The steps.
        """
        return check_pipeline(steps)

    @field_validator("available")
    @classmethod
    def _check_available(
        cls, available: dict[str, AvailablePluginConfig]
    ) -> dict[str, AvailablePluginConfig]:
        """Check the plugin identifiers of the allowlist.

        Args:
            available: Allowlist.

        Returns:
            The allowlist.
        """
        for plugin_id in available:
            check_plugin_id(plugin_id)
        return available

    def pipelines(self) -> dict[str, list[StepConfig]]:
        """Get all admin-defined pipelines, by name.

        Returns:
            Pipelines, by name (`pipeline` for the default pipeline).
        """
        return {"pipeline": self.pipeline}

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        """Check that pipelines refer to allowlisted plugins only.

        Returns:
            The configuration.

        Raises:
            ValueError: A pipeline refers to a plugin that is not in
                `available`, or a step other than in
                `client_pipelines.append` sets `if_missing_phase`.
        """
        for name, steps in self.pipelines().items():
            for step in steps:
                if step.if_missing_phase is not None and name != "append":
                    raise ValueError(
                        f"'if_missing_phase' is only allowed in"
                        f" 'client_pipelines.append' (step {step.id!r} of {name})"
                    )
                for alternative in step.alternatives:
                    if alternative.plugin not in self.available:
                        raise ValueError(
                            f"Plugin {alternative.plugin!r} of {name} is not in"
                            " 'plugins.available'"
                        )
        return self
