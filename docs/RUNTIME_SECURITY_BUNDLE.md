# Runtime security bundle v1

The existing hosted shell tools and Python cloud adapters share the
`tandem-runtime-bundle` package in `packages/runtime-bundle`. Its pure
`build_security_bundle(mapping)` function defines security settings and mounts;
service topology remains in the existing renderers. `runtime-security.json` and
release metadata carry `runtime_security_version: 1`. Consumers must pin the
package revision and reject unknown versions.

The supported test target is Linux amd64, engine 0.7.2 and the panel built from
Tandem commit `3ee2d83d76497565680538ef00f1616f55650524`, local storage,
and `hosted_single_tenant` authentication with bound replay. This is centrally
authenticated: existing accounts and organizations remain in the control plane.
It does not promise disconnected login. Shared/Postgres storage, a separate
outbox, other runtime versions, local-auth fallback and unpinned images reject.

Required non-secret inputs:

- `HOSTED_DEPLOYMENT_ID`, `HOSTED_ORGANIZATION_ID`: existing UUIDs.
- `HOSTED_INSTALL_ROOT`: normalized absolute Linux installation path.
- `HOSTED_AUDIT_ANCHOR_ROOT`: independent path outside the installation tree.
- `HOSTED_CONTROL_PLANE_URL`, `HOSTED_CONTROL_PANEL_PUBLIC_URL`: HTTPS URLs;
  explicit HTTP loopback is supported for development.
- `HOSTED_LOGIN_BASE_URL`: optional website URL when separate from the API.
- `HOSTED_TANDEM_ENGINE_RELEASE_VERSION` and
  `HOSTED_TANDEM_CONTROL_PANEL_RELEASE_VERSION`: `0.7.2`.
- `HOSTED_TANDEM_CONTROL_PANEL_SOURCE_REVISION`:
  `3ee2d83d76497565680538ef00f1616f55650524`. npm panel 0.7.2 predates merged
  identity hardening and is insufficient. The panel Dockerfile builds the
  pinned source with its frozen dependency lockfile.
- `HOSTED_ENGINE_IMAGE`, `HOSTED_CONTROL_PANEL_IMAGE`, `HOSTED_PROXY_IMAGE`,
  and selected `HOSTED_ACA_IMAGE` / `HOSTED_KB_IMAGE`: `repository@sha256:digest`.
- `HOSTED_HOST_UID` / `HOSTED_HOST_GID`: nonzero runtime owner, default 1000.

The release publisher records actual build digests; rendering never resolves
`latest`. At implementation time the public stack images stopped at 0.7.1.
Publish and register a tested 0.7.2 image set before deploying this contract;
older image tags are not relabeled as compatible.

Bootstrap additionally requires operator-provisioned files via
`HOSTED_CONTEXT_KEYRING_SOURCE_FILE` and `HOSTED_HOST_AGENT_TOKEN_SOURCE_FILE`.
The token must already be registered with the existing control plane. The
keyring contains public Ed25519 verification metadata, explicitly scoped to the
organization, deployment and `tandem-runtime` audience with an active
`context_assertion` key. Private assertion signing keys stay with the control
plane. Bootstrap does not create accounts or invent host-agent authority.

Security provisioning creates runtime-owned directories with mode 0700 and
files with mode 0600. The engine creates its SQLite replay file inside durable
storage; an empty placeholder must not be created. The audit HMAC key is
generated once, mounted only into the engine security directory, and preserved
on retry. Missing keys after initialization require authorized recovery.
Symlinks and insecure existing ownership/modes reject. Audit anchors have an
independent mount outside engine state. Engine and panel run without root,
with read-only roots, dropped capabilities and no new privileges.

The panel host-agent token and trusted configuration are provisioned together
in a separate owner-only directory mounted read-only only into the panel.
The shared data directory is not an authentication configuration authority.
Security roots cannot overlap ordinary workload mounts; provisioning rejects
symlink aliases. HTTP development origins must use literal 127.0.0.1 or [::1].

Validation:

```sh
python -m pip install ./packages/runtime-bundle PyYAML cryptography
python -m unittest discover -s packages/runtime-bundle/tests -p 'test_*.py' -v
TANDEM_TEST_ENGINE=/path/to/verified/tandem-engine \
  python packages/runtime-bundle/tests/engine_integration.py
```

CI verifies the SHA-256 of the released 0.7.2 enterprise engine, tests startup
with each missing prerequisite, validates owner-only permissions, sends signed
and unsigned requests, and tests bound replay across a process restart. The
existing hosted health smoke remains service liveness evidence only.

Full user recovery, membership revocation, policy synchronization, two-user
privacy, encrypted backup/key recovery and clean-host restoration remain
separate acceptance gates. This contract does not close TAN-836 or TAN-840.

## Policy synchronization profile v2

