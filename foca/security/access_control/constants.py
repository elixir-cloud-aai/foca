"""File to store permission based constants."""

import string
from datetime import timedelta

DEFAULT_ACCESS_CONTROL_DB_NAME = "access_control_db"
DEFAULT_ACCESS_CONTROL_COLLECTION_NAME = "policy_rules"
DEFAULT_API_SPEC_PATH = "access-control-specs.yaml"
DEFAULT_MODEL_FILE = "default_model.conf"
RESOURCE_ROLE_MODEL_FILE = "resource_role_model.conf"

ACCESS_CONTROL_BASE_PATH = "foca.security.access_control.api"

# Field marking stored policy rules set via `access_control.policies`
CONFIGURED_FIELD = "configured"
# Field listing the generations of configured rules (the sets of configured
# rules stored on startup) that a configured rule belongs to
RULE_GENERATIONS_FIELD = "generations"
# MongoDB identifier (`_id`) of the document in the policy collection that
# names the active generation, in field `MARKER_GENERATION_FIELD`
GENERATION_MARKER_ID = "configured_policies"
MARKER_GENERATION_FIELD = "generation"
# Generation of configured rules stored by FOCA 0.16 and earlier, i.e.,
# without generations; active as long as no other generation is
LEGACY_GENERATION = "legacy"
# Age after which generations that never became active (e.g., because the
# app instance storing them failed) are removed
STALE_GENERATION_AGE = timedelta(hours=1)
# Attempts to load all rules while the active generation does not change
LOAD_POLICY_ATTEMPTS = 5
# Timeout (in seconds) of the check for stored configured rules on startup
# without `access_control.policies`, after which apps start without the
# database
CONFIGURED_POLICIES_CHECK_TIMEOUT = 5
# Field ordering role rules stored by `Adapter.replace_role_rule()`
GRANTED_FIELD = "granted"

# Prefix of group claim values used as roles without `group_mapping`
GROUP_PREFIX = "group:"
# Prefix of service clients (client IDs) in policy rules
CLIENT_PREFIX = "client:"
# Fixed prefixes of subject types other than users (whose prefix is
# configurable via `access_control.subject_prefix`)
SUBJECT_TYPE_PREFIXES = {"group": GROUP_PREFIX, "client": CLIENT_PREFIX}

# Default roles on resources managed by the resource ownership helpers: the
# owner role, which the creator of a resource gets, and the maintainer role
OWNER_ROLE = "owner"
MAINTAINER_ROLE = "maintainer"
# Role of subjects that may do everything, as in the reference model
# (`resource_role_model.conf`)
ADMIN_ROLE = "admin"

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
