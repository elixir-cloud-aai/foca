"""Entry point for petstore example app."""

from foca import Foca
from foca.security.access_control.enforcement import get_enforcer

# Policy rules the app relies on; ownership rules are added by controllers
POLICIES = [
    ("*", "/pets", "(GET)|(POST)"),
    ("owner", "/pets/:id", "(GET)|(DELETE)"),
]

if __name__ == "__main__":
    foca = Foca(config_file="config.yaml")
    app = foca.create_app()
    with app.app.app_context():
        enforcer = get_enforcer()
        for policy in POLICIES:
            # No-op if the rule already exists
            enforcer.add_policy(*policy)
    app.run()
