# M5 release checklist

[Version 0.1.0a1 is published](https://github.com/dzuritaa/cloudmarks/releases/tag/v0.1.0a1) as an MIT-licensed experimental prerelease. The immutable source tag is `v0.1.0a1`. M5 prepares a
shareable experimental package with clear limits. It does not turn pending
physical acceptance into a production-safety claim.

## Candidate gates

- [x] Standard-library CLI runtime, version output and alpha package metadata.
- [x] Pinned build/lint tools, package assets and source-reproduction contents.
- [x] Credential-free CI matrix for Python 3.11–3.14 and package/source rebuild.
- [x] Read-only CI permissions; no cloud/desktops/secrets in default jobs.
- [x] Tag-only prerelease publication runs after validation; branch pushes never publish.
- [x] Compatibility, install/upgrade/rollback/disable/uninstall/recovery instructions.
- [x] Optional status-only helper unit template; no automatic view/download service.
- [x] MIT selected by the owner and applied to LICENSE plus SPDX package metadata.
- [x] Candidate checks and validation record finalized (licensed local candidate validation).
- [x] Licensed source reviewed and committed locally.
- [x] Source commits pushed to personal repository dzuritaa/cloudmarks main.
- [x] Hosted main and exact-tag CI passed.
- [x] The deliberate release tag published validated assets/notes/checksums as an experimental prerelease.

MIT is applied to LICENSE and SPDX package metadata. Both CLI and Git
credentials were verified as personal account dzuritaa. Personal-account workflow
reauthorization resolved the earlier scope rejection. Main is pushed and
[hosted Python 3.11–3.14/package CI passed](https://github.com/dzuritaa/cloudmarks/actions/runs/39450706558).
The [exact-tag release pipeline](https://github.com/dzuritaa/cloudmarks/actions/runs/39451141486) also passed and published the prerelease.
The downloaded wheel and source archive matched SHA256SUMS and passed the
license/source inventory checker. Physical operational acceptance remains
separate and open.

## Prepare and publish after the gates

Run the [contributor checks](../../CONTRIBUTING.md), including installed-wheel tests
and source rebuild, from the intended source revision. Review the full diff,
sanitary archive inventory and selected license. Include all project sources and
historical evidence intentionally; the repository initially tracked documentation
only, so a tag of the old HEAD would not contain the implementation.

Commit/push the reviewed licensed source and wait for Validate to pass. Create
and push the matching version tag only when ready to publish. **Publish
experimental prerelease** runs for that tag, reruns validation, verifies the
version/license/source contents, and publishes a **prerelease** with the two
distributions and SHA256SUMS. This is the release action already authorized by M5;
there is no additional draft-approval gate. A manual run is also available on a
matching tag. Branch pushes never publish. An existing release is not overwritten
by a retry; inspect its state deliberately before further action.

The publication job uses the repository's GitHub Actions token with contents:write;
no OneDrive credential is involved. The local GitHub API connection and Git
transport may have different permissions. Actual source/workflow push and hosted
publication must succeed before recording the release as published.

Do not publish to PyPI
or label this production-ready as part of this milestone. Checksums verify bytes,
not publisher identity; signing/attestation can be evaluated for a later release.
The exact local and hosted outcomes are recorded separately in
[the M5 validation record](../evidence/m5/validation.json).

## Operational acceptance before daily use

These remain open even if an experimental prerelease is published:

- [ ] Physically lose/restore network access during a disposable download/read.
- [ ] Disconnect/reconnect the actual content disk and verify expected UUID, marker and binding.
- [ ] Full logout/login, optional helper lifecycle and deliberate view restart.
- [x] M6 development: 16 MiB real-file server checksum, two offline mounts and bounded read-only helper checks. This is not part of the immutable 0.1.0a1 assets.
- [x] M6 development: explicit archive/export and same-identity conflict retry, including interruption recovery tests.
- [ ] Accessibility/readability and repair-workflow review with the user at their normal settings.

The [M6 trial guide](m6-hardening.md) records the development workflow and keeps
physical/user acceptance separate from software checks.

Save work and backups before tests that interrupt the desktop or storage. Do not
simulate these by unplugging an active disk/session autonomously. No daily mount
replacement, personal account connection or administrator package change is
part of the current release work.
