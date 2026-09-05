# Runtime security bundle v1

The existing hosted shell tools and Python cloud adapters share the
`tandem-runtime-bundle` package in `packages/runtime-bundle`. Its pure
`build_security_bundle(mapping)` function defines security settings and mounts;
service topology remains in the existing renderers. `runtime-security.json` and
release metadata carry `runtime_security_version: 1`. Consumers must pin the
package revision and reject unknown versions.

The supported test target is Linux amd64, engine and panel 0.7.2, local storage,
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
