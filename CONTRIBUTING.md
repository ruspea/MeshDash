# Contributing to MeshDash

First off, thanks for taking the time to contribute. MeshDash is built by operators, for operators.

## Plugin Development

The easiest way to extend MeshDash is through the plugin system — no core modifications needed.

- [Plugin Development Guide](https://meshdash.co.uk/docs/?page=plugin-development)
- Drop your plugin folder into `plugins_core/` and it's live

## Code Contributions

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes
4. Push to your fork
5. Open a Pull Request

### Code Style

- **Python:** Follow PEP 8. Use type hints where practical.
- **JavaScript:** Consistent with existing codebase. Use `const`/`let`, not `var`.
- **PHP:** Follow PSR-12.

### Pull Request Guidelines

- One feature per PR. Keep it focused.
- Include a clear description of what the PR does and why.
- If changing API behaviour, document the changes.

## Reporting Issues

- Use [GitHub Issues](https://github.com/ruspea/MeshDash/issues)
- Include MeshDash version, OS, Python version, and radio connection type
- Logs help — redact any tokens or passwords before posting

## Security Vulnerabilities

Do not report security issues publicly. Email info@meshdash.co.uk with details.

## License

By contributing, you agree that your contributions will be licensed under GPL-3.0-only.
