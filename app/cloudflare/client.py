"""Async Cloudflare API v4 client for DNS record management.

Never logs the API token. Every request is retried with backoff on HTTP 429
(honoring Retry-After if present) and HTTP 5xx; anything else that fails
raises CloudflareApiError with Cloudflare's error payload attached.
"""

import logging
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
MAX_ATTEMPTS = 5


class CloudflareApiError(Exception):
    """A non-retryable (or retries-exhausted) Cloudflare API failure."""

    def __init__(self, *, status_code: int, errors: list[dict[str, Any]], request_desc: str):
        self.status_code = status_code
        self.errors = errors
        super().__init__(f"Cloudflare API error on {request_desc}: HTTP {status_code} {errors}")


class _RetryableCloudflareError(Exception):
    """Internal signal that a request got HTTP 429 or 5xx and should be retried."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self.retry_after = _parse_retry_after(response)
        super().__init__(f"retryable Cloudflare error: HTTP {response.status_code}")


def _parse_retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _extract_errors(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except ValueError:
        return [{"message": response.text}]
    errors = body.get("errors") if isinstance(body, dict) else None
    return errors or [{"message": response.text}]


def _wait_for_retry(retry_state) -> float:
    exc = retry_state.outcome.exception()
    if isinstance(exc, _RetryableCloudflareError) and exc.retry_after is not None:
        return exc.retry_after
    return min(0.5 * (2 ** (retry_state.attempt_number - 1)), 30)


def _log_before_sleep(retry_state) -> None:
    exc = retry_state.outcome.exception()
    status_code = exc.response.status_code if isinstance(exc, _RetryableCloudflareError) else "?"
    logger.warning(
        "Cloudflare API call attempt %s failed with HTTP %s, retrying",
        retry_state.attempt_number,
        status_code,
    )


def _raise_typed_error_after_retries(retry_state):
    exc = retry_state.outcome.exception()
    if isinstance(exc, _RetryableCloudflareError):
        response = exc.response
        raise CloudflareApiError(
            status_code=response.status_code,
            errors=_extract_errors(response),
            request_desc=f"{response.request.method} {response.request.url.path}",
        ) from exc
    raise exc


cloudflare_retry = retry(
    retry=retry_if_exception_type(_RetryableCloudflareError),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=_wait_for_retry,
    before_sleep=_log_before_sleep,
    retry_error_callback=_raise_typed_error_after_retries,
)


class CloudflareClient:
    def __init__(
        self,
        api_token: str | None = None,
        *,
        base_url: str = CLOUDFLARE_API_BASE,
        timeout: float = 15.0,
    ):
        token = api_token if api_token is not None else get_settings().cloudflare_api_token
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CloudflareClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @cloudflare_retry
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)

        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableCloudflareError(response)

        body = response.json()
        if not response.is_success or not body.get("success", False):
            raise CloudflareApiError(
                status_code=response.status_code,
                errors=body.get("errors") or [{"message": response.text}],
                request_desc=f"{method} {path}",
            )
        return body.get("result")

    async def list_dns_records(self, zone_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            result = await self._request(
                "GET",
                f"/zones/{zone_id}/dns_records",
                params={"page": page, "per_page": 100},
            )
            records.extend(result)
            if len(result) < 100:
                break
            page += 1
        return records

    async def get_dns_record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/zones/{zone_id}/dns_records/{record_id}")

    async def update_dns_record(
        self, zone_id: str, record_id: str, *, content: str, proxied: bool
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            json={"content": content, "proxied": proxied},
        )

    async def create_dns_record(
        self, zone_id: str, *, name: str, type: str, content: str, proxied: bool
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/zones/{zone_id}/dns_records",
            json={"name": name, "type": type, "content": content, "proxied": proxied},
        )

    async def delete_dns_record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
