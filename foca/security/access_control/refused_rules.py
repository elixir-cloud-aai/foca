"""Policy rules that the permissions API refuses.

Apps can refuse policy types, roles and resource paths in rules written via the
permissions API (`access_control.api_refused_rules`), e.g., so that whoever may
manage rules can't grant themselves access to the resources of others.

Paths are compared as sets of request paths: a rule is refused if one of its
paths may apply to a request path that a refused path matches. Values of rules
are patterns of the model's matching functions, so they are read generously:
as `keyMatch2` patterns (`:name` matches a segment), with `*` matching any
characters (also as in `keyMatch`), `.` any character, other quantifiers and
`{...}` any characters, and values with groups, character classes, alternations,
escapes or anchors any path at all. Values that are resources of
resource-scoped roles also cover their sub-paths and custom methods. Reading
them generously refuses some rules that apply to no refused path, but no rule
that does.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from casbin.model import Model

from foca.security.access_control.constants import BASE_PATH_PLACEHOLDER
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    InvalidRuleError,
)

# Path parameter, as `keyMatch2` replaces it (`:` up to the next `/`)
_PARAMETER = re.compile(r":[^/]+")
# Characters of regular expressions that refused paths must not have
_REFUSED_PATH_FORBIDDEN = frozenset("()[]{}|\\^$+?")
# Characters of values that make them match any path, as the patterns
# they form can't be read character by character
_ANY_PATH_CHARS = frozenset("()[]|\\^$")
# Kinds of items of a path pattern
_LITERAL = "literal"  # the character of the item
_ANY_CHAR = "any-char"  # any character
_SEGMENT = "segment"  # one or more characters other than `/`
_ANY_CHARS = "any-chars"  # zero or more characters


@dataclass(frozen=True)
class _Item:
    """Item of a path pattern.

    Attributes:
        kind: Kind of item (`_LITERAL`, `_ANY_CHAR`, `_SEGMENT` or
            `_ANY_CHARS`).
        char: Character of literal items.
    """

    kind: str
    char: str = ""


_ANY_CHARS_ITEM = _Item(_ANY_CHARS)
# Labels of transitions: a character, any character, or any but `/`
_ANY = "any"
_NOT_SLASH = "not-slash"


def validate_refused_path(path: str) -> str:
    """Validate a refused path.

    Args:
        path: Path, as in `access_control.api_refused_rules.paths`.

    Returns:
        The path.

    Raises:
        ValueError: The path is not absolute (optionally after the placeholder
            `{base_path}` at its start), has the placeholder elsewhere, or has
            characters of regular expressions other than those of `keyMatch2`
            patterns (`*`, `:name`), commas or whitespace, or a `*` that
            doesn't follow `/` (which `keyMatch2` reads as a quantifier).
    """
    rest = path.removeprefix(BASE_PATH_PLACEHOLDER)
    if not (rest.startswith("/") or (rest == "" and path)):
        raise ValueError(
            f"Refused paths must be absolute, optionally after "
            f"'{BASE_PATH_PLACEHOLDER}': {path!r}"
        )
    if BASE_PATH_PLACEHOLDER in rest:
        raise ValueError(
            f"Refused paths may only start with '{BASE_PATH_PLACEHOLDER}': {path!r}"
        )
    if _REFUSED_PATH_FORBIDDEN & set(rest) or re.search(r"[\s,]", rest):
        raise ValueError(
            "Refused paths are 'keyMatch2' patterns ('*' and ':name') without "
            f"other characters of regular expressions, commas or whitespace: {path!r}"
        )
    if re.search(r"(?<!/)\*", rest):
        raise ValueError(
            f"Refused paths may only have '*' after '/' (e.g., '/tasks/*'): {path!r}"
        )
    return path


def expand_refused_paths(paths: Iterable[str], base_paths: Sequence[str]) -> list[str]:
    """Replace the placeholder `{base_path}` in refused paths.

    Args:
        paths: Refused paths.
        base_paths: Base paths of the app's APIs.

    Returns:
        Paths without the placeholder as they are, and those with it once for
        each base path; a path that is only the placeholder is the path `/`
        for an API at the root, as for configured policy rules.
    """
    expanded: list[str] = []
    for path in paths:
        if path.startswith(BASE_PATH_PLACEHOLDER):
            expanded.extend(
                path.replace(BASE_PATH_PLACEHOLDER, base, 1) or "/"
                for base in base_paths
            )
        else:
            expanded.append(path)
    return expanded


def _parse_refused_path(path: str) -> list[_Item]:
    """Parse a refused path (cf. :py:func:`validate_refused_path`).

    Args:
        path: Path without the placeholder `{base_path}`.

    Returns:
        Items of the path: `*` matches any characters, `:name` a segment
        (one or more characters other than `/`), other characters themselves.
    """
    return _parse(path, generous=False)


def _parse_value(value: str) -> list[_Item]:
    """Parse a value of a rule generously, as a pattern of any matcher.

    Args:
        value: Value of a policy rule.

    Returns:
        Items of a pattern matching at least the request paths that the value
        matches as a `keyMatch`, `keyMatch2` or `keyMatch3` pattern or as a
        regular expression (cf. the module's description).
    """
    if _ANY_PATH_CHARS & set(value):
        return [_ANY_CHARS_ITEM]
    return _parse(value.replace("/*", "/.*"), generous=True)


def _parse(pattern: str, generous: bool) -> list[_Item]:
    """Parse a path pattern.

    Args:
        pattern: Pattern, as `keyMatch2` reads it (`:name` matches a segment).
        generous: Read it generously (cf. :py:func:`_parse_value`): `.` is any
            character; `*`, `?` and `{...}` are any characters, in place of
            the item before them, and `+` is any characters after it; `:name`
            with any of these is any characters (as other matchers read it).
            Otherwise, `*` is any characters, and other characters are
            literal.

    Returns:
        Items of the pattern.
    """
    items: list[_Item] = []

    def add_any_chars() -> None:
        """Add any characters, unless the last item already is."""
        if not items or items[-1] != _ANY_CHARS_ITEM:
            items.append(_ANY_CHARS_ITEM)

    index = 0
    while index < len(pattern):
        char = pattern[index]
        segment = _PARAMETER.match(pattern, index)
        if segment is not None:
            end = segment.end()
            if generous and set(pattern[index:end]) & set("*.+?{"):
                add_any_chars()
            else:
                items.append(_Item(_SEGMENT))
            index = end
            continue
        if char == "*" or (generous and char in "?{"):
            if generous and items:
                items.pop()
            if char == "{":
                end = pattern.find("}", index)
                index = len(pattern) if end < 0 else end
            add_any_chars()
        elif generous and char == "+":
            add_any_chars()
        elif generous and char == ".":
            items.append(_Item(_ANY_CHAR))
        else:
            items.append(_Item(_LITERAL, char))
        index += 1
    return items


class _Automaton:
    """Automaton accepting the request paths of a pattern.

    Args:
        items: Items of the pattern.
        within: Also accept sub-paths and custom methods of the paths (as
            resources of resource-scoped roles cover them).

    Attributes:
        accepting: States in which paths are accepted.
        edges: Transitions consuming a character, by state: label (a
            character, `_ANY` or `_NOT_SLASH`) and next state.
        epsilon: Transitions without consuming a character, by state.
    """

    def __init__(self, items: list[_Item], within: bool = False) -> None:
        """Build the automaton."""
        self.edges: dict[int, list[tuple[str, int]]] = {}
        self.epsilon: dict[int, list[int]] = {}
        self._closures: dict[int, frozenset[int]] = {}
        self._states = 1
        state = 0
        for item in items:
            following = self._new_state()
            if item.kind == _LITERAL:
                self._edge(state, item.char, following)
            elif item.kind == _ANY_CHAR:
                self._edge(state, _ANY, following)
            elif item.kind == _SEGMENT:
                loop = self._new_state()
                self._edge(state, _NOT_SLASH, loop)
                self._edge(loop, _NOT_SLASH, loop)
                self.epsilon.setdefault(loop, []).append(following)
            else:
                loop = self._new_state()
                self.epsilon.setdefault(state, []).append(loop)
                self._edge(loop, _ANY, loop)
                self.epsilon.setdefault(loop, []).append(following)
            state = following
        self.accepting = {state}
        if within:
            below = self._new_state()
            for separator in "/:":
                self._edge(state, separator, below)
            self._edge(below, _ANY, below)
            self.accepting.add(below)

    def _new_state(self) -> int:
        """Add a state.

        Returns:
            The state.
        """
        self._states += 1
        return self._states - 1

    def _edge(self, state: int, label: str, following: int) -> None:
        """Add a transition consuming a character.

        Args:
            state: State the transition starts from.
            label: Character, `_ANY` or `_NOT_SLASH`.
            following: State the transition leads to.
        """
        self.edges.setdefault(state, []).append((label, following))

    def closure(self, state: int) -> frozenset[int]:
        """Get the states reachable from a state without consuming characters.

        Args:
            state: State.

        Returns:
            The state and those reachable from it; computed once per state.
        """
        if state in self._closures:
            return self._closures[state]
        states = {state}
        pending = [state]
        while pending:
            for following in self.epsilon.get(pending.pop(), []):
                if following not in states:
                    states.add(following)
                    pending.append(following)
        self._closures[state] = frozenset(states)
        return self._closures[state]


def _labels_overlap(first: str, second: str) -> bool:
    """Check whether a character exists that two labels of transitions accept.

    Args:
        first: Character, `_ANY` or `_NOT_SLASH`.
        second: Character, `_ANY` or `_NOT_SLASH`.

    Returns:
        Whether a character exists that both accept.
    """
    if _ANY in (first, second):
        return True
    if first == _NOT_SLASH:
        return second != "/"
    if second == _NOT_SLASH:
        return first != "/"
    return first == second


def _overlap(first: _Automaton, second: _Automaton) -> bool:
    """Check whether a path exists that two automata accept.

    Args:
        first: Automaton.
        second: Automaton.

    Returns:
        Whether a path exists that both accept.
    """
    start = {(one, other) for one in first.closure(0) for other in second.closure(0)}
    seen = set(start)
    pending = list(start)
    while pending:
        one, other = pending.pop()
        if one in first.accepting and other in second.accepting:
            return True
        for label, one_next in first.edges.get(one, []):
            for other_label, other_next in second.edges.get(other, []):
                if not _labels_overlap(label, other_label):
                    continue
                for pair in (
                    (a, b)
                    for a in first.closure(one_next)
                    for b in second.closure(other_next)
                ):
                    if pair not in seen:
                        seen.add(pair)
                        pending.append(pair)
    return False


@lru_cache(maxsize=256)
def _refused_automaton(refused_path: str) -> _Automaton:
    """Get the automaton of a refused path, built once per path.

    Args:
        refused_path: Refused path, without the placeholder `{base_path}`.

    Returns:
        Automaton accepting the request paths that it matches.
    """
    return _Automaton(_parse_refused_path(refused_path))


def may_apply_to(value: str, refused_path: str, within: bool = False) -> bool:
    """Check whether a value of a rule may apply to paths of a refused path.

    Args:
        value: Value of a policy rule, as a path pattern (cf. the module's
            description).
        refused_path: Refused path, without the placeholder `{base_path}`.
        within: Whether the value also covers its sub-paths and custom methods,
            as resources of resource-scoped roles do; trailing slashes of the
            value are then ignored.

    Returns:
        Whether a request path exists that both match.
    """
    if within:
        value = value.rstrip("/")
    return _overlap(
        _Automaton(_parse_value(value), within=within),
        _refused_automaton(refused_path),
    )


def path_fields(ptype: str, model: Model) -> list[tuple[int, bool]]:
    """Get the fields of rules of a policy type that are paths.

    Args:
        ptype: Policy type, defined in the model.
        model: Casbin model.

    Returns:
        Indices of the values that are paths, and whether each is a resource
        (covering sub-paths and custom methods): for policy definitions, the
        fields named `obj` (e.g., `p = sub, obj, act`); for role definitions
        with three fields (e.g., `g2 = _, _, _`), the third.
    """
    tokens = model.model[ptype[0]][ptype].tokens
    if ptype.startswith("p"):
        return [
            (index, False)
            for index, token in enumerate(tokens)
            if token == f"{ptype}_obj"
        ]
    return [(2, True)] if len(tokens) == 3 else []


def inheriting_roles(roles: Iterable[str], links: Iterable[Sequence[str]]) -> set[str]:
    """Get roles and the subjects and roles that inherit them.

    Args:
        roles: Roles.
        links: Values of role rules (e.g., `["team-lead", "owner"]`): a subject or
            role, followed by the role it gets.

    Returns:
        The roles, and the subjects and roles that get one of them via the links,
        directly or via other roles (e.g., `team-lead` for `owner`). Values
        after the second (e.g., resources) are ignored, so that roles count as
        inherited on all resources.
    """
    inheriting = set(roles)
    edges = [(link[0], link[1]) for link in links if len(link) >= 2]
    added = True
    while added:
        added = False
        for subject, role in edges:
            if role in inheriting and subject not in inheriting:
                inheriting.add(subject)
                added = True
    return inheriting


def check_refused(
    ptype: str,
    values: list[str],
    model: Model,
    *,
    policy_types: Iterable[str] = (),
    roles: Iterable[str] = (),
    paths: Iterable[str] = (),
) -> None:
    """Refuse a rule of a policy type, role or path that is refused.

    Args:
        ptype: Policy type of the rule, defined in the model.
        values: Values of the rule, valid for the policy type.
        model: Casbin model.
        policy_types: Refused policy types.
        roles: Roles that role rules (policy types `g*`) must not grant,
            including those that inherit refused roles (cf.
            :py:func:`inheriting_roles`).
        paths: Refused paths, without the placeholder `{base_path}`.

    Raises:
        InvalidRuleError: The policy type is refused (`refused-policy-type`), the
            rule is a role rule granting one of `roles` (`refused-role`), or a
            value that is a path (cf. :py:func:`path_fields`) may apply to a
            path that a refused path matches (`refused-path`, cf.
            :py:func:`may_apply_to`).
    """
    if ptype in policy_types:
        raise InvalidRuleError(
            f"Rules of policy type {ptype!r} can't be written via the API",
            code="refused-policy-type",
            reason="Rules of this policy type can't be written via the API",
            field="policy_type",
        )
    if ptype.startswith("g") and len(values) >= 2 and values[1] in roles:
        raise InvalidRuleError(
            f"Role rules granting {values[1]!r} can't be written via the API",
            code="refused-role",
            reason="Role rules granting this role can't be written via the API",
            field=1,
        )
    paths = list(paths)
    for index, within in path_fields(ptype, model):
        for path in paths:
            if may_apply_to(values[index], path, within=within):
                raise InvalidRuleError(
                    f"Rule value {values[index]!r} may apply to the refused path "
                    f"{path!r}",
                    code="refused-path",
                    reason=(
                        "Rules for the paths of this value can't be written via the API"
                    ),
                    field=index,
                )
