"""Casbin rule class."""

from typing import (Dict, Optional)


class CasbinRule:
    """This class defines the basic structuring of a casbin rule object.

    Args:
        ptype: Policy type for the given rule based on the given conf file.
        v0: Policy parameter.
        v1: Policy parameter.
        v2: Policy parameter.
        v3: Policy parameter.
        v4: Policy parameter.
        v5: Policy parameter.

    Attributes:
        ptype: Policy type for the given rule based on the given conf file.
        v0: Policy parameter.
        v1: Policy parameter.
        v2: Policy parameter.
        v3: Policy parameter.
        v4: Policy parameter.
        v5: Policy parameter.
    """

    def __init__(
        self,
        ptype: Optional[str] = None,
        v0: Optional[str] = None,
        v1: Optional[str] = None,
        v2: Optional[str] = None,
        v3: Optional[str] = None,
        v4: Optional[str] = None,
        v5: Optional[str] = None
    ):
        """Casbin rule object initializer."""
        self.ptype = ptype
        self.v0 = v0
        self.v1 = v1
        self.v2 = v2
        self.v3 = v3
        self.v4 = v4
        self.v5 = v5

    def dict(self) -> Dict:
        """Method to convert params into casbin rule object.

        Returns:
            Casbin rule object.
        """
        rule_dict = {"ptype": self.ptype}

        for value in dir(self):
            if (
                getattr(self, value) is not None
                and value.startswith("v")
                and value[1:].isnumeric()
            ):
                rule_dict[value] = getattr(self, value)

        return rule_dict

    def __str__(self):
        return ", ".join(self.dict().values())

    def __repr__(self):
        print("self   ", self)
        return '<CasbinRule :"{}">'.format(str(self))
