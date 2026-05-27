# Image Publishing

Tandem Agents publishes public/local images and hosted enterprise variants from
the same Dockerfiles.

Public images install the standard engine package:

- `ghcr.io/frumu-ai/tandem-agents/engine:<tag>`
- `ghcr.io/frumu-ai/tandem-agents/aca:<tag>`

Hosted images install the enterprise engine package:

- `ghcr.io/frumu-ai/tandem-agents/engine-enterprise:<tag>`
- `ghcr.io/frumu-ai/tandem-agents/aca-enterprise:<tag>`

Shared images are the same for public and hosted deployments:

- `ghcr.io/frumu-ai/tandem-agents/tandem-control-panel:<tag>`
- `ghcr.io/frumu-ai/tandem-agents/tandem-kb-mcp:<tag>`
- `ghcr.io/frumu-ai/tandem-agents/tandem-proxy:<tag>`

After the matching Tandem npm release has been published, run the `Publish
Images` workflow with:

```bash
gh workflow run publish-images.yml \
  -R frumu-ai/tandem-agents \
  -f tag=vX.Y.Z \
  -f tandem_release_version=X.Y.Z \
  -f registry=ghcr.io/frumu-ai/tandem-agents \
  -f push_latest=false
```

Hosted release registration in Tandem Web should use the enterprise refs:

```json
{
  "version": "X.Y.Z",
  "channel": "stable",
  "engine_image_ref": "ghcr.io/frumu-ai/tandem-agents/engine-enterprise:vX.Y.Z",
  "aca_image_ref": "ghcr.io/frumu-ai/tandem-agents/aca-enterprise:vX.Y.Z",
  "control_panel_image_ref": "ghcr.io/frumu-ai/tandem-agents/tandem-control-panel:vX.Y.Z",
  "proxy_image_ref": "ghcr.io/frumu-ai/tandem-agents/tandem-proxy:vX.Y.Z",
  "kb_image_ref": "ghcr.io/frumu-ai/tandem-agents/tandem-kb-mcp:vX.Y.Z",
  "manifest_json": {},
  "published": true
}
```
