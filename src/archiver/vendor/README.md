# Vendored Dependencies

## single-file-bundle.js

- **Source**: [single-file-cli](https://github.com/nicois/single-file-cli) npm package v2.0.83
- **License**: AGPL-3.0
- **Purpose**: Injected into browser pages via Playwright to capture self-contained HTML snapshots
- **How to update**: `npm pack single-file-cli && tar -xzf single-file-cli-*.tgz && cp package/lib/single-file-bundle.js src/archiver/vendor/`

This file is committed to the repo to avoid a runtime Node.js dependency.
