"""Default route step of gateways: ordered failover (`foca.routing.ordered`).

The least surprising routing for an operator who lists backends: the
candidates are tried in the order of the backend list (cf.
:py:class:`foca.plugins.backends.BackendList`), the next one only if a
backend certainly did not accept the request. Backends discovered in the
same service registry are shuffled among themselves, so that the
registry's own order does not decide which backend gets the load; there is
no round-robin state.

The plugin is built into FOCA (cf.
:py:data:`foca.plugins.config.BUILTIN_PLUGINS`, its only source; it is no
entry point of FOCA's metadata): it is available without an entry in
`plugins.available` (listing it there sets only `enabled`, `roles`,
`client_selectable` and `config`), is the default pipeline, and is
appended to per-request pipelines without a route step.
"""

import random
from typing import cast

from pydantic import BaseModel, ConfigDict

from foca.plugins.base import InvalidParams, Plugin
from foca.plugins.context import Context
from foca.plugins.model import Backend


class RouterConfig(BaseModel):
    """Admin settings of the router.

    Args:
        allow_seed: Whether pipelines may set a `seed` (for tests only:
            seeded orders are predictable).

    Attributes:
        allow_seed: Whether pipelines may set a `seed`.
    """

    model_config = ConfigDict(extra="forbid")

    allow_seed: bool = False


class RouterParams(BaseModel):
    """Parameters of the router in a pipeline step.

    Args:
        labels: Labels that candidates must have, e.g.,
            `{"region": "eu-north"}`; all candidates if empty.
        seed: Seed of the shuffling, for tests; refused unless
            `allow_seed` is set.

    Attributes:
        labels: Labels that candidates must have.
        seed: Seed of the shuffling, if any.
    """

    model_config = ConfigDict(extra="forbid")

    labels: dict[str, str] = {}
    seed: int | None = None


class OrderedRouter(Plugin[RouterConfig, RouterParams]):
    """Ordered failover.

    Keeps the order of the backend list and shuffles the backends of each
    service registry among themselves; optionally keeps only backends with
    given labels.
    """

    Config = RouterConfig
    Params = RouterParams
    maturity = "stable"

    def check_params(self, params: RouterParams) -> None:
        """Refuse seeds unless allowed.

        Args:
            params: Validated parameters.

        Raises:
            InvalidParams: A seed is set, but not allowed.
        """
        if params.seed is not None and not self.config.allow_seed:
            raise InvalidParams("'seed' is not allowed (cf. 'allow_seed')")

    def on_route(self, candidates: list[Backend], ctx: Context) -> list[Backend]:
        """Order the candidates.

        Args:
            candidates: Candidates, in the order of the backend list.
            ctx: Context.

        Returns:
            Candidates with the labels, in order, with the backends of each
            registry shuffled in place.
        """
        params = cast(RouterParams, ctx.params)
        kept = [
            backend
            for backend in candidates
            if all(backend.labels.get(k) == v for k, v in params.labels.items())
        ]
        if len(kept) < len(candidates):
            ctx.record("filtered", len(candidates) - len(kept))
        rng = (
            random.Random(params.seed)  # noqa: S311 (tests only)
            if params.seed is not None
            else random.SystemRandom()
        )
        positions: dict[str, list[int]] = {}
        for index, backend in enumerate(kept):
            if backend.source == "discovered" and backend.group is not None:
                positions.setdefault(backend.group, []).append(index)
        ordered = list(kept)
        for indexes in positions.values():
            group = [kept[index] for index in indexes]
            rng.shuffle(group)
            for index, backend in zip(indexes, group, strict=True):
                ordered[index] = backend
        if positions:
            ctx.record("shuffled_groups", len(positions))
        return ordered
