"""Casbin enforcers cached per app instance.

Loading all policy rules for each request makes requests slower the more
rules are stored (e.g., with roles on many resources). Instead, each app
instance (i.e., each worker process) keeps one enforcer, shared by its
threads, and checks before each use whether stored rules changed, via the
changes recorded by the adapter (cf.
:py:class:`foca.security.access_control.foca_casbin_adapter.adapter.PolicyChanges`).
Changed rules are loaded again and applied to the enforcer; all rules are
loaded again only if the changes cannot be applied, e.g., because more
changes were made since the last check than are recorded.
"""

import logging
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from casbin import Enforcer
from casbin.model import Assertion, Model
from casbin.model.policy_op import PolicyOp
from casbin.rbac.default_role_manager import DomainManager, RoleManager
from casbin.rbac.default_role_manager.role_manager import (
    DomainManagerBase,
    Link,
    match_error_handler,
)

from foca.security.access_control.foca_casbin_adapter.adapter import (
    Adapter,
    PolicyChanges,
)
from foca.utils.logging import escape_log_value

logger = logging.getLogger(__name__)

# Policy effect of models whose rules Casbin sorts by subject hierarchy
_SUBJECT_PRIORITY_EFFECT = "subjectPriority(p_eft) || deny"

_UPDATE_NOT_SUPPORTED = (
    "Updating policy rules is not supported; remove and add rules instead"
)


def is_within_path(request_path: str, resource_path: str) -> bool:
    """Check whether a request path is a resource path or a sub-path.

    Args:
        request_path: Request path.
        resource_path: Resource path a role was granted on. Trailing
            slashes are ignored. The root path `/` only matches itself.

    Returns:
        `True` if `request_path` equals `resource_path` or starts with
        `resource_path` followed by `/`.
    """
    resource_path = resource_path.rstrip("/")
    if not resource_path:
        return request_path == "/"
    return request_path == resource_path or request_path.startswith(f"{resource_path}/")


def get_resource_role_ptypes(model: Model) -> list[str]:
    """Get policy types of resource-scoped role definitions of a model.

    Args:
        model: Casbin model.

    Returns:
        Policy types of role definitions with three fields (subject, role,
        resource), e.g., `g2` for `g2 = _, _, _`.
    """
    return [
        ptype
        for ptype, assertion in model.model.get("g", {}).items()
        if assertion.value.count("_") == 3
    ]


