"""Tests for config.py."""

import doctest
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest
import yaml
from pydantic import ValidationError
from werkzeug.exceptions import NotFound

import foca.models.config
import foca.utils.service_info
from foca.models.config import (
    AccessControlConfig,
    APIConfig,
    AuthConfig,
    CollectionConfig,
    Config,
    DBConfig,
    ExceptionConfig,
    IndexConfig,
    MongoConfig,
    SecurityConfig,
    ServerConfig,
    ServiceInfoConfig,
    SpecConfig,
)
from tests.test_files.service_info_data import FIELDS, REMOVE, SERVICE, service

DIR = Path(__file__).parent / "test_files"
PATH = str(DIR / "openapi_2_petstore.yaml")
PATH_ADDITION = str(DIR / "openapi_2_petstore.addition.yaml")
INDEX_CONFIG: dict[str, Any] = {
    "keys": {"last_name": -1},
    "options": {
        "name": "indexLastName",
        "unique": True,
        "background": False,
        "sparse": False,
    },
}
COLLECTION_CONFIG: dict[str, Any] = {
    "indexes": [INDEX_CONFIG],
}
DB_CONFIG: dict[str, Any] = {
    "collections": {
        "wes-col": COLLECTION_CONFIG,
    },
}
MONGO_CONFIG: dict[str, Any] = {
    "host": "mongodb",
    "port": 27017,
    "dbs": {
        "wes": DB_CONFIG,
    },
}
SPEC_CONFIG: dict[str, Any] = {
    "path": "/my/abs/path",
}
SPEC_CONFIG_REL: dict[str, Any] = {
    "path": "path",
}
SPEC_CONFIG_LIST: dict[str, Any] = {"path": ["path1", "/my/abs/path2"]}


def test_config_empty() -> None:
    """Test basic creation of the main config model."""
    res = Config()
    assert isinstance(res, Config)


def test_server_config_public_url_unset() -> None:
    """Apps have no public URL by default."""
    assert ServerConfig().public_url is None


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://api.example.org/my-app/", "https://api.example.org/my-app"),
        ("HTTP://API.Example.org:80/", "http://api.example.org"),
        ("https://api.example.org:8443", "https://api.example.org:8443"),
        ("https://api.example.org/a%20b", "https://api.example.org/a%20b"),
        ("https://api.example.org/caf\u00e9", "https://api.example.org/caf%C3%A9"),
        ("http://[FE80::1%25eth0]:8080/x/", "http://[fe80::1%25eth0]:8080/x"),
        ("https://api.example.org/x/../y", "https://api.example.org/y"),
        ("https://api.example.org//x//y", "https://api.example.org/x/y"),
    ],
)
def test_server_config_public_url(url: str, normalized: str) -> None:
    """Public URLs are normalized."""
    assert ServerConfig(public_url=url).public_url == normalized


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.org/my-app?x=1",
        "https://api.example.org/my-app?",
        "https://api.example.org/my-app#top",
        "https://api.example.org/my-app#",
        "https://user:secret@api.example.org/",
        "ftp://api.example.org/",
        "mailto:admin@example.org",
        "/my-app",
        "api.example.org",
        "https://api.example.org/a b",
        "",
    ],
)
def test_server_config_invalid_public_url(url: str) -> None:
    """Invalid public URLs, and URLs with a query or fragment, are rejected."""
    with pytest.raises(ValidationError, match=r"server\.public_url"):
        ServerConfig(public_url=url)


def test_server_config_public_url_not_string() -> None:
    """Public URLs must be strings."""
    with pytest.raises(ValidationError):
        ServerConfig(public_url=1)  # type: ignore[arg-type]  # invalid on purpose


class PetNotFound(NotFound):
    """Pet not found, a subclass of a werkzeug exception."""


PROBLEMS: dict[type[Exception], dict[str, Any]] = {
    NotFound: {"title": "Not Found", "status": 404},
    PetNotFound: {"title": "Pet Not Found", "status": 404, "detail": "x"},
}


