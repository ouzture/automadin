# Code signing policy

## Current status

Automadin `0.3.1` Windows artifacts are published unsigned while the project
applies to the SignPath Foundation open-source program. Windows may therefore
display **Unknown publisher** or a Microsoft Defender SmartScreen warning. An
unsigned release is identified prominently on its GitHub release page and is
accompanied by SHA-256 hashes and matching Corresponding Source.

Automadin intends to use **free code signing provided by SignPath.io,
certificate by SignPath Foundation**. This statement describes the requested
signing route; it does not claim that SignPath Foundation has accepted Automadin
or signed any artifact. This document and the release page will be updated when
that status changes.

Self-signed development certificates are not used to represent public releases
as trusted. A checksum can detect that downloaded bytes differ from the bytes
published by the project, but it is not a substitute for an Authenticode
publisher signature.

## Project and roles

- Source repository: <https://github.com/ouzture/automadin>
- License: GPL-3.0-only
- Committer and reviewer: [ouzture](https://github.com/ouzture)
- Signing approver: [ouzture](https://github.com/ouzture)

Automadin currently has one maintainer. Contributions are accepted through pull
requests; the maintainer reviews changes before merging. Release signing, once
enabled, will require a separate manual approval through the signing service.

## Build and signing controls

- Release inputs and build scripts are maintained in the public repository.
- Protected `main` rejects force pushes and deletion; changes are merged through
  pull requests.
- Release dependencies are exactly pinned and verified before packaging.
- Release artifacts are generated from a clean tagged commit by the documented
  Windows release workflow.
- The signing workflow will accept only artifacts produced by the configured
  trusted build system from the approved repository and release reference.
- Signing credentials or private keys must never be committed to the repository
  or exposed to a local build.
- Signed executable and installer metadata must identify Automadin and use the
  project version from `pyproject.toml`.

The SignPath workflow configuration will be added after the project receives its
SignPath organization, project, policy, and artifact-configuration identifiers.

## Privacy and network behavior

Automadin runs locally. It transfers information to networked systems only when
the user configures or invokes functionality that requires it, such as browser
automation, downloading optional runtimes, or sending configured notifications.
The [privacy and safe-sharing guide](https://ouzture.github.io/automadin/user-guide/privacy-and-safety/)
describes sensitive local data and safe reporting practices.

## Reporting concerns

Report suspected vulnerabilities privately as described in
[SECURITY.md](SECURITY.md). Questions about release authenticity may be opened as
a public issue when they do not disclose a vulnerability or private data.
