"""Test cases of the Python rules in `foca.yml` (`semgrep --test .semgrep/`)."""

import jwt
import requests
import yaml

TOKEN = "token"  # noqa: S105
KEY = "key"

# jwt-decode-without-verification

# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": False})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], options={"verify_signature": False})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify=False)
# ok: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], options={"verify_aud": False})

# jwt-decode-unsafe-algorithms

# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["none"])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["RS256", "None"])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=[jwt.get_unverified_header(TOKEN)["alg"]])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=jwt.get_unverified_header(TOKEN).get("alg"))
# ok: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["RS256", "ES256"])

# yaml-unsafe-load

# ruleid: yaml-unsafe-load
yaml.unsafe_load("a: 1")
# ruleid: yaml-unsafe-load
yaml.full_load("a: 1")
# ruleid: yaml-unsafe-load
yaml.unsafe_load_all("a: 1")
# ruleid: yaml-unsafe-load
yaml.full_load_all("a: 1")
# ruleid: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.UnsafeLoader)
# ruleid: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.FullLoader)
# ok: yaml-unsafe-load
yaml.safe_load("a: 1")
# ok: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.SafeLoader)

# tls-verification-disabled

session = requests.Session()
# ruleid: tls-verification-disabled
session.verify = False
# ok: tls-verification-disabled
session.verify = "/etc/ssl/certs/ca-certificates.crt"
