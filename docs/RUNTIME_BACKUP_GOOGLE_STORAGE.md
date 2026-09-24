# Google Cloud Storage transport for v3 backup objects

`tandem_runtime_bundle.backup_google_storage` implements the export-only
`OffsiteUploader` subprocess protocol. It uploads encrypted `archive.aead` and
`anchors.aead`, then the authenticated `manifest.json`, under exact
`v3/<organization UUID>/<deployment UUID>/<backup UUID>/` keys. The module does
not decrypt or restore the objects. Install the package with both `backup`
and `google-backup-storage` extras and put a root-owned mode `0500` or `0700`
executable wrapper outside captured roots that runs
`python3 -m tandem_runtime_bundle.backup_google_storage`. The packaged console
entry point is usually mode `0755`, so the exporter rejects it directly.
The optional SDK dependency is pinned to published `google-cloud-storage`
`3.14.1`; CI checks its bucket metadata and Blob method shape without cloud
credentials.

The command reads `/etc/tandem-backup-gcs/config.json`. It and the explicit
credentials file must be root-owned, single-link regular files, mode `0600`,
under root-owned ancestors that are not group- or world-writable. No workload
container receives them. The config has exactly these fields:

```json
{
  "schema_version": 1,
  "bucket": "private-tandem-backups",
  "project_id": "example-backup-project",
  "credentials_file": "/etc/tandem-backup-gcs/credentials.json",
  "organization_id": "00000000-0000-4000-8000-000000000001",
  "deployment_id": "00000000-0000-4000-8000-000000000002",
  "minimum_retention_seconds": 2592000
}
```

The credential file must be beside the config, outside captured roots. The
configured organization and deployment IDs bind every accepted object key.
Provision the bucket in the region or dual-region approved for the
organization's data-residency and legal jurisdiction requirements. Verify its
actual location and any replication policy independently during provisioning
and include that evidence in the recovery authority's protected receipt; this
adapter does not enforce or attest bucket location.
The bucket name must be a DNS-safe name without dots. The resulting URI is
`https://<bucket>.storage.googleapis.com/v3/...`, so pass
`--offsite-host <bucket>.storage.googleapis.com` to the exporter. This is an
object identifier, not a public grant or signed URL. The uploader validates
the bucket metadata before and after each operation: uniform bucket-level
access, explicit public access prevention, a locked and effective retention
policy of at least the configured duration, and disabled Object Versioning.
The bucket lock protects objects for the **configured retention window**, not
forever. An administrator can still change access or enable versioning later;
the latter can make a retained live object noncurrent, so the URL would no
longer necessarily address the retained generation. Revalidate policy and
generation during every independent recovery decision. Public access
prevention does not disable signed URLs; this adapter never creates one, and
the credential should be kept outside the workload with a short lifetime.

Provision the uploader with only the permissions it needs to read bucket
metadata, create objects, and read objects. Do not grant bucket update/delete,
object update/delete, or public policy administration to that credential. The
adapter first hashes the root-only encrypted staging file, creates the object
with `if_generation_match=0`, and checks the created generation. A separate
`verify` call receives the generation returned by `put_if_absent`, downloads
that live generation with a generation precondition, streams its SHA-256, and
compares exact size and digest. The exporter rejects a different generation
even if the bytes and URL match. When the uploader attests generations for all
three objects, export output includes their keys, hashes, sizes, URLs and
generations in `offsite_objects`. This output is evidence to submit to the
independent recovery authority; local export output is not itself that
authority's durable receipt. A failed or unacknowledged
manifest upload requires reconciliation before retrying; the export command
does not interpret an exception as proof that no object was created.

This adapter **does not create the independent durable backup receipt** that
`verify-runtime-backup.py` requires, nor does it fence an old host or authorize
recovery. The separate authority must retain the exact object generation,
digest, scope, original bucket policy evidence, and later fencing decision
outside both the old host and its encrypted backup. The preflight currently
accepts an authority-provided URL and hashes the downloaded objects; it does
not fetch a retained noncurrent generation. No production bucket, IAM policy,
live transfer, independent authority, or clean-host drill is attested by the
fake-client tests in this repository.

Cloud Storage documents the [virtual hosted HTTPS endpoint](https://docs.cloud.google.com/storage/docs/request-endpoints),
[generation preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions),
[Bucket Lock retention limits](https://docs.cloud.google.com/storage/docs/bucket-lock),
[public access prevention](https://docs.cloud.google.com/storage/docs/public-access-prevention),
and [Object Versioning behavior](https://docs.cloud.google.com/storage/docs/object-versioning).
