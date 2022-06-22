from pydantic import BaseModel


class CustomConfig(BaseModel):
    """Valid custom config model test class.

    Args:
        param: Test parameter.
    """
    param: str = 'STRING'
