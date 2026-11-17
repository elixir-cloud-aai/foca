"""Test cases of the Python rules in `foca.yml` (`semgrep --test`)."""

import jwt
import jwt as pyjwt
import requests
import yaml
from jwt import decode as jwt_decode
from jwt import get_unverified_header
from yaml import full_load as yaml_full_load

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
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify=0)
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": 0})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=dict(verify_signature=0))  # noqa: C408
zero = {"verify_signature": 0}
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=zero)
zero_dict = dict(verify_signature=0)  # noqa: C408
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=zero_dict)
# ruleid: jwt-decode-without-verification
jwt_decode(TOKEN, options={"verify_signature": False})
# ruleid: jwt-decode-without-verification
pyjwt.decode(TOKEN, options={"verify_signature": False})
unverified = {"verify_signature": False}
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=unverified)
unverified_dict = dict(verify_signature=False)  # noqa: C408
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=unverified_dict)
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": None})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=dict(verify_signature=None))  # noqa: C408
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": ""})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify=None)
none_options = {"verify_signature": None}
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=none_options)
none_dict = dict(verify_signature=None)  # noqa: C408
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=none_dict)
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": 0.0})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify="")
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options=dict(verify_signature=""))  # noqa: C408
VERIFY = False
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, options={"verify_signature": VERIFY})
# ruleid: jwt-decode-without-verification
jwt.decode(TOKEN, verify=VERIFY)
# ok: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], options={"verify_signature": True})
# ok: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], options={"verify_signature": "yes"})
# ok: jwt-decode-without-verification
jwt.decode(TOKEN, KEY, algorithms=["RS256"], verify=1)
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
imported_header = get_unverified_header(TOKEN)
# ruleid: jwt-decode-unsafe-algorithms
pyjwt.decode(TOKEN, KEY, algorithms=[imported_header["alg"]])
alg = header["alg"]
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=[alg])
alg_get = jwt.get_unverified_header(TOKEN).get("alg", "RS256")
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode_complete(TOKEN, KEY, algorithms=[alg_get])
header_algorithms = [header["alg"]]
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=header_algorithms)
none_algorithms = ["RS256", "NONE"]
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=none_algorithms)
# ruleid: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms={"none"})


def decode_with_header_algorithm(token: str) -> dict:
    """Decode a token with the algorithm that its header names."""
    alg = jwt.get_unverified_header(token)["alg"]
    # ruleid: jwt-decode-unsafe-algorithms
    return jwt.decode(token, KEY, algorithms=[alg])


def decode_with_key_algorithm(token: str, jwks: dict) -> dict:
    """Decode a token with the algorithm of the key that its header names."""
    jwk = jwks[jwt.get_unverified_header(token)["kid"]]
    # ok: jwt-decode-unsafe-algorithms
    return jwt.decode(token, jwk.key, algorithms=[jwk.algorithm_name])


def decode_without_none(token: str, algorithms: set[str]) -> dict:
    """Decode a token with the given algorithms, but `none`."""
    allowed = algorithms - {"none"}
    # ok: jwt-decode-unsafe-algorithms
    return jwt.decode(token, KEY, algorithms=list(allowed))


# ok: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["none-of-these"])
# ok: jwt-decode-unsafe-algorithms
jwt.decode(TOKEN, KEY, algorithms=["RS256", "ES256"])

# yaml-unsafe-load

# ruleid: yaml-unsafe-load
yaml.unsafe_load("a: 1")
# ruleid: yaml-unsafe-load
yaml.full_load("a: 1")
# ruleid: yaml-unsafe-load
yaml_full_load("a: 1")
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
# ruleid: tls-verification-disabled
session.verify = 0
# ruleid: tls-verification-disabled
requests.get("https://example.org", verify=0, timeout=10)
# ruleid: tls-verification-disabled
session.get("https://example.org", verify=False, timeout=10)
# ruleid: tls-verification-disabled
session.request("GET", "https://example.org", verify=0, timeout=10)
request = session.prepare_request(requests.Request("GET", "https://example.org"))
# ruleid: tls-verification-disabled
session.send(request, verify=False, timeout=10)
# ruleid: tls-verification-disabled
requests.Session().post("https://example.org", verify=False, timeout=10)
# Also flagged by ruff (S501), which is suppressed here
# ruleid: tls-verification-disabled
requests.get("https://example.org", verify=False, timeout=10)  # noqa: S501
# ruleid: tls-verification-disabled
session.put("https://example.org", verify="", timeout=10)
TLS_VERIFY = False
# ruleid: tls-verification-disabled
session.patch("https://example.org", verify=TLS_VERIFY, timeout=10)
# ruleid: tls-verification-disabled
session.verify = TLS_VERIFY
# ok: tls-verification-disabled
session.get("https://example.org", verify=None, timeout=10)
# ok: tls-verification-disabled
session.verify = "/etc/ssl/certs/ca-certificates.crt"
# ok: tls-verification-disabled
session.get("https://example.org", verify=True, timeout=10)
# ok: tls-verification-disabled
session.get("https://example.org", verify="/etc/ssl/certs/ca.pem", timeout=10)