@pytest.fixture
def problems_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Register module `problems_module` with mappings as attributes."""
    module = ModuleType("problems_module")
    vars(module).update(
        valid=PROBLEMS,
        not_dict=[NotFound],
        not_exception={"a": {"title": "A", "status": 400}},
        not_class={NotFound(): {"title": "A", "status": 400}},
        problem_not_dict={NotFound: "Not Found"},
        no_status={NotFound: {"title": "Not Found"}},
        status_str={NotFound: {"title": "Not Found", "status": "404"}},
        status_bool={NotFound: {"title": "Not Found", "status": True}},
        status_200={NotFound: {"title": "OK", "status": 200}},
        no_title={NotFound: {"status": 404}},
    )
    monkeypatch.setitem(sys.modules, "problems_module", module)
    return module


def test_exception_config_empty() -> None:
    """Test basic creation of the ExceptionConfig model."""
    res = ExceptionConfig()
    assert res.mapping is None
    assert res.problems == {}
    assert res.public_members is None
    assert res.media_type == "application/problem+json"


@pytest.mark.parametrize(
    "media_type",
    [
        "application/json",
        "application/problem+json",
        "application/vnd.example.error+json",
        "Application/JSON",
    ],
)
def test_exception_config_media_type(media_type: str) -> None:
    """Test creation of the ExceptionConfig model with a media type."""
    res = ExceptionConfig(media_type=media_type)
    assert res.media_type == media_type.lower()


@pytest.mark.parametrize(
    "media_type",
    [
        "",
        "json",
        "application/",
        "/json",
        "application/xml",
        "application/problem+xml",
        "text/plain",
        "application/jsonx",
        "application/json; charset=utf-8",
        "application/json\nX-Injected: 1",
        " application/json",
        "application/x+y/json",
        1,
    ],
)
def test_exception_config_invalid_media_type(media_type: str | int) -> None:
    """Test creation of the ExceptionConfig model with invalid media types."""
    with pytest.raises(ValidationError, match="media_type"):
        ExceptionConfig(media_type=media_type)  # type: ignore[arg-type]  # invalid on purpose


def test_exception_config_mapping(problems_module: ModuleType) -> None:
    """Test creation of the ExceptionConfig model with a mapping."""
    res = ExceptionConfig(
        mapping="problems_module.valid",
        public_members=["title", "status"],
        logging="minimal",  # type: ignore[arg-type]  # coerced by Pydantic
    )
    assert res.problems is PROBLEMS
    assert res.public_members == ["title", "status"]


@pytest.mark.parametrize(
    "mapping",
    [
        "some.unavailable.module.problems",
        "problems_module.unavailable",
        "problems",
        "problems_module.not_dict",
        "problems_module.not_exception",
        "problems_module.not_class",
        "problems_module.problem_not_dict",
        "problems_module.no_status",
        "problems_module.status_str",
        "problems_module.status_bool",
        "problems_module.status_200",
        "problems_module.no_title",
    ],
)
def test_exception_config_invalid_mapping(
    problems_module: ModuleType, mapping: str
) -> None:
    """Test creation of the ExceptionConfig model with invalid mappings."""
    with pytest.raises(ValidationError):
        ExceptionConfig(mapping=mapping)


@pytest.mark.parametrize(
    "field",
    [
        {"exceptions": "foca.errors.exceptions.exceptions"},
        {"required_members": [["title"], ["status"]]},
        {"status_member": ["status"]},
        {"private_members": [["detail"]]},
        {"public_members": [["title"]]},
    ],
)
def test_exception_config_removed_fields(field: dict[str, Any]) -> None:
    """Test creation of the ExceptionConfig model with removed fields."""
    with pytest.raises(ValidationError):
        ExceptionConfig(**field)


def test_index_config_empty() -> None:
    """Test basic creation of the IndexConfig model."""
    res = IndexConfig()
    assert isinstance(res, IndexConfig)


def test_index_config_with_data() -> None:
    """Test creation of the IndexConfig model with data."""
    res = IndexConfig(**INDEX_CONFIG)
    assert isinstance(res, IndexConfig)


def test_collection_config_empty() -> None:
    """Test basic creation of the CollectionConfig model."""
    res = CollectionConfig()
    assert isinstance(res, CollectionConfig)


def test_collection_config_with_data() -> None:
    """Test creation of the CollectionConfig model with data."""
    res = CollectionConfig(**COLLECTION_CONFIG)
    assert isinstance(res, CollectionConfig)


def test_db_config_empty() -> None:
    """Test basic creation of the DBConfig model."""
    res = DBConfig()
    assert isinstance(res, DBConfig)


def test_db_config_with_data() -> None:
    """Test creation of the DBConfig model with data."""
    res = DBConfig(**DB_CONFIG)
    assert isinstance(res, DBConfig)


def test_mongo_config_empty() -> None:
    """Test basic creation of the MongoConfig model."""
    res = MongoConfig()
    assert isinstance(res, MongoConfig)


def test_mongo_config_with_data() -> None:
    """Test creation of the MongoConfig model with data."""
    res = MongoConfig(**MONGO_CONFIG)
    assert isinstance(res, MongoConfig)


def test_spec_config() -> None:
    """Test creation of the SpecConfig model with valid data."""
    res = SpecConfig(**SPEC_CONFIG)
    assert res.path == [Path(SPEC_CONFIG["path"])]


def test_spec_config_rel() -> None:
    """Test creation of the SpecConfig model with relative file path."""
    res = SpecConfig(**SPEC_CONFIG_REL)
    assert res.path == [Path(SPEC_CONFIG_REL["path"]).resolve()]


def test_spec_config_list() -> None:
    """Test creation of SpecConfig model with list of paths."""
    res = SpecConfig(**SPEC_CONFIG_LIST)
    assert all(Path(path).is_absolute() for path in res.path)  # type: ignore[union-attr]  # validated to a list
    assert res.path[1] == Path("/my/abs/path2")  # type: ignore[index]  # validated to a list


def test_spec_config_no_path() -> None:
    """Test creation of the SpecConfig model without path."""
    with pytest.raises(ValidationError):
        SpecConfig()  # type: ignore[call-arg]  # invalid on purpose


def test_spec_config_path_out_removed() -> None:
    """Test creation of the SpecConfig model with removed `path_out`."""
    with pytest.raises(ValidationError):
        SpecConfig(path=PATH, path_out="/my/abs/out/path")  # type: ignore[call-arg,arg-type]  # invalid on purpose


def test_SpecConfig_merge() -> None:
    """Test SpecConfig instantiation; multiple config files."""
    res = SpecConfig(path=[PATH, PATH_ADDITION])  # type: ignore[list-item]  # coerced by Pydantic
    assert res.path == [Path(PATH), Path(PATH_ADDITION)]


def test_SpecConfig_extra_arg() -> None:
    """Test SpecConfig instantiation; extra argument."""
    with pytest.raises(ValidationError):
        SpecConfig(non_existing=PATH)  # type: ignore[call-arg]  # invalid on purpose


def test_SpecConfig_patches(tmp_path: Path) -> None:
    """Test SpecConfig instantiation; patch files and inline patches.

    Relative paths of patch files are resolved like those of `path`.
    """
    inline = {"info": {"title": "Patched"}}
    res = SpecConfig.model_validate(
        {"path": PATH, "patches": ["patch.yaml", inline, "/abs/patch.json"]},
        context={"base_dir": tmp_path},
    )
    assert res.patches == [tmp_path / "patch.yaml", inline, Path("/abs/patch.json")]
    assert SpecConfig(path=PATH).patches is None  # type: ignore[arg-type]  # coerced by Pydantic


@pytest.mark.parametrize("patches", [[1], ["a.yaml", [{"a": 1}]], {"a": 1}])
def test_SpecConfig_patches_invalid(patches: Any) -> None:
    """Test SpecConfig instantiation; patches neither paths nor mappings."""
    with pytest.raises(ValidationError):
        SpecConfig.model_validate({"path": PATH, "patches": patches})


def test_APIConfig_named_specs() -> None:
    """Test APIConfig instantiation; specs given as named entries.

    Names are set on the specs, in order; removed entries (`None`) are
    dropped.
    """
    res = APIConfig.model_validate(
        {
            "specs": {
                "first": {"path": PATH},
                "removed": None,
                "second": SpecConfig(path=PATH_ADDITION),  # type: ignore[arg-type]  # coerced by Pydantic
                "third": {"path": PATH, "name": "third"},
            }
        }
    )
    assert [spec.name for spec in res.specs] == ["first", "second", "third"]
    assert res.specs[1].path == [Path(PATH_ADDITION)]


def test_APIConfig_list_specs() -> None:
    """Test APIConfig instantiation; specs given as a list, names optional."""
    res = APIConfig.model_validate(
        {"specs": [{"path": PATH}, {"path": PATH, "name": "named"}]}
    )
    assert [spec.name for spec in res.specs] == [None, "named"]


@pytest.mark.parametrize(
    "specs,message",
    [
        # spec without `path`, e.g., a misspelled name in an overlay
        ({"unknown": {"patches": [{"a": 1}]}}, "API spec 'unknown' has no 'path'"),
        # name differing from the key
        ({"first": {"path": PATH, "name": "other"}}, "different 'name'"),
        (
            {"first": SpecConfig(path=PATH, name="other")},  # type: ignore[arg-type]  # coerced by Pydantic
            "different 'name'",
        ),
        # entries that are not specs
        ({"first": PATH}, "valid dictionary"),
        # empty names
        ({"": {"path": PATH}}, "non-empty strings"),
        ({1: {"path": PATH}}, "non-empty strings"),
        ([{"path": PATH, "name": ""}], "at least 1 character"),
        # duplicate names
        ([{"path": PATH, "name": "a"}, {"path": PATH, "name": "a"}], "unique"),
    ],
)
def test_APIConfig_named_specs_invalid(specs: Any, message: str) -> None:
    """Test APIConfig instantiation; invalid named specs."""
    with pytest.raises(ValidationError, match=message):
        APIConfig.model_validate({"specs": specs})


def test_AccessControlConfig_named_policies() -> None:
    """Test AccessControlConfig instantiation; named policy rules.

    Rules are converted to a list, keeping their names; removed rules
    (`None`) are dropped.
    """
    res = AccessControlConfig.model_validate(
        {
            "policies": {
                "read": ["p", "*", "/pets", "GET"],
                "removed": None,
                "admin": ["g", "user:bob", "admin"],
            }
        }
    )
    assert res.policies == [["p", "*", "/pets", "GET"], ["g", "user:bob", "admin"]]
    assert res.named_policies == {
        "read": ["p", "*", "/pets", "GET"],
        "admin": ["g", "user:bob", "admin"],
    }
    # names are kept on revalidation of the instance, e.g., in a parent model
    assert AccessControlConfig.model_validate(res).named_policies == (
        res.named_policies
    )
    # names no longer apply if rules are added or removed after validation
    changed = res.model_copy(update={"policies": res.policies[:1]})
    assert changed.named_policies is None
    reordered = res.model_copy(update={"policies": res.policies[::-1]})
    assert reordered.named_policies is None
    # the names are not changed via the returned mapping
    res.named_policies["read"].append("x")
    assert res.named_policies == {
        "read": ["p", "*", "/pets", "GET"],
        "admin": ["g", "user:bob", "admin"],
    }
    # listed rules have no names
    assert (
        AccessControlConfig(policies=[["p", "*", "/a", "GET"]]).named_policies is None
    )
    assert AccessControlConfig().named_policies is None


@pytest.mark.parametrize(
    "policies",
    [
        {"read": "p, *, /pets, GET"},
        {"read": [["p", "*", "/pets", "GET"]]},
        {1: ["p", "*", "/pets", "GET"]},
        [{"read": ["p", "*", "/pets", "GET"]}],
        {"": ["p", "*", "/pets", "GET"]},
    ],
)
def test_AccessControlConfig_named_policies_invalid(policies: Any) -> None:
    """Test AccessControlConfig instantiation; invalid named policy rules."""
    with pytest.raises(ValidationError):
        AccessControlConfig.model_validate({"policies": policies})


@pytest.mark.parametrize("prefix", ["user:", "u/", "user:group:", "groups:"])
def test_AccessControlConfig_subject_prefix(prefix: str) -> None:
    """Test AccessControlConfig instantiation; valid subject prefixes."""
    res = AccessControlConfig(subject_prefix=prefix)
    assert res.subject_prefix == prefix


@pytest.mark.parametrize(
    "prefix", ["", "group:", "group:x", "g", "group", "client:", "client:x", "c"]
)
def test_AccessControlConfig_subject_prefix_invalid(prefix: str) -> None:
    """Test AccessControlConfig instantiation; invalid subject prefixes.

    These are subject prefixes that may collide with role names or group
    subjects.
    """
    with pytest.raises(ValidationError):
        AccessControlConfig(subject_prefix=prefix)


@pytest.mark.parametrize("mapping", ["group_mapping", "client_mapping"])
@pytest.mark.parametrize("role", ["user:alice", "group:admins", "client:conquest"])
def test_AccessControlConfig_mapped_roles_invalid(mapping: str, role: str) -> None:
    """Test AccessControlConfig instantiation; mapped roles that are subjects."""
    with pytest.raises(ValidationError, match="must not start with"):
        AccessControlConfig.model_validate({mapping: {"x": role}})


@pytest.mark.parametrize("mapping", ["group_mapping", "client_mapping"])
def test_AccessControlConfig_mapped_roles(mapping: str) -> None:
    """Test AccessControlConfig instantiation; mapped roles."""
    res = AccessControlConfig.model_validate({mapping: {"x": "admin"}})
    assert getattr(res, mapping) == {"x": "admin"}


@pytest.mark.parametrize("mapping", ["group_mapping", "client_mapping"])
@pytest.mark.parametrize(
    "role,resource_roles",
    [
        ("owner", None),
        ("maintainer", None),
        ("reader", ["owner", "reader"]),
    ],
)
def test_AccessControlConfig_mapped_resource_roles_invalid(
    mapping: str, role: str, resource_roles: list[str] | None
) -> None:
    """Test AccessControlConfig instantiation; mapped resource roles.

    A mapped role is a subject of its own, which would have the role on every
    resource.
    """
    data: dict[str, Any] = {mapping: {"x": role}}
    if resource_roles is not None:
        data["resource_roles"] = resource_roles
    with pytest.raises(ValidationError, match=f"must not map to '{role}'"):
        AccessControlConfig.model_validate(data)


@pytest.mark.parametrize("mapping", ["group_mapping", "client_mapping"])
def test_AccessControlConfig_mapped_other_roles(mapping: str) -> None:
    """Test AccessControlConfig instantiation; mapped non-resource roles.

    Roles that are resource roles by default may be mapped if they are not
    configured as such.
    """
    res = AccessControlConfig.model_validate(
        {mapping: {"x": "maintainer"}, "resource_roles": ["owner"]}
    )
    assert getattr(res, mapping) == {"x": "maintainer"}


@pytest.mark.parametrize(
    "charset,length",
    [("abcdef", 8), ("0123456789", 6), ("0123456789", 64), ("a-._~", 9)],
)
def test_AccessControlConfig_ids(charset: str, length: int) -> None:
    """Test AccessControlConfig instantiation; valid identifier settings."""
    res = AccessControlConfig(id_charset=charset, id_length=length)
    assert (res.id_charset, res.id_length) == (charset, length)


@pytest.mark.parametrize(
    "charset,length",
    [
        ("", 6),
        ("ab/", 6),
        ("a b", 6),
        ("äb", 6),
        ("ab", 3),
        ("ab", 65),
        # too few different identifiers
        ("a", 64),
        ("aaaaaaaaaab", 19),
        ("0123456789", 5),
    ],
)
def test_AccessControlConfig_ids_invalid(charset: str, length: int) -> None:
    """Test AccessControlConfig instantiation; invalid identifier settings."""
    with pytest.raises(ValidationError):
        AccessControlConfig(id_charset=charset, id_length=length)


ISSUERS = ["https://login.example.org/oidc/"]


@pytest.mark.parametrize(
    "issuers,access_control",
    [
        # access control options without access control
        (ISSUERS, {"enforce": True}),
        (ISSUERS, {"api_specs": "specs.yaml"}),
        (ISSUERS, {"self_service": True}),
        (ISSUERS, {"policies": [["p", "*", "/pets", "GET"]]}),
        (ISSUERS, {"require_admin": True}),
        # access control without trusted issuers
        ([], {"enabled": True}),
        ([], {"enabled": True, "enforce": True}),
    ],
)
def test_SecurityConfig_access_control_invalid(
    issuers: list[str], access_control: dict[str, Any]
) -> None:
    """Test SecurityConfig instantiation; inconsistent access control."""
    with pytest.raises(ValidationError):
        SecurityConfig(
            auth=AuthConfig(issuers=issuers, audience=["my-app"]),
            access_control=AccessControlConfig(**access_control),
        )


@pytest.mark.parametrize(
    "issuers,access_control",
    [
        ([], {}),
        (ISSUERS, {}),
        (ISSUERS, {"enabled": True}),
        (
            ISSUERS,
            {
                "enabled": True,
                "enforce": True,
                "api_specs": "specs.yaml",
                "self_service": True,
                "policies": [["p", "*", "/pets", "GET"]],
            },
        ),
    ],
)
def test_SecurityConfig_access_control(
    issuers: list[str], access_control: dict[str, Any]
) -> None:
    """Test SecurityConfig instantiation; consistent access control."""
    res = SecurityConfig(
        auth=AuthConfig(issuers=issuers, audience=["my-app"]),
        access_control=AccessControlConfig(**access_control),
    )
    assert res.access_control.enabled is access_control.get("enabled", False)


@pytest.mark.parametrize(
    "service_clients",
    [
        [{"client_id": "conquest"}, {"client_id": "conquest", "subject": "x"}],
        [{"client_id": ""}],
        [{"client_id": "conquest", "subject": ""}],
        [{"client_id": "conquest", "extra": True}],
    ],
)
def test_AuthConfig_service_clients_invalid(
    service_clients: list[dict[str, Any]],
) -> None:
    """Test AuthConfig instantiation; duplicate, empty or unknown fields."""
    with pytest.raises(ValidationError):
        AuthConfig.model_validate({"service_clients": service_clients})


def test_AuthConfig_service_clients() -> None:
    """Test AuthConfig instantiation; service clients with defaults."""
    res = AuthConfig.model_validate(
        {
            "service_clients": [
                {"client_id": "conquest"},
                {"client_id": "protes", "subject": "uuid-1", "on_behalf_of": True},
            ],
            "on_behalf_of_header": "X-On-Behalf-Of",
        }
    )
    assert [client.subject for client in res.service_clients] == [None, "uuid-1"]
    assert [client.on_behalf_of for client in res.service_clients] == [False, True]


@pytest.mark.parametrize(
    "issuers,issuer",
    [
        # required with several trusted issuers
        (["https://a.example.org", "https://b.example.org"], None),
        # must be trusted
        (["https://a.example.org", "https://b.example.org"], "https://c.example.org"),
        (["https://a.example.org"], "https://b.example.org"),
        ([], "https://a.example.org"),
        # must not be empty
        (["https://a.example.org"], ""),
    ],
)
def test_AuthConfig_service_client_issuers_invalid(
    issuers: list[str], issuer: str | None
) -> None:
    """Test AuthConfig instantiation; missing or untrusted client issuers."""
    with pytest.raises(ValidationError, match="issuer"):
        AuthConfig.model_validate(
            {
                "issuers": issuers,
                "audience": ["foca"],
                "service_clients": [{"client_id": "conquest", "issuer": issuer}],
            }
        )


@pytest.mark.parametrize(
    "issuers,issuer",
    [
        (["https://a.example.org", "https://b.example.org"], "https://b.example.org"),
        # implied with one trusted issuer, given twice
        (["https://a.example.org", "https://a.example.org"], None),
        (["https://a.example.org"], None),
        (["https://a.example.org"], "https://a.example.org"),
        ([], None),
    ],
)
def test_AuthConfig_service_client_issuers(
    issuers: list[str], issuer: str | None
) -> None:
    """Test AuthConfig instantiation; issuers of service clients."""
    res = AuthConfig.model_validate(
        {
            "issuers": issuers,
            "audience": ["foca"],
            "service_clients": [{"client_id": "conquest", "issuer": issuer}],
        }
    )
    assert res.service_clients[0].issuer == issuer


def test_AuthConfig_service_client_issuers_one_missing() -> None:
    """Test AuthConfig instantiation; one of several clients lacks an issuer."""
    with pytest.raises(ValidationError, match="'protes' needs an issuer"):
        AuthConfig.model_validate(
            {
                "issuers": ["https://a.example.org", "https://b.example.org"],
                "audience": ["foca"],
                "service_clients": [
                    {"client_id": "conquest", "issuer": "https://a.example.org"},
                    {"client_id": "protes"},
                ],
            }
        )


def test_AuthConfig_service_clients_not_accepted() -> None:
    """Test AuthConfig instantiation; service client not an accepted client."""
    with pytest.raises(ValidationError, match="conquest"):
        AuthConfig.model_validate(
            {
                "clients": ["dashboard"],
                "service_clients": [{"client_id": "conquest"}],
            }
        )


def test_AuthConfig_service_clients_accepted() -> None:
    """Test AuthConfig instantiation; service client among accepted clients."""
    res = AuthConfig.model_validate(
        {
            "clients": ["dashboard", "conquest"],
            "service_clients": [{"client_id": "conquest"}],
        }
    )
    assert res.service_clients[0].client_id == "conquest"


@pytest.mark.parametrize("claims", [["client_id"], ["eduperson_entitlement", "azp"]])
def test_AuthConfig_userinfo_claims_invalid(claims: list[str]) -> None:
    """Test AuthConfig instantiation; client claims from user info."""
    with pytest.raises(ValidationError, match="identifying the client"):
        AuthConfig(userinfo_claims=claims)


@pytest.mark.parametrize(
    "service_clients,access_control",
    [
        # client mapping requires access control
        ([{"client_id": "conquest"}], {"client_mapping": {"conquest": "submitter"}}),
        # mapped clients must be service clients
        ([], {"enabled": True, "client_mapping": {"conquest": "submitter"}}),
        (
            [{"client_id": "protes"}],
            {"enabled": True, "client_mapping": {"conquest": "submitter"}},
        ),
    ],
)
def test_SecurityConfig_client_mapping_invalid(
    service_clients: list[dict[str, Any]], access_control: dict[str, Any]
) -> None:
    """Test SecurityConfig instantiation; inconsistent client mapping."""
    with pytest.raises(ValidationError):
        SecurityConfig.model_validate(
            {
                "auth": {
                    "issuers": ISSUERS,
                    "audience": ["my-app"],
                    "service_clients": service_clients,
                },
                "access_control": access_control,
            }
        )


def test_SecurityConfig_client_mapping() -> None:
    """Test SecurityConfig instantiation; service clients mapped to roles."""
    res = SecurityConfig.model_validate(
        {
            "auth": {
                "issuers": ISSUERS,
                "audience": ["my-app"],
                "service_clients": [{"client_id": "conquest"}],
            },
            "access_control": {
                "enabled": True,
                "client_mapping": {"conquest": "submitter"},
            },
        }
    )
    assert res.access_control.client_mapping == {"conquest": "submitter"}


def test_AuthConfig_required_removed() -> None:
    """Test AuthConfig instantiation; `required` is no longer supported."""
    with pytest.raises(ValidationError):
        AuthConfig(required=True)  # type: ignore[call-arg]  # invalid on purpose


@pytest.mark.parametrize("log_traffic", [True, False])
def test_AccessControlConfig_log_traffic(log_traffic: bool) -> None:
    """Test AccessControlConfig instantiation; traffic logging.

    Traffic of the permissions API is logged by default.
    """
    assert AccessControlConfig().log_traffic is True
    res = AccessControlConfig(log_traffic=log_traffic)
    assert res.log_traffic is log_traffic


def test_AccessControlConfig_default_model() -> None:
    """Test AccessControlConfig instantiation; default model."""
    model = Path(AccessControlConfig().model)  # type: ignore[arg-type]  # default model is set
    assert model.name == "default_model.conf"
    assert model.is_file()


@pytest.mark.parametrize("path", ["access_model.conf", "../../test/access_model.conf"])
def test_AccessControlConfig_relative_model(path: str) -> None:
    """Test AccessControlConfig instantiation; relative model paths.

    Relative model paths are resolved against the working directory without
    config file.
    """
    assert AccessControlConfig(model=path).model == str(Path(path).resolve())


def test_AccessControlConfig_absolute_model() -> None:
    """Test AccessControlConfig instantiation; absolute model path."""
    path = "/test/access_model.conf"
    assert AccessControlConfig(model=path).model == path


RUNTIME_FIELDS = f"{FIELDS}.runtime_fields"


def _no_version(name: str) -> NoReturn:
    raise AssertionError(f"version of '{name}' looked up")


def test_ServiceInfoConfig(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test ServiceInfoConfig instantiation; configured version."""
    monkeypatch.setattr(foca.utils.service_info, "version", _no_version)
    res = ServiceInfoConfig(service=SERVICE, package="my-app")
    assert res.service == SERVICE
    assert res.package_version is None
    assert res.runtime_function is None
    assert res.log_traffic is True


