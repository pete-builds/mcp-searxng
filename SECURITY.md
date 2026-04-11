# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please report it responsibly.

**Email:** security@brooksnewmedia.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Security Practices

- Secrets are loaded from environment variables, never hardcoded.
- `.env` files are excluded from version control via `.gitignore`.
- Dependencies are monitored with `pip-audit`.
