# Runtime security v3 recovery preflight candidate

`scripts/hosted/verify-runtime-backup.py` is a **read-only** Linux root operator
command. It consumes the encrypted format produced by
`export-runtime-backup.py`; it does not extract files, write
`storage-roots.json`, authorize a replacement host, start services, or enable
the tandem-web legacy restore job. The v3 image allowlist remains empty.

The operator must obtain three exact encrypted objects from a private off-site
store into a private temporary directory: `manifest.json`, `archive.aead`,
and `anchors.aead`. The authority command must use an independent durable
receipt store, not the downloaded manifest or captured volumes, to return the
manifest object key, SHA-256, size, archive and anchor SHA-256 values, a
fenced-old-host decision, and an explicit operator authorization ID. The
preflight requires all of those fields and a private, immutable, verified HTTPS
object receipt for the exact organization/deployment/backup scope. A copied
local directory or a caller-provided manifest hash is not an authority.

Example invocation with **operator-provisioned** adapters (none are bundled):

```sh
python3 scripts/hosted/verify-runtime-backup.py \
  --backup-id <uuid> --organization-id <uuid> --deployment-id <uuid> \
  --manifest-path /var/lib/tandem-recovery-input/manifest.json \
  --archive-path /var/lib/tandem-recovery-input/archive.aead \
  --anchors-path /var/lib/tandem-recovery-input/anchors.aead \
  --recovery-authority-command /usr/local/libexec/tandem-recovery-authority \
  --offsite-host private-backups.example \
  --backup-kms-command /usr/local/libexec/tandem-backup-kms \
  --backup-key-id projects/<project>/locations/<location>/keyRings/<ring>/cryptoKeys/<key> \
  --backup-key-version projects/<project>/locations/<location>/keyRings/<ring>/cryptoKeys/<key>/cryptoKeyVersions/<version> \
  --memory-kms-command /usr/local/libexec/tandem-memory-recovery-challenge
```

All three commands must be root-owned, root-only executables under ancestors a
non-root user cannot replace. The recovery authority receives one JSON object
on stdin with `schema_version: 1`, `operation: "authorize_recovery_preflight"`,
and the three scoped UUIDs. It returns the
same scope, `schema_version: 1`, `manifest_object_key`, `manifest_sha256`,
`manifest_size`, `archive_sha256`, `anchors_sha256`, `remote_uri`,
`private: true`, `immutable: true`, `verified: true`,
`old_host_fenced: true`, `operator_authorized: true`, a nonempty
`authorization_id`, and `memory_challenge` with independently recorded
`ciphertext_base64` and `plaintext_sha256`. The command must establish the
fence and authorization from an external record; echoing the request is not
acceptable.

The backup KMS receives `operation: "unwrap"`, its exact key ID and version,
the three scoped UUIDs, and the encrypted `wrapped_dek_base64`; it returns
the same key/scope plus `plaintext_dek_base64` (exactly 32 bytes). It must
authorize unwrapping for that scope and must never log or persist the DEK.
The memory KMS command receives `operation: "decrypt_recovery_challenge"`,
the scoped UUIDs, the sealed memory KMS references, and the independent
challenge ciphertext. It returns those same references and scope plus
`plaintext_base64`. The preflight checks its SHA-256 against the external
authority's expected value; it never prints the plaintext.

After the authority and backup KMS checks, the verifier authenticates the
manifest AEAD, streams and hashes every encrypted archive record, checks each
tar member against the sealed inventory without extracting it, verifies the
separate anchor object's exact inventory, checks the original root
path/device/inode/sentinel binding and policy/release/source references, and
deserializes a bounded replay SQLite database in memory to run integrity,
pinned-schema, version, and row checks. The exact pinned engine uses SQLite
DELETE journal mode, so an uncheckpointed replay WAL is rejected. The replay
database is capped at 128 MiB; no plaintext archive file is staged on disk.
The output is a non-secret preflight report.

This **does not prove** the external adapters are correctly implemented, the
audit ledger's semantic HMAC chain, historical audit keys, replay continuity
against a later checkpoint, memory record decryptability, current control-plane
policy, old-host fencing in a live deployment, or clean-host startup. Those
checks and an atomic, independently authorized storage-root rebind are required
before any restore path may be enabled. The preflight result is evidence to
review, never an authorization token for changing a binding.
