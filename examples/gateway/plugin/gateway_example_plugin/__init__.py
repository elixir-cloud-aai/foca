"""Plugin of the gateway example."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from foca.plugins import Context, InboundRequest, Plugin
from foca.plugins.base import EmptyModel


class Config(BaseModel):
    """Admin settings of the plugin (`plugins.available.<id>.config`).

    Attributes:
        stamp: Value of the field that the plugin adds to requests.
    """

    model_config = ConfigDict(extra="forbid")

    stamp: str


class Stamp(Plugin[Config, EmptyModel]):
    """Stamp requests, and show what the plugin's store does in this process.

    Adds to the payload: the configured stamp, a counter of the requests in
    the store, the store's mode and a key derived with the store's secret.
    """

    Config = Config
    apis = frozenset({"*"})
    maturity = "experimental"
    data_stored: ClassVar[tuple[str, ...]] = ("number of requests",)

    def on_request(self, request: InboundRequest, ctx: Context) -> None:
        """Add the stamp and the details of the store to the payload.

        Args:
            request: Inbound request.
            ctx: Context of the call.
        """
        request.payload.update(
            {
                "stamp": self.config.stamp,
                "count": ctx.store.increment("requests"),
                "store_mode": ctx.store.mode,
                "store_key": ctx.store.key_for("example"),
            }
        )