class SharedEnforcer(Enforcer):
    """Casbin enforcer shared by the threads of an app instance.

    Casbin updates rules (e.g., via `update_policy()`) in memory before
    storing them, and FOCA's adapter does not support updating rules, so
    that a shared enforcer would keep rules that are not stored. Updating
    rules is therefore refused before changing the enforcer; remove and add
    rules instead.
    """

    def _update_policy(self, *args: Any, **kwargs: Any) -> bool:
        """Refuse to update a rule.

        Args:
            *args: Arguments of Casbin's method.
            **kwargs: Keyword arguments of Casbin's method.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(_UPDATE_NOT_SUPPORTED)

    def _update_policies(self, *args: Any, **kwargs: Any) -> bool:
        """Refuse to update rules.

        Args:
            *args: Arguments of Casbin's method.
            **kwargs: Keyword arguments of Casbin's method.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(_UPDATE_NOT_SUPPORTED)

    def _update_filtered_policies(self, *args: Any, **kwargs: Any) -> bool:
        """Refuse to update rules.

        Args:
            *args: Arguments of Casbin's method.
            **kwargs: Keyword arguments of Casbin's method.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(_UPDATE_NOT_SUPPORTED)


class ResourceRoleManager(DomainManager):
    """Role manager of role definitions with domains, e.g., resource roles.

    Unlike Casbin's `DomainManager`, role managers of single domains are not
    cached, so that the memory of a long-lived enforcer does not grow with
    each domain (e.g., request path) checked. With
    :py:func:`is_within_path` as domain matching function, the domains
    matching a request path are looked up via the path's prefixes, rather
    than by checking every domain. Links are also indexed by subject, so
    that the rules of a subject or resource can be listed without checking
    every rule (cf. :py:meth:`get_rules`).

    The class extends internals of Casbin's `DomainManager` (e.g., its
    `add_link()` and its role manager map), which is why FOCA pins Casbin's
    minor version; tests check that it links roles as `DomainManager` does.

    Args:
        max_hierarchy_level: Maximum depth of role inheritance.
    """

    def __init__(self, max_hierarchy_level: int = 10) -> None:
        """Create role manager."""
        super().__init__(max_hierarchy_level)
        # Domains with links, by domain without trailing slashes
        self._domains_by_path: dict[str, set[str]] = {}
        # Domains with links of a subject, by subject, in the order added
        self._domains_by_subject: dict[str, dict[str, None]] = {}

    def clear(self) -> None:
        """Remove all links."""
        super().clear()
        self._domains_by_path = {}
        self._domains_by_subject = {}

    def add_link(self, name1: str, name2: str, *domain: str) -> None:
        """Add a link: `name1` has role `name2` in a domain.

        Args:
            name1: Subject or role.
            name2: Role.
            domain: Domain of the link, e.g., a resource path.
        """
        DomainManagerBase.add_link(self, name1, name2, *domain)
        key = self._get_domain(*domain)
        self._domains_by_path.setdefault(key.rstrip("/"), set()).add(key)
        self._domains_by_subject.setdefault(name1, {})[key] = None

    def delete_link(self, name1: str, name2: str, *domain: str) -> None:
        """Delete a link.

        Args:
            name1: Subject or role.
            name2: Role.
            domain: Domain of the link.

        Raises:
            RuntimeError: The link does not exist (as for `DomainManager`).
        """
        key = self._get_domain(*domain)
        links = self.all_links.get(key, [])
        if Link(name1, name2) not in links:
            raise RuntimeError(
                f"error: link between {name1} and {name2} does not exist"
            )
        links.remove(Link(name1, name2))
        if all(link.user != name1 for link in links):
            domains = self._domains_by_subject[name1]
            del domains[key]
            if not domains:
                del self._domains_by_subject[name1]
        if not links:
            del self.all_links[key]
            keys = self._domains_by_path[key.rstrip("/")]
            keys.discard(key)
            if not keys:
                del self._domains_by_path[key.rstrip("/")]

    def get_rules(self, field_index: int, *field_values: str) -> list[list[str]] | None:
        """Get the rules of the links that match a filter.

        Answers the filters used by FOCA's resource role helpers from the
        indexes, as Casbin's `get_filtered_named_grouping_policy()` checks
        every rule.

        Args:
            field_index: Index of the value that `field_values` start at, as
                for `get_filtered_named_grouping_policy()`.
            field_values: Values the rules have, starting at `field_index`.

        Returns:
            Subject, role and domain of each link that matches the filter,
            or `None` if the filter is not supported, so that rules need to
            be checked one by one. Supported are filters by subject,
            starting at index 0, by domain, at index 2, and by role and
            domain, at index 1, without empty values (which Casbin's filters
            treat as wildcards).
        """
        end = field_index + len(field_values)
        if "" in field_values:
            return None
        if field_index == 0 and 1 <= end <= 3:
            subject = field_values[0]
            domains = self._domains_by_subject.get(subject, {})
            if end == 3:
                domains = {field_values[2]: None} if field_values[2] in domains else {}
            return [
                [subject, link.role, domain]
                for domain in domains
                for link in self.all_links[domain]
                if link.user == subject and (end == 1 or link.role == field_values[1])
            ]
        if end == 3 and field_index in (1, 2):
            domain = field_values[-1]
            return [
                [link.user, link.role, domain]
                for link in self.all_links.get(domain, [])
                if field_index == 2 or link.role == field_values[0]
            ]
        return None

    def _get_role_manager(self, *domain: str) -> RoleManager:
        """Get a role manager of the links that apply in a domain.

        Args:
            domain: Domain, e.g., a request path.

        Returns:
            Role manager with the links of the domain and, if a domain
            matching function is set, of the domains matching it.
        """
        domain1 = self._get_domain(*domain)
        links = list(self.all_links.get(domain1, []))
        for domain2 in self._get_matching_domains(domain1):
            if domain2 != domain1:
                links.extend(self.all_links.get(domain2, []))
        role_manager = RoleManager(max_hierarchy_level=self.max_hierarchy_level)
        role_manager.add_matching_func(self.matching_func)
        for link in links:
            role_manager.add_link(link.user, link.role)
        return role_manager

    def _get_matching_domains(self, domain: str) -> Iterable[str]:
        """Get the domains with links that match a domain.

        Args:
            domain: Domain, e.g., a request path.

        Returns:
            Domains for which the domain matching function, if any, is true.
        """
        if self.domain_matching_func is None:
            return []
        if self.domain_matching_func is not is_within_path:
            return [
                other
                for other in list(self.all_links)
                if match_error_handler(self.domain_matching_func, domain, other)
            ]
        # The path itself and its prefixes followed by `/`, without trailing
        # slashes; the root path only matches itself
        paths = {
            domain[:index] for index in range(1, len(domain)) if domain[index] == "/"
        }
        paths.add(domain)
        paths.discard("")
        if domain == "/":
            paths.add("")
        return [
            other for path in paths for other in self._domains_by_path.get(path, ())
        ]


class EnforcerCache:
    """Casbin enforcer of an app instance, with current policies.

    The enforcer is shared by the threads of the app instance; hold `lock`
    while using it, as it is changed when stored rules change (cf.
    :py:meth:`get_enforcer`). Before each use, the version of the changes
    recorded by the adapter is read (one small read); if it changed, the
    changed rules are loaded and applied. A rule granted or revoked via the
    adapter (e.g., via FOCA's helpers or the permissions API) thus applies
    from the next use of the enforcer, in any app instance.

    Args:
        model: Path to the Casbin model file.
        adapter: Adapter of the stored rules.
        resource_roles_cover_subpaths: Whether resource-scoped roles also
            apply to sub-paths of the resource they were granted on (cf.
            :py:func:`is_within_path`).

    Attributes:
        model: Path to the Casbin model file.
        adapter: Adapter of the stored rules.
        resource_roles_cover_subpaths: Whether resource-scoped roles apply
            to sub-paths.
        lock: Lock to hold while using the enforcer.
    """

    def __init__(
        self, model: str, adapter: Adapter, resource_roles_cover_subpaths: bool
    ) -> None:
        """Create cache; the enforcer is created when first used."""
        self.model = model
        self.adapter = adapter
        self.resource_roles_cover_subpaths = resource_roles_cover_subpaths
        self.lock = threading.RLock()
        self._enforcer: Enforcer | None = None
        self._epoch: str | None = None
        self._version = 0

    def is_for(
        self, model: str, adapter: Adapter, resource_roles_cover_subpaths: bool
    ) -> bool:
        """Check whether the cache is for a model, adapter and setting.

        Args:
            model: Path to the Casbin model file.
            adapter: Adapter of the stored rules.
            resource_roles_cover_subpaths: Whether resource-scoped roles apply
                to sub-paths.

        Returns:
            Whether the cache was created with these arguments.
        """
        return (
            self.model == model
            and self.adapter is adapter
            and self.resource_roles_cover_subpaths == resource_roles_cover_subpaths
        )

    def get_enforcer(self) -> Enforcer:
        """Get the enforcer, with the stored rules in effect.

        Rules changed since the last call are applied first; all rules are
        loaded on the first call, and if the changes cannot be applied.

        Returns:
            Casbin enforcer, shared by all callers; hold `lock` while using
            it.

        Raises:
            pymongo.errors.PyMongoError: The stored rules or their changes
                could not be read.
        """
        with self.lock:
            state = self.adapter.get_changes(with_changes=False)
            if self._enforcer is not None and (state.epoch, state.version) == (
                self._epoch,
                self._version,
            ):
                return self._enforcer
            if self._enforcer is not None and self._apply(
                self._enforcer, self.adapter.get_changes()
            ):
                return self._enforcer
            return self._load()

    def _load(self) -> Enforcer:
        """Create an enforcer and load all stored rules in effect.

        Returns:
            Casbin enforcer.
        """
        # Read before loading, so that changes made while loading are
        # applied on the next call
        state = self.adapter.get_changes(with_changes=False)
        self._enforcer = None
        # Without adapter, Casbin neither loads rules nor logs them (it logs
        # all rules, including subjects, when loading them on creation)
        enforcer = SharedEnforcer(self.model)
        for ptype, role_manager in list(enforcer.rm_map.items()):
            if type(role_manager) is DomainManager:
                enforcer.set_named_role_manager(
                    ptype, ResourceRoleManager(role_manager.max_hierarchy_level)
                )
        if self.resource_roles_cover_subpaths:
            for ptype in get_resource_role_ptypes(enforcer.get_model()):
                enforcer.add_named_domain_matching_func(ptype, is_within_path)
        enforcer.set_adapter(self.adapter)
        enforcer.load_policy()
        self._enforcer, self._epoch, self._version = (
            enforcer,
            state.epoch,
            state.version,
        )
        logger.debug("Policy rules loaded (changes version %s)", _describe(state))
        return enforcer

    def _apply(self, enforcer: Enforcer, changes: PolicyChanges) -> bool:
        """Apply recorded changes to the enforcer.

        Args:
            enforcer: Casbin enforcer with the rules as of the last call.
            changes: Recorded changes.

        Returns:
            Whether the changes since the last call were applied; `False` if
            they cannot be applied, e.g., because they are not all recorded
            anymore, because the document recording them was re-created
            since, or because they changed all rules, so that all rules need
            to be loaded again.
        """
        # If no changes were recorded when rules were loaded, the document
        # recording changes was created since, starting from version 0
        same_record = changes.epoch == self._epoch or self._epoch is None
        count = changes.version - self._version
        if not same_record or not 0 < count <= len(changes.changes):
            return False
        filters = _get_filters(changes.changes[len(changes.changes) - count :])
        model = enforcer.get_model()
        if filters is None or model["e"]["e"].value == _SUBJECT_PRIORITY_EFFECT:
            return False
        updates = []
        for ptype, field_index, values in filters:
            assertion = model.model.get(ptype[0], {}).get(ptype)
            # Rules of policy types not in the model are not loaded
            if assertion is None:
                continue
            # Casbin keeps rules of models with priorities sorted, and does
            # not update conditional role links incrementally
            if assertion.priority_index >= 0 or ptype in enforcer.cond_rm_map:
                return False
            stored = self.adapter.load_filtered_rules(
                model, ptype, field_index, *values
            )
            updates.append((ptype, assertion, field_index, values, stored))
        # Stored rules are loaded before changing the enforcer, so that it is
        # not left half updated if loading fails
        try:
            for update in updates:
                _update_rules(enforcer, *update)
        except BaseException:
            # Never use a half-updated enforcer: load all rules next time
            self._enforcer = None
            raise
        self._epoch, self._version = changes.epoch, changes.version
        logger.debug(
            "Policy rule changes applied (changes version %s)", _describe(changes)
        )
        return True


def _describe(changes: PolicyChanges) -> str:
    """Describe the version of recorded changes for logging.

    Args:
        changes: Recorded changes.

    Returns:
        Epoch and version, escaped (the epoch is read from the database).
    """
    return escape_log_value(f"{changes.epoch}:{changes.version}")


def _get_filters(
    changes: Sequence[Any],
) -> list[tuple[str, int, tuple[str, ...]]] | None:
    """Get the filters of the rules that recorded changes may have changed.

    Args:
        changes: Recorded changes (cf. :py:class:`PolicyChanges`).

    Returns:
        Policy type, index and values of each distinct filter, or `None` if
        a change applies to all rules or is malformed (e.g., edited in the
        database), so that all rules need to be loaded again.
    """
    filters: dict[tuple[str, int, tuple[str, ...]], None] = {}
    for change in changes:
        if not isinstance(change, dict):
            return None
        ptype = change.get("ptype")
        field_index = change.get("index")
        values = change.get("values")
        if (
            not isinstance(ptype, str)
            or not ptype
            or not isinstance(field_index, int)
            or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
        ):
            return None
        filters[(ptype, field_index, tuple(values))] = None
    return list(filters)


def _update_rules(
    enforcer: Enforcer,
    ptype: str,
    assertion: Assertion,
    field_index: int,
    values: tuple[str, ...],
    stored: list[list[str]],
) -> None:
    """Replace the rules of an enforcer that match a filter.

    Args:
        enforcer: Casbin enforcer.
        ptype: Policy type of the rules.
        assertion: Policy or role definition of the policy type.
        field_index: Index of the value that `values` start at.
        values: Values the rules have, starting at `field_index`.
        stored: Stored rules in effect that match the filter.
    """
    end = field_index + len(values)
    sec = ptype[0]
    role_manager = enforcer.rm_map.get(ptype) if sec == "g" else None
    current = None
    if isinstance(role_manager, ResourceRoleManager):
        current = role_manager.get_rules(field_index, *values)
    if current is None:
        current = [
            rule
            for rule in assertion.policy
            if len(rule) >= end and tuple(rule[field_index:end]) == values
        ]
    stored_keys = {tuple(rule) for rule in stored}
    current_keys = {tuple(rule) for rule in current}
    removed = [rule for rule in current if tuple(rule) not in stored_keys]
    added = [rule for rule in stored if tuple(rule) not in current_keys]
    model = enforcer.get_model()
    for rule in removed:
        model.remove_policy(sec, ptype, rule)
    for rule in added:
        model.add_policy(sec, ptype, rule)
    if role_manager is not None:
        if removed:
            model.build_incremental_role_links(
                role_manager, PolicyOp.Policy_remove, sec, ptype, removed
            )
        if added:
            model.build_incremental_role_links(
                role_manager, PolicyOp.Policy_add, sec, ptype, added
            )
