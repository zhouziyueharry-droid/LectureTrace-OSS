# Security

## Supported scope

Security fixes are made on the current `main` branch. This project is intended for a single trusted user on a local computer. The backend binds to `127.0.0.1`; it has no multi-user authentication layer. Do not expose it through a public reverse proxy or network tunnel.

API keys are stored in the local backend database or environment, not a dedicated encrypted secrets vault. Protect your OS account, data directory and backups. Model requests can send selected course text and images to configured providers. Review each send plan before approving paid processing.

## Reporting a vulnerability

Use the repository's **Security → Advisories → Report a vulnerability** flow for private reports. Include affected versions, impact and a minimal synthetic reproduction. Do not publish working credentials, personal data or private course content in an issue. If private reporting is unavailable, open an issue requesting a private contact channel without exploit details or sensitive data.

This is a volunteer-maintained project without a guaranteed response-time SLA. There has not been a comprehensive external security audit. Automated dependency checks cover known advisories, not every possible vulnerability.

## Dependencies and content

Install from the committed lockfiles. CI checks the lightweight Python dependencies and high/critical npm advisories. Optional ASR/OCR/model installations need their own review. PDFs and course packages are untrusted inputs; report bypasses of parsing, path validation, size limits or isolation.