def test_ServiceInfoConfig_log_traffic_disabled() -> None:
    """Test ServiceInfoConfig instantiation; traffic logging disabled."""
    res = ServiceInfoConfig(service=SERVICE, log_traffic=False)
    assert res.log_traffic is False


@pytest.mark.parametrize("configured", [service(version=REMOVE), service(version=None)])
def test_ServiceInfoConfig_package(
    monkeypatch: pytest.MonkeyPatch, configured: dict[str, Any]
) -> None:
    """Test ServiceInfoConfig instantiation; version from package.

    This also holds if `version` is `None` (e.g., in a config overlay).
    """
    monkeypatch.setattr(foca.utils.service_info, "version", lambda name: "2.0")
    res = ServiceInfoConfig(service=configured, package="my-app")
    assert res.package_version == "2.0"


def test_ServiceInfoConfig_package_not_installed() -> None:
    """Test ServiceInfoConfig instantiation; package not installed."""
    with pytest.raises(
        ValidationError, match=r"Package 'not-installed-app' .* not installed"
    ):
        ServiceInfoConfig(service=service(version=REMOVE), package="not-installed-app")


def test_ServiceInfoConfig_runtime_fields() -> None:
    """Test ServiceInfoConfig instantiation; runtime fields.

    Required fields may be computed at runtime.
    """
    res = ServiceInfoConfig(service=service(name=REMOVE), runtime_fields=RUNTIME_FIELDS)
    assert res.runtime_function is not None
    assert res.runtime_function.__name__ == "runtime_fields"


