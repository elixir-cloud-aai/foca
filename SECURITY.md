# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately, **not** in public issues, pull
requests or discussions:

- through GitHub's [private vulnerability
  reporting](https://github.com/elixir-cloud-aai/foca/security/advisories/new)
  ("Report a vulnerability" on the repository's Security tab), or
- by email to
  [cloud-service@elixir-europe.org](mailto:cloud-service@elixir-europe.org).

Please include, as far as you can:

- the affected version (release or commit);
- the relevant configuration (without secrets);
- steps to reproduce, ideally a minimal example;
- the impact, i.e., what an attacker can achieve.

## Supported versions

Security fixes are made on `dev` and released in a new version of the latest
release series (the latest minor version). Older versions get fixes only for
critical issues, at the maintainers' discretion.

## What happens next

The maintainers acknowledge the report, assess it, and, if they confirm it,
develop a fix in private. They publish the fix with a GitHub security advisory
(with a CVE where appropriate), coordinating the timing with the reporter, and
credit reporters who wish to be credited. The project is maintained on a
best-effort basis: there are no guaranteed response or fix times.

## Scope

This policy covers the code of this repository and the artifacts published
from it (the `foca` package on PyPI, the container images and the Helm
library chart). Vulnerabilities in apps and deployments run by third parties
go to their operators, and those in dependencies to their projects; tell us
too if they affect FOCA.
