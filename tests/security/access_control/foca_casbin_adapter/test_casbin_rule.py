"""Tests for initialising casbin rule object.
"""

from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    CasbinRule
)

# Define data
BASE_RULE_OBJECT = {
    "ptype": "ptype",
    "v0": "v0",
    "v1": "v1",
    "v2": "v2",
    "v3": "v3",
    "v4": "v4",
    "v5": "v5"
}
BASE_RULE_STR_REPRESENTATION = "ptype, v0, v1, v2, v3, v4, v5"
BASE_RULE_REPRESENTATION = f'<CasbinRule :"{BASE_RULE_STR_REPRESENTATION}">'


class TestCasbinRule:
    def test_initialise_object(self):
        test_rule = CasbinRule(**BASE_RULE_OBJECT)
        assert test_rule.ptype == "ptype"
        assert test_rule.v0 == "v0"
        assert test_rule.v1 == "v1"
        assert test_rule.v2 == "v2"
        assert test_rule.v3 == "v3"
        assert test_rule.v4 == "v4"
        assert test_rule.v5 == "v5"
        assert repr(test_rule) == BASE_RULE_REPRESENTATION
        assert test_rule.dict() == BASE_RULE_OBJECT
        assert test_rule.__str__() == BASE_RULE_STR_REPRESENTATION