def test_ServiceInfoConfig_runtime_fields_invalid() -> None:
    """Test ServiceInfoConfig instantiation; invalid runtime fields.

    The runtime fields function cannot be imported.
    """
    with pytest.raises(
        ValidationError,
        match=(
            r"Runtime fields function 'not_a_module\.f' "
            r"\(`service_info\.runtime_fields`\) could not be imported"
        ),
    ):
        ServiceInfoConfig(service=SERVICE, runtime_fields="not_a_module.f")


@pytest.mark.parametrize(
    "configured,runtime_fields,match",
    [
        (
            service(name=REMOVE),
            None,
            r"`service_info\.service`: 'name' is a required property",
        ),
        (
            service(name=None),
            None,
            r"`service_info\.service`: 'name' is a required property",
        ),
        (
            {**SERVICE, "name": 1},
            RUNTIME_FIELDS,
            r"`service_info\.service\.name`: 1 is not of type 'string'",
        ),
        (
            {**SERVICE, "organization": {"name": "x", "url": "x"}},
            None,
            r"`service_info\.service\.organization\.url`: 'x' is not a 'uri'",
        ),
        (
            {**SERVICE, "x-date": date(2026, 10, 20)},
            None,
            r"`service_info\.service`: not JSON-serializable",
        ),
    ],
)
def test_ServiceInfoConfig_invalid(
    configured: dict[str, Any], runtime_fields: str | None, match: str
) -> None:
    """Test ServiceInfoConfig instantiation; invalid service info.

    `None` values remove fields.
    """
    with pytest.raises(ValidationError, match=match):
        ServiceInfoConfig(service=configured, runtime_fields=runtime_fields)


@pytest.mark.parametrize("data", [{}, {"service": SERVICE, "x": 1}])
def test_ServiceInfoConfig_fields_invalid(data: dict[str, Any]) -> None:
    """Test ServiceInfoConfig instantiation; invalid keys.

    Service info is missing or keys are unknown.
    """
    with pytest.raises(ValidationError):
        ServiceInfoConfig(**data)


def test_ServiceInfoConfig_template() -> None:
    """Test ServiceInfoConfig instantiation; config template.

    The section of the config template validates as is.
    """
    template = Path(__file__).parents[2] / "templates" / "config.yaml"
    data = yaml.safe_load(template.read_text(encoding="utf-8"))
    res = ServiceInfoConfig(**data["service_info"])
    assert res.package is None
    assert res.runtime_fields is None


def test_Config_service_info() -> None:
    """Test Config instantiation; service info section."""
    assert Config().service_info is None
    res = Config.model_validate({"service_info": {"service": SERVICE}})
    assert isinstance(res.service_info, ServiceInfoConfig)


def test_docstring_examples() -> None:
    """Test that the examples in the docstrings of the config models run."""
    results = doctest.testmod(foca.models.config)
    assert results.attempted > 0
    assert results.failed == 0
