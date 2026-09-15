# Markdown Renderer

The chat UI serves markdown-it 15.0.2 locally, without a CDN or Node.js build
step. Raw HTML and images are disabled. A small rule in `api.py` lets adjacent
tables render separately when the model omits a blank line.

Source: https://github.com/markdown-it/markdown-it

The browser bundle and MIT license are unmodified files from the npm package:

- Package: `https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.2.tgz`
- Package Integrity: `sha512-q4IGxMv56jCqT4OCRCADBoDP3LO4MhmTXjFbphHPXs4g3j9Xg5RDnxqN8IF/3vIWEU+VCnUq+7JUg/cfy2E6Qw==`
- Bundle SHA-256: `635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5`

To update, use `npm pack markdown-it@VERSION` and replace the bundle from
`package/dist/browser/markdown-it.umd.min.js` and license from `package/LICENSE`.
Update the version and checksums here, then run `pytest tests/test_web_markdown.py`
with Node.js available.
