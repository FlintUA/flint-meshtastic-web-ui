# Sentinel Security Journal

## 2026-03-30 - Sanitize OS Errors in API Responses
**Vulnerability:** Raw `OSError` exceptions during weather API key secret saving were converted to string and returned in JSON HTTP responses, exposing internal server filesystem paths and OS permissions details to clients.
**Learning:** Returning stringified exceptions directly in error responses inadvertently exposes system internals such as absolute directory structure and file permissions.
**Prevention:** Log system/OS exceptions on the server side with `print`/logger and return sanitized generic error messages in API responses.
