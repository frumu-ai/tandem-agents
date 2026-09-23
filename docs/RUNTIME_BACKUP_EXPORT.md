# Runtime security v3 backup export candidate

`scripts/hosted/export-runtime-backup.py` is an **export-only** Linux operator
command. It cannot restore data, rebind `storage-roots.json`, authorize a new
host, or turn the tandem-web legacy snapshot/restore jobs back on. The v3 image
allowlist remains empty and tandem-web currently supports only v1/v2 bundle
rendering, so this is not live hosted backup or clean-host recovery evidence.

The command uses the installed `runtime-security.json` as the path inventory
contract. It rejects any version other than initialized v3, missing roots,
changed storage bindings/sentinels, missing replay SQLite, invalid policy scope,
missing audit key or context keyring, and release/engine provenance mismatch.
It captures the configured ordinary roots and the independent state, security,
replay, audit-anchor, panel-auth, and policy roots. The memory KMS executable
root is **not** copied: its command files, credentials, KEK, IAM policy, and
principal have to be provisioned independently. The encrypted manifest records
the non-secret memory KEK reference and rotation epoch. The on-host
`hosted-policy/current.json` is a cached revision; tandem-web's control-plane
database remains the policy source of truth.

## Operator boundary

The exporter only checks quiescence; it does not stop or restart anything.
The operator must stop `tandem-hosted-update-agent.service`,
`tandem-policy-sync-<deployment-id>.timer` and its service, and every hosted
Compose service, then confirm the deployment has no other writers. The CLI
rejects an active or unknown systemd unit, a running Compose service, a
non-root caller, or a non-Linux host. After export, the operator must restart
only services that were running before maintenance. A host outside this
maintenance boundary must not invoke the export.

Create a root-owned mode `0700` staging directory outside every captured root.
Every ancestor through the filesystem root must be root-owned and deny group
and other writes; a staging path under a writable parent such as `/tmp` is
rejected. The exporter creates private temporary files by directory descriptor
with exclusive, no-follow creation.

Provision two separate root-owned mode `0500` or `0700` executable commands
with ancestors that non-root users cannot replace: a backup KMS **wrap** command and a private
off-site uploader. Neither executable or its credentials belong in the
captured workload tree. For example, with operator-provisioned adapters:

```sh
python3 /srv/tandem/<slug>/export-runtime-backup.py \
  --install-root /srv/tandem/<slug> \
  --staging-root /var/lib/tandem-backup-staging \
  --backup-kms-command /usr/local/libexec/tandem-backup-kms-wrap \
  --backup-key-id projects/<project>/locations/<location>/keyRings/<ring>/cryptoKeys/<key> \
  --backup-key-version projects/<project>/locations/<location>/keyRings/<ring>/cryptoKeys/<key>/cryptoKeyVersions/<version> \
  --offsite-uploader-command /usr/local/libexec/tandem-private-backup-upload \
  --offsite-host private-backups.example
```

The exporter gives the commands a minimal `PATH` and `LANG` environment; their
credentials must come from separately provisioned root-only files or workload
identity. The wrap command reads one JSON object on stdin with `schema_version: 1`,
`operation: "wrap"`, the configured key ID/version, backup ID, organization and
deployment IDs, and `plaintext_dek_base64` (a fresh 32-byte DEK). It must
return `schema_version: 1`, the exact key ID/version and
`wrapped_dek_base64`. It must never log or persist the plaintext DEK. The
backup authority and key material must live outside the volumes being
captured; the engine's memory KMS command is a different authority.

The uploader reads JSON on stdin. `put_if_absent` receives an encrypted local
`source_path`, an immutable `v3/<organization>/<deployment>/<backup>/...`
object key, SHA-256, size, and `private: true`. It must conditionally create a
private object and return a receipt with `schema_version: 1`, the same key,
digest and size, `private: true`, `if_absent: true`, `verified: false`,
`created: true`, and a canonical HTTPS `remote_uri` on the configured host.
`verify` receives the expected key/digest/size and must independently read
back and hash the remote object; its matching receipt sets `verified: true`.
The exporter checks both receipts. A signed URL, public object, overwrite, or
local `file:` destination does not satisfy this interface. These adapters are
not bundled or operationally attested by this candidate; a real deployment
must validate the implementation, credentials, bucket privacy and retention.

## Artifact and completion semantics

The full archive and a separate copy of audit anchors are tar streams encrypted
as ordered AES-256-GCM chunks with a fresh DEK, disjoint nonce domains, and
scope-bound additional authenticated data. The manifest inventory is encrypted
and authenticated with the same DEK; its additional authenticated data binds
the outer scope, ciphertext digests, sizes, chunk counts, and wrapped DEK
reference. It records root path/device/inode/sentinel identities, file hashes,
policy revision/hash, engine source/image/binary/attestation references, and
non-secret memory KMS references. Plaintext archives are never staged on disk.

The exporter uploads and verifies the archive and anchor objects first. It
publishes `manifest.json` **last**; only a verified manifest object counts as a
completed export. Failures may leave encrypted orphan objects, which must not
be interpreted as backups. The exporter never edits the source roots or
`storage-roots.json`. Its command result reports only the backup ID, manifest
URI and SHA-256, and tenant scope.

If the final manifest PUT succeeds remotely but its acknowledgement or
verification fails, the command reports **completion unconfirmed** with the
backup ID, manifest object key, SHA-256 and size. An operator must reconcile
that exact object and its integrity in the remote store before retrying;
command failure alone does not prove the manifest is absent.

This slice does not prove the protected audit ledger's semantic chain or
historical audit-key availability, memory KMS access from a new host, control-
plane policy continuity, remote storage immutability, or restorable recovery.
Those require separate independent evidence, a reviewed restore/rebind design,
and an authorized operator decision. No restore path is provided here.
