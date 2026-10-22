"""Test cases of the Python rules in `foca.yml` (`semgrep --test`)."""

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
jwt.decode_complete(TOKEN, options={"verify_signature": False})
# ruleid: jwt-decode-without-verification
jwt.PyJWT().decode(TOKEN, options={"verify_signature": False})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=dict(verify_signature=False))  # noqa: C408
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify=False)
unverified = {"verify_signature": False}
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=unverified)
# ok: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], options={"verify_aud": False})
# ok: jwt-decode-without-verification
b"token".decode("utf-8")

# jwt-decode-unsafe-algorithms

# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["none"])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["RS256", "None"])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode_complete(TOKEN, KEY, algorithms=("none",))
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=[jwt.get_unverified_header(TOKEN)["alg"]])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=jwt.get_unverified_header(TOKEN).get("alg"))
header = jwt.get_unverified_header(TOKEN)
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=[header["alg"]])
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=header.get("alg"))
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
# ruleid: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.Loader)
# ruleid: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.CUnsafeLoader)
# ruleid: yaml-unsafe-load
yaml.load_all("a: 1", yaml.FullLoader)
# ok: yaml-unsafe-load
yaml.safe_load("a: 1")
# ok: yaml-unsafe-load
yaml.load_all("a: 1", Loader=yaml.SafeLoader)
# ok: yaml-unsafe-load
yaml.load_all("a: 1", yaml.CSafeLoader)

# tls-verification-disabled

session = requests.Session()
# ruleid: tls-verification-disabled
session.verify = False
# ok: tls-verification-disabled
session.verify = "/etc/ssl/certs/ca-certificates.crt"
