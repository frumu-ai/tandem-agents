# Third-Party Notices

This repository is licensed under the Business Source License 1.1. See
[LICENSE](LICENSE) for the terms that apply to Tandem Agents source code.

The project also depends on third-party software. Those dependencies remain
under their own licenses.

## Python Dependencies

Runtime Python dependencies are declared in [requirements.txt](requirements.txt)
and [pyproject.toml](pyproject.toml):

- PyYAML - MIT License
- packaging - Apache License 2.0 or BSD 2-Clause License
- psycopg and psycopg-binary - GNU Lesser General Public License v3 or later
  with exceptions
- tandem-client - see the package's published license metadata
- fastapi - MIT License
- uvicorn - BSD 3-Clause License
- sse-starlette - BSD 3-Clause License
- python-multipart - Apache License 2.0
- httpx - BSD 3-Clause License

## Container And Node Dependencies

The Docker images install and run third-party base images and npm packages:

- Python base images are governed by the license terms published with the
  corresponding Docker image.
- `caddy:2-alpine`, used by the hosted proxy image, is governed by Caddy and
  Alpine Linux component licenses.
- `@frumu/tandem` and `@frumu/tandem-panel` are separate Tandem packages and
  are governed by their own published license terms.

## Assets And Marks

The Tandem name, ACA name, Tandem logo, and ACA logo are trademarks or brand
assets of Frumu LTD. The source license does not grant permission to use those
marks except as expressly required to identify this project or preserve notices.

The file [assets/aca_logo.png](assets/aca_logo.png) is included for display in
this repository's documentation and project packaging. It is not licensed for
separate reuse as a standalone brand asset.
