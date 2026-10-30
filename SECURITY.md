# Security policy

Hangar manages system software. It runs a privileged helper as root through PolicyKit and it can
delete files, so security reports are taken seriously.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |
| < 1.0 | No (pre-release builds) |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through
[GitHub private vulnerability reporting](https://github.com/Rajveer-Mahida/hangar/security/advisories/new),
or by email to **hello@rajveerx.in**.

Please include:

- the Hangar version and how it was installed (`.deb`, `install.sh` or from source)
- your Ubuntu (or derivative) version
- the steps to reproduce, and what an attacker gains

Hangar is maintained by one person, so replies may take a few days. You'll be kept updated until the
issue is fixed, and you'll be credited in the release notes unless you'd rather not be.

## What counts

Especially interesting:

- running code or commands as root through the helper (`hangar-helper`) or its PolicyKit actions
- getting the helper to install, remove or delete something other than what the user confirmed
- deleting or trashing files outside what the removal preview showed, including through symlinks,
  mount points or race conditions
- bypassing package-name, path or checksum validation
- leaking credentials or command output into the history database or logs

Out of scope: problems that need an attacker who already has root, or who can already write to
root-owned directories such as `/usr` or `/usr/libexec`.
