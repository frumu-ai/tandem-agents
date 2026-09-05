# Assertion verifier key rotation

This operator workflow uses the existing scoped Ed25519 metadata keyring and
runtime reload. Private assertion signing keys remain in the control plane/KMS.
Account JWT signing, service tokens, memory encryption and audit HMAC keys are
separate lifecycles; this command does not rotate or export them.

The shared package 1.2 adds `tandem_runtime_bundle.keyring_lifecycle`. Use the
engine source pinned by this package. Its narrow
`POST /admin/context-assertions/reload` requires a currently verified hosted
administrator and current hosted policy. It reads only the operator-configured
keyring; request bodies cannot select keys, paths or credentials. It preserves
the replay store, issuer/audience, lifetime settings and other runtime services.

## Planned rotation

1. Obtain the next KMS public key through the existing control-plane exporter.
   Use a new key ID. Keep the currently used key active and add the new active
   entry with the same organization/deployment/audience scope. A future
   `not_before_ms` can stage the new key before its intended activation.
2. Save the proposed public document and a copy of the matching runtime bundle
   as root-owned, mode-0600 files. Compute the current public-document SHA-256
   using `fingerprint(parse_document(current_bytes))` from the shared module.
   This digest identifies the staged JSON, not the engine's metadata fingerprint.
3. Preview, inspect the returned old/new document digests, then stage:

   ```sh
   python -m tandem_runtime_bundle.keyring_lifecycle \
     --bundle /root/rotation/runtime-security.json \
     --keyring /root/rotation/proposed-keyring.json --expected CURRENT_SHA256
   # Repeat the same command with --apply after reviewing the preview.
   ```

4. Invoke the narrow reload through the existing authenticated runtime transport
   with a fresh administrator assertion. Check the response's verifier
   fingerprint and probe both old-key and new-key assertions. The prepared
   fingerprint is durably audited before publication. A file receipt alone
   does not establish that the engine has loaded it. A lost response can be
   reconciled by repeating reload and checking its returned fingerprint.
5. Switch the existing control-plane signing key ID/KMS version only after all
   intended runtimes accept the new key. Keep both public keys during rollout
   and the maximum assertion lifetime. Verify fresh user sessions use the new
   key. The operator owns this rollout; no host command changes live KMS state.
6. Mark the old entry `retired`, retaining its metadata. Repeat preview/stage,
   reload and probes. New requests signed by the old key must fail; new-key
   requests and the unaffected user must succeed. Test again after restart.

Changing key material under an existing ID, extending its declared validity,
dropping old metadata, or reactivating retired/revoked keys is rejected. Existing
bootstrap uses the same transition checks, preventing a stale deployment update
from silently restoring a retired key. Competing updates serialize with an OS
file lock and compare the expected digest. Retrying an already staged document
is idempotent and completes its file/directory durability checks.

## Rejected reload and recovery

Malformed, wrongly scoped or regressive replacements leave the live verifier
unchanged. Repair the operator file before restart: a live last-known-good
snapshot is not an on-disk backup. Retain the complete latest scoped document,
including retired/revoked entries, in the authorized recovery set. Missing keys
after initialization require recovery; bootstrap must not create new authority.

Retirement applies at assertion verification on new requests. Work already
admitted is governed by its existing assertion expiry and current policy. For
a compromised signing key, stop/quiesce the affected runtime before installing
the revocation and restart from the reviewed keyring; do not assume a normal
reload cancels all previously admitted work.

The companion process test exercises real non-root runtime reload, overlap,
retirement, wrong scope, malformed reload, an unwritable audit anchor, preserved
replay and restart with synthetic keys. CI results must be recorded before
release. This workflow does not constitute encrypted clean-host recovery or
permission to close TAN-840/TAN-836.
