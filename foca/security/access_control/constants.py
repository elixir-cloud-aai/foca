"""File to store permission based constants."""

import string

DEFAULT_ACCESS_CONTROL_DB_NAME = "access_control_db"
DEFAULT_ACESS_CONTROL_COLLECTION_NAME = "policy_rules"
DEFAULT_API_SPEC_PATH = "access-control-specs.yaml"
DEFAULT_MODEL_FILE = "default_model.conf"
RESOURCE_ROLE_MODEL_FILE = "resource_role_model.conf"
DEFAULT_SPEC_CONTROLLER = "foca.security.access_control.access_control_server"

ACCESS_CONTROL_BASE_PATH = "foca.security.access_control.api"

# Prefix of group claim values used as roles without `group_mapping`
GROUP_PREFIX = "group:"

# Identifiers of policy rules: characters, length and attempts to generate a
# unique identifier
DEFAULT_ID_CHARSET = string.ascii_letters + string.digits
DEFAULT_ID_LENGTH = 6
ID_ATTEMPTS = 5
# Minimum number of possible identifiers for configured characters and length
MIN_ID_SPACE = 1_000_000

# Default and maximum number of permissions per page of the permissions API
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
