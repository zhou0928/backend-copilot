# Privacy Policy

Backend Copilot runs entirely within your own Dify instance.

## What data is processed
- The API catalog (YAML) you provide, including endpoint definitions.
- Credentials (tokens, usernames/passwords, captcha codes) you configure — stored by Dify's credential storage and only used to sign requests to YOUR backend.
- Request/response payloads exchanged between the plugin and your backend at runtime.

## What is NOT collected
- No telemetry, analytics, or data leaves your infrastructure. All HTTP requests go directly from the plugin to the backend base_url you configured.

## Security
- Write operations are blocked by default (directory `write: true` + credential `allow_write` both required).
- Use HTTPS base_urls and scoped credentials in production.
