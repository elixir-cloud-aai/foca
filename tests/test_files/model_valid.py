"""Valid custom configuration model."""

from pydantic import BaseModel


class CustomConfig(BaseModel):
    """Valid custom config model test class.

    Args:
        param: Test parameter.
    """

    param: str = "STRING"


class FailingConfig:
    """Custom config model test class that fails with its input."""

    def __init__(self, **kwargs: str) -> None:
        """Fail.

        Args:
            kwargs: Settings.

        Raises:
            TypeError: Always, showing the settings.
        """
        raise TypeError(str(kwargs))
