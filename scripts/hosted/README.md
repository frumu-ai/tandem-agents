# Hosted Release Scripts

This directory contains build, publish, and deployment helpers for hosted Tandem
installations. Keep secrets and deployment-specific runtime artifacts out of this
directory; use local env files, `secrets/`, or the hosted control plane instead.

## Image Namespace

By default, the scripts derive the GHCR namespace from the current git remote.
In this repo that means:

```text
ghcr.io/frumu-ai/tandem-agents
```

The published image refs are:

```text
ghcr.io/frumu-ai/tandem-agents/engine:<tag>
ghcr.io/frumu-ai/tandem-agents/engine-enterprise:<tag>
ghcr.io/frumu-ai/tandem-agents/aca:<tag>
ghcr.io/frumu-ai/tandem-agents/aca-enterprise:<tag>
ghcr.io/frumu-ai/tandem-agents/tandem-control-panel:<tag>
ghcr.io/frumu-ai/tandem-agents/tandem-proxy:<tag>
ghcr.io/frumu-ai/tandem-agents/tandem-kb-mcp:<tag>
```

Override the namespace when needed:

```bash
export HOSTED_IMAGE_NAMESPACE=ghcr.io/frumu-ai/<repo-or-package>
```

Or split owner/repository:

```bash
export HOSTED_IMAGE_OWNER=frumu-ai
export HOSTED_IMAGE_REPOSITORY=tandem-agents
```

## Build Locally For Smoke Testing

```bash
./scripts/hosted/build-images.sh --load
./scripts/hosted/smoke-test.sh --skip-build
```

Or build as part of the smoke test:

```bash
./scripts/hosted/smoke-test.sh
```

## Publish Hosted Images

The normal local flow uses the same GitHub token file as ACA:

```bash
GITHUB_PERSONAL_ACCESS_TOKEN_FILE=./secrets/github_token
GITHUB_TOKEN_FILE=./secrets/github_token
```

Log in to GHCR with that token before publishing:

```bash
docker logout ghcr.io
cat "${GITHUB_PERSONAL_ACCESS_TOKEN_FILE:-./secrets/github_token}" \
  | docker login ghcr.io -u <github-user> --password-stdin
```

Then push the hosted images:

```bash
./scripts/hosted/publish-images.sh
```

Alternatively, set explicit registry login env vars and let the script login:

```bash
export HOSTED_REGISTRY_USERNAME=<github-user>
export HOSTED_REGISTRY_TOKEN=<github-token-with-packages-write>
./scripts/hosted/publish-images.sh
```

Equivalent explicit form:

```bash
./scripts/hosted/build-images.sh --push
```

To bump the fallback hosted release version before publishing images:

```bash
./scripts/hosted/publish-images.sh --auto-bump patch
```

Use `minor` or `major` instead of `patch` when needed.

## Publish Release Metadata

Publishing images only pushes GHCR tags. The hosted control plane also needs a
release metadata record.

The release publisher needs:

```bash
export HOSTED_RELEASE_PUBLISH_INTERNAL_BASE_URL=https://tandem.ac
export HOSTED_RELEASE_PUBLISH_TOKEN=<internal-release-publish-token>
```

If `../tandem-web/.env` exists, `publish-release.sh` can read those values from
there automatically.

Publish the current manifest as the stable, published release:

```bash
./scripts/hosted/publish-release.sh --channel stable --published true
```

Add release notes:

```bash
./scripts/hosted/publish-release.sh \
  --channel stable \
  --published true \
  --release-notes "Public tandem-agents hosted image release."
```

Preview the JSON payload without posting it:

```bash
./scripts/hosted/release-payload.sh --channel stable --published true
```

## Usual Release Sequence

```bash
./scripts/hosted/publish-images.sh --auto-bump patch
./scripts/hosted/publish-release.sh --channel stable --published true
```

If the release version is already correct:

```bash
cat "${GITHUB_PERSONAL_ACCESS_TOKEN_FILE:-./secrets/github_token}" \
  | docker login ghcr.io -u <github-user> --password-stdin
./scripts/hosted/publish-images.sh --no-bump
./scripts/hosted/publish-release.sh --channel stable --published true
```

## Bundle A Customer Deployment

```bash
./scripts/hosted/package-bundle.sh \
  --deployment-slug <deployment-slug> \
  --public-url https://<deployment-hostname>
```

The bundle contains rendered Compose, Caddy, control-panel config, bootstrap,
secret-generation, and release manifest files.

## Bootstrap A Server From A Bundle

On the target server:

```bash
./bootstrap-server.sh --bundle-dir /path/to/bundle
```

For cloud-init style bootstrapping:

```bash
./scripts/hosted/render-cloud-init.sh \
  --bundle-url https://<bucket-or-cdn>/<bundle>.tar.gz \
  --bundle-sha256 <sha256>
```

## Useful Inspection Commands

```bash
./scripts/hosted/release-manifest.sh
./scripts/hosted/release-payload.sh --channel stable --published true
./scripts/hosted/render-compose.sh
./scripts/hosted/render-runtime-env.sh --deployment-slug demo --public-url https://demo.example
./scripts/hosted/render-control-panel-config.sh --public-url https://demo.example
```
