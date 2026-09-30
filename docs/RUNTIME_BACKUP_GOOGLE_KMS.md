# Google Cloud KMS command for v3 backup DEKs

`tandem_runtime_bundle.backup_google_kms` implements the backup KMS subprocess
protocol used by the export and read-only recovery-preflight commands. Install
the package with both `backup` and `google-backup-kms` extras, then install a root-owned,
root-only executable wrapper outside all captured roots that runs
`python3 -m tandem_runtime_bundle.backup_google_kms`. The packaged console
entry point is convenient for testing; its usual mode `0755` is deliberately
rejected by the operator CLIs. No workload container receives this command or
its credential.

The command reads `/etc/tandem-backup-kms/config.json`, which must be a
root-owned, single-link regular file of mode `0600` under root-owned ancestors
that are not group- or world-writable. It contains exactly these fields:

```json
{
  "schema_version": 1,
  "operation": "wrap",
  "key_id": "projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/BACKUP_KEY",
  "key_version": "projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/BACKUP_KEY/cryptoKeyVersions/1",
  "organization_id": "00000000-0000-4000-8000-000000000001",
  "deployment_id": "00000000-0000-4000-8000-000000000002",
  "credentials_file": "/etc/tandem-backup-kms/credentials.json"
}
```

The explicit credentials file has the same owner, link and mode requirements.
It may be a Google-auth supported external-account configuration or another
operator-approved credential type. Provision a **separate backup key**, not
the hosted-memory KEK. The old host must have a principal permitted only to
encrypt under the selected version and a config with `operation: "wrap"`.
Only after independent recovery authorization and old-host fencing should a
new host receive a different, decrypt-capable principal and a config with
`operation: "unwrap"`. The module refuses the opposite operation before any
cloud call. The unwrap credential is a trusted root capability: this module
does not independently verify the recovery receipt or operator authorization.
The read-only preflight verifies those facts before it invokes the command;
any root process holding the unwrap credential could instead call Cloud KMS
directly. Provision and remove that credential within the independently
authorized recovery window. Preserve the old key version in enabled state for the backup's
retention period; changing the primary version does not re-encrypt old DEKs.

Each KMS request binds backup, organization, deployment, key ID and exact key
version as additional authenticated data. Wrapping asks Cloud KMS for that
version explicitly and verifies the returned version, request verification
flags and ciphertext CRC32C. Unwrapping passes the same authenticated data
and checks the returned plaintext CRC32C and 32-byte DEK length. Cloud API
details, paths and key material are not printed on failure. The short-lived
process returns plaintext only to the calling root operator process over its
stdout protocol; it never writes a DEK to disk.

This command does **not** create or attest the independent backup receipt,
off-site object store, recovery authorization, memory-KMS challenge, live
fence, or clean-host restoration. The read-only preflight must obtain that
authority before it invokes the unwrap command. The installation must verify
the cloud IAM grants, credential lifetime, key-version availability and
provider behavior in a real drill; unit tests use a fake KMS client.
