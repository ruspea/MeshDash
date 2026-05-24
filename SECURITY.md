# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| R3.0 | ✅ |
| R2.x | ⚠️ Security fixes only |
| R1.x | ❌ End of life |

## Reporting a Vulnerability

**Do not report security vulnerabilities through public GitHub issues.**

Email: **info@meshdash.co.uk**

Include:
- Description of the vulnerability
- Steps to reproduce
- Affected version
- Any mitigation you've identified

You should receive a response within 48 hours. If the vulnerability is confirmed, we'll work on a fix and coordinate disclosure with you.

## Security Features

MeshDash implements:
- JWT tokens in HttpOnly, SameSite cookies
- Bcrypt password hashing with automatic salt generation
- CSRF double-submit cookie protection
- Optional TOTP two-factor authentication
- Rate limiting on authentication endpoints
- HMAC-signed remote access (5 access tiers)

## Responsible Disclosure

We ask that you:
- Give us reasonable time to fix the issue before public disclosure
- Do not access or modify other users' data
- Do not degrade service availability
