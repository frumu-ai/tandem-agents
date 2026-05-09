# GitHub Container Registry (GHCR) Integration Guide

This guide explains the end-to-end process of setting up GitHub Container Registry (GHCR) for a project. It covers creating the package namespace manually, automating builds with GitHub Actions, and securely authenticating your servers so they can pull the images.

## Phase 1: Initialize the GHCR Package

To use GHCR seamlessly with Actions, you typically need to push an initial image manually. This creates the package entity in GitHub and allows you to properly link it to your repository so Actions can write to it.

1. **Create a Personal Access Token (PAT)**
   - Go to [GitHub Developer Settings](https://github.com/settings/tokens).
   - Generate a new token with the `read:packages` and `write:packages` scopes.

2. **Log into GHCR locally**
   - Run the following in your terminal, replacing the placeholders:
     ```bash
     echo "YOUR_PAT_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
     ```

3. **Build and Tag Your Initial Image**
   - Whatever your project name or image name is, tag it targeting the `ghcr.io` namespace:
     ```bash
     # Format: ghcr.io/<org-or-username>/<repo-name>/<image-name>:<tag>
     docker build -t ghcr.io/my-organization/my-project/api:initial .
     ```

4. **Push the Image**
   - Push the initially tagged image:
     ```bash
     docker push ghcr.io/my-organization/my-project/api:initial
     ```

> [!IMPORTANT]
> **Link the Package to Your Repository**
> Once pushed, go to the Packages tab on your GitHub profile/organization. Select the newly created package, click **Package Settings**, and under **Manage Actions access**, add your repository and grant it **Write** access. If you skip this, GitHub Actions will get a `Permission Denied` error.

---

## Phase 2: Automating Pushes (GitHub Actions)

Once the package namespace exists and repository write permissions are granted, GitHub Actions can automate pushing Docker images natively without requiring your PAT.

### 1. Enable Workflow Permissions
Go to your repository **Settings → Actions → General**. Scroll down to **Workflow permissions** and ensure **Read and write permissions** is selected.

### 2. Create the GitHub Action
Create a new file at `.github/workflows/build-and-push.yml` inside your repository. Here is a generic boilerplate you can adapt to any image type:

```yaml
name: Build and Push Docker Image

on:
  push:
    branches: [ 'main' ]
  # Optional: Also run on version tags (e.g., v1.0.0)
  # tags: [ 'v*' ]

env:
  REGISTRY: ghcr.io
  # Example: org-name/repo-name/component
  IMAGE_NAME: ${{ github.repository }}/app-name

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write   # Absolutely required for GHCR token access
      
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          # Uses the native, built-in repository token
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Extract metadata (tags, labels)
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}
          tags: |
            type=sha,prefix={{branch}}-
            type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          # Change context if your Dockerfile is not in the root directory
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

---

## Phase 3: Server Authentication & Deployment

If your GHCR package is private, your deployment servers (either via direct SSH, Ansible, Docker Swarm, Kubernetes, etc.) must authenticate before pulling the images.

### Server Authentication Flow

Your target servers need your GitHub username and a Personal Access Token (`read:packages` scope) to pull from GHCR.

**Example using Ansible:**
When automating deployments with Ansible, pass the credentials securely (e.g. from environment variables on your control node) rather than hardcoding them.

Provide a playbook `docker-registry-login.yml`:

```yaml
---
- name: Configure Docker Registry Authentication
  hosts: my_servers
  vars:
    # Safely inject secrets from local environment
    github_username: "{{ lookup('env', 'GITHUB_USERNAME') }}"
    github_token: "{{ lookup('env', 'GITHUB_TOKEN') }}"
    docker_registry: "ghcr.io"
    
  tasks:
    - name: Validate GitHub credentials exist
      fail:
        msg: "GITHUB_USERNAME and GITHUB_TOKEN environment variables must be set locally"
      when: github_username == "" or github_token == ""

    - name: Login to GitHub Container Registry
      command: >
        docker login {{ docker_registry }}
        --username {{ github_username }}
        --password-stdin
      args:
        stdin: "{{ github_token }}"
      no_log: true  # CRITICAL: Prevent the token from appearing in Ansible logs!
      
    - name: Verify Docker login
      command: docker info
      changed_when: false
```

### Typical Deployment Pattern
1. Ensure the control machine running Ansible has `GITHUB_USERNAME` and `GITHUB_TOKEN` set.
2. Ansible connects to your servers.
3. Ansible runs the `docker login` command securely using the YAML above.
4. Ansible then triggers a `docker pull`, `docker run`, `docker service update`, or a customized compose deployment. Because the server is now authenticated with GHCR, the images will pull correctly.
