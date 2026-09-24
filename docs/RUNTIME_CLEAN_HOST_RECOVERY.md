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

The Tandem web host-agent snapshot reviewed at commit
`02edded32762eba4e7b9f4b47cc18b2414a62fc4` explicitly rejects both legacy
snapshot and restore jobs whenever `runtime-security.json` exists. Preserve
that gate. Its archive path includes deployment-local data, engine state, panel
state, repositories, runs, proxy, secrets, and release files, but omits the
separate runtime-security root (audit HMAC key, public keyring, and storage
binding), durable context replay root, independent audit-anchor root,
hosted-policy root, and panel-auth root. The external memory KMS key, principal,
command provisioning, and access policy also require independent recovery. The
local tar archive and adjacent SHA-256 metadata do not establish an encrypted,
off-site, independently authenticated backup. A v3 backup/restore procedure
must be a separate path, not an extension that enables the legacy archive.

The next backup slice belongs in tandem-web's host-agent and control plane:

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
   ledger, prove replay continuity, and verify the KMS challenge. Require an
   explicit operator decision and fence the old host. Atomically compare the
   old `storage-roots.json` identity before writing a new one, then sync the
   file and parent directory. Start the engine only after its own audit and
   memory checks pass, and record the decision outside the restored volume.

A future rebind command must reject missing, mismatched, stale, or self-signed
evidence. No caller-supplied JSON inventory or copied directory is itself an
authorization to change the binding. The production v3 image allowlist remains
empty until separate source/image attestation and the engine memory regression
are resolved.