The v2 profile adds a separate read-only engine policy mount and an independent
host timer. Set `HOSTED_RUNTIME_SECURITY_VERSION=2` and explicitly pin
`HOSTED_TANDEM_ENGINE_SOURCE_REVISION` to the source supported in
`policy_contract.py`. The historical 0.7.2 release binary does not support this
profile. A source pin in a manifest is not proof of image contents; build and
verify the corresponding image before publishing a v2 release.

The authorized Linux operator installs the policy service using the same
shared package. Its host token source must be root-owned with mode 0600. A
root-only retained copy stays in the management directory; the engine receives
only the policy snapshot. The timer runs independently of deployment updates,
with a 15-second service timeout and a 30-second interval. Fetches verify TLS,
do not follow redirects, bound response size and retain the source timestamp.
The engine remains responsible for complete semantic validation, revision
rollback protection, effective grants and the 120-second freshness limit.
Failed fetches leave the last complete file intact, while expiry blocks use.

Policy files are replaced atomically with mode 0600 and the runtime UID. A
policy-enabled installation rejects a downgrade to v1. The engine projects
current memberships and narrowly scoped operation grants into its existing
enterprise authorization. Source-built engine image validation and the broader
deployment acceptance gates are still required before rollout.

The `policy-engine` CI job builds the exact enterprise engine source recorded
by `POLICY_ENGINE_REVISION` and runs it as a non-root user with the copied policy
agent and synthetic authenticated TLS. Its process test covers private session
isolation, membership removal, fresh identity after revision changes, restart,
failed fetches, real wall-clock expiry and recovery. Registry projection is
exercised through the existing enterprise HTTP APIs. This test does not publish
an image or establish encrypted clean-host recovery or governed-memory privacy.

## Encrypted hosted-memory profile v3

The v3 profile retains the v2 policy, replay and audit protections and requires
envelope encryption for hosted memory. Set `HOSTED_RUNTIME_SECURITY_VERSION=3`
and pin `MEMORY_ENGINE_REVISION` from `policy_contract.py`. Its source includes
the external KMS interface while the v2 source pin remains unchanged. This
candidate is blocked by the global-memory plaintext regression described below.
Consumers must opt in to v3 and supply all of these non-secret references:

- `HOSTED_MEMORY_ENCRYPTION_REQUIRED=true`
- `HOSTED_MEMORY_KMS_PROVIDER=google_cloud_kms`
- `HOSTED_MEMORY_KMS_RUNTIME_PRINCIPAL_ID`, scoped to the deployment ID
- `HOSTED_MEMORY_KMS_ENCRYPT_COMMAND` and `HOSTED_MEMORY_KMS_DECRYPT_COMMAND`,
  direct executable paths in the dedicated `/run/tandem-memory-kms` engine mount
- `HOSTED_MEMORY_KMS_COMMAND_ROOT`, the independent host source for that mount
- `HOSTED_MEMORY_KEK_ID`, `HOSTED_MEMORY_KEK_VERSION`, and
  `HOSTED_MEMORY_KEK_ROTATION_EPOCH`

Production v3 rendering and provisioning currently reject all images: the
code-owned verified image digest map is empty. A separate release PR must add
an independently verified digest built from `MEMORY_ENGINE_REVISION` before v3
can be packaged or deployed. A caller-supplied source revision alone is not
image provenance. The engine receives the corresponding `TANDEM_MEMORY_*`
settings. The operator must preprovision the command root as root-owned,
runtime-group-owned mode 0750, with direct regular single-link command files
mode 0550 and no secret bytes in those executables. Its parent path must also
prevent non-root replacement. Only the engine mounts this
root, read-only; the shared `/run/secrets` mount remains separate. This bundle
does not install KMS credentials or provision a KMS key.

The host bootstrap creates and chowns the state and data roots before security
provisioning. It never stages or chowns the dedicated KMS command root. No
product container except the non-root engine mounts that root, and the engine's
bind is read-only. The root-owned 0550 files cannot be changed by the engine's
UID/GID through these product mounts.

An initialized v1/v2 store cannot be relabeled v3 without an authorized memory
migration. A new security root rejects existing engine state or workload data
beyond the bootstrap panel config, and v3 binds the exact state/data directory
identities on every repeated provision. V3 cannot be downgraded. The web
host-agent adapter must add these values before it can consume v3.

The `memory-engine` CI job builds the pinned source and runs a non-root process
with a disposable external-command KMS. The test uses synthetic local-mode
authorization only to isolate the crypto path; it does not establish hosted
policy/grant behavior. The current candidate fails its at-rest check: a
`/memory/put` global record leaves the content canary in `memory.sqlite-wal`.
The source inserts global-record content, metadata and provenance directly into
`memory_records`, and its FTS trigger copies content. A tandem engine fix must
seal those fields without retaining plaintext in the DB, WAL or FTS, then pass
the cold-read and wrong/missing-key checks before `MEMORY_ENGINE_REVISION` and
the trusted image digest map can advance. The current red CI check and empty
image map deliberately prevent v3 release. Live KMS, authorized encrypted
migration, off-site backup, clean-host recovery, and hosted two-user privacy
still require separate acceptance evidence.
