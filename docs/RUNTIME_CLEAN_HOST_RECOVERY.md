# V3 clean-host recovery gate

Runtime security v3 has no authorized storage-root rebind operation. An
initialized installation records the state, data, replay, and independent
audit-anchor path, device, and inode in `storage-roots.json`. The replay and
anchor roots also hold a per-root provisioning sentinel. Repeated provisioning
rejects copied directories, empty replacements, and roots whose sentinel was
removed instead of silently accepting a new empty history directory. This
sentinel does not authenticate the mutable replay database or individual audit
anchors; selective deletion or rollback still needs independent evidence and
engine verification. Do not edit `storage-roots.json` to make a copied
installation start.

Legacy local snapshots do not meet the v3 recovery contract. Recovery requires
an encrypted off-site backup, an independently authenticated receipt, preserved
replay and audit continuity, current signing authority, and separately
provisioned KMS access. A copied archive with an adjacent checksum is
insufficient. Existing v3 restrictions on legacy snapshot recovery remain in
force until the complete recovery procedure has acceptance evidence.

An export-only operator candidate is described in
`RUNTIME_BACKUP_EXPORT.md`. A read-only encrypted recovery preflight candidate
is described in `RUNTIME_BACKUP_RECOVERY_PREFLIGHT.md`. Neither enables
extraction or any rebind. The complete recovery contract requires:

1. Quiesce writes and policy synchronization, then capture a consistent set of
   all v3 data and security roots, including replay and audit anchors. Record
   the organization/deployment scope, source and image digests, old root
   identities, file inventory, and per-root hashes. Reject links and path
   traversal during archive creation and extraction.
2. Encrypt the backup and authenticate its manifest with a backup authority
   outside the restored volumes. Retain an independently protected audit-anchor
   export and the historical audit key IDs. Provision KMS access on the new host
   independently; prove it can decrypt a stored challenge under the required
   key version without exposing key bytes.
3. Before any rebind, verify the backup signature and contents against that
   independent authority, compare the external anchor with the restored audit
   ledger, prove replay continuity, verify the KMS challenge, and compare the
   archived verifier keyring with the latest runtime-acknowledged checkpoint
   from a separate recovery ledger. Require an explicit operator decision and
   fence the old host. Atomically compare the old `storage-roots.json` identity before writing a new one, then sync the
   file and parent directory. Start the engine only after its own audit and
   memory checks pass, and record the decision outside the restored volume.

A future rebind command must reject missing, mismatched, stale, or self-signed
evidence. No caller-supplied JSON inventory or copied directory is itself an
authorization to change the binding. The production v3 image allowlist remains
empty until separate source/image attestation and the engine memory regression
are resolved.
