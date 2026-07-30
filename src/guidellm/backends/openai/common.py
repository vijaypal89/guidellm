"""Shared helpers for OpenAI-compatible HTTP and WebSocket backends."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from pydantic import SecretStr

from guidellm.utils.imports import json

__all__ = [
    "ERROR_BODY_LIMIT",
    "FALLBACK_TIMEOUT",
    "UNIQUE_HEADER_PLACEHOLDERS",
    "OpenAIResponseError",
    "UniqueHeaderGenerator",
    "build_headers",
    "format_ws_error",
    "raise_for_status",
    "resolve_validate_kwargs",
    "validate_unique_header_templates",
]

# NOTE: This value is taken from httpx's default
FALLBACK_TIMEOUT = 5.0

UNIQUE_HEADER_PLACEHOLDERS = ("index", "seq", "worker", "uuid", "value")

# Enough of the server's body to identify a failure without bloating the report
ERROR_BODY_LIMIT = 400

# Rate limit rejections often have an empty body and explain themselves in headers
ERROR_DIAGNOSTIC_HEADERS = ("retry-after",)
ERROR_DIAGNOSTIC_HEADER_PREFIXES = ("x-ratelimit",)


class OpenAIResponseError(Exception):
    """
    An error response from an OpenAI-compatible server.

    Carries the HTTP status code as a field so benchmark reports can group
    failures by code (429 rate limited vs 500 server error) instead of parsing
    it back out of a message, and includes the response body, which is where
    these servers explain the failure.
    """

    def __init__(
        self,
        status_code: int,
        body: str = "",
        url: str = "",
        diagnostics: dict[str, str] | None = None,
    ):
        """
        :param status_code: HTTP status code returned by the server
        :param body: Response body, truncated to :data:`ERROR_BODY_LIMIT`
        :param url: Request URL that produced the error
        :param diagnostics: Response headers that explain the failure
        """
        self.status_code = status_code
        self.body = body
        self.url = url
        self.diagnostics = diagnostics or {}

        # The server's explanation leads and the URL trails, so that a report
        # truncating this message keeps the part that identifies the failure.
        reason = httpx.codes.get_reason_phrase(status_code)
        detail = f"HTTP {status_code} {reason}"
        if body:
            detail += f": {body}"
        if self.diagnostics:
            headers = " ".join(f"{k}={v}" for k, v in self.diagnostics.items())
            detail += f" [{headers}]"
        if url:
            detail += f" for {url}"
        super().__init__(detail)


def _diagnostic_headers(response: httpx.Response) -> dict[str, str]:
    """
    :param response: Response to inspect
    :return: Headers that explain the failure, such as rate limit budgets
    """
    return {
        name.lower(): value
        for name, value in response.headers.items()
        if name.lower() in ERROR_DIAGNOSTIC_HEADERS
        or name.lower().startswith(ERROR_DIAGNOSTIC_HEADER_PREFIXES)
    }


async def raise_for_status(response: httpx.Response) -> None:
    """
    Raise :class:`OpenAIResponseError` when a response carries an error status.

    Replaces ``httpx.Response.raise_for_status`` so the status code stays
    machine-readable and the server's explanation is captured. Streaming
    responses are read first, since the body has not been consumed at the point
    of failure.

    :param response: Response to check
    :raises OpenAIResponseError: If the response status is 4xx or 5xx
    """
    if not response.is_error:
        return

    try:
        body = response.text
    except httpx.ResponseNotRead:
        body = (await response.aread()).decode("utf-8", errors="replace")

    raise OpenAIResponseError(
        status_code=response.status_code,
        body=" ".join(body.split())[:ERROR_BODY_LIMIT],
        url=str(response.request.url),
        diagnostics=_diagnostic_headers(response),
    )


class UniqueHeaderGenerator:
    """
    Render per-request header values that stay unique across worker processes.

    Each backend instance lives in its own worker process, and the processes share
    no state, so a plain counter would hand out the same values in every process.
    Indices are instead strided by the worker count: worker ``w`` on its ``n``-th
    request yields ``n * workers + w``, which no other worker can produce. This
    makes every value in a run distinct no matter how the scheduler happens to
    distribute requests across processes.

    ``count`` additionally confines indices to ``[0, count)`` for drawing from a
    fixed pool of values. Because guidellm feeds workers from a shared queue,
    per-worker request counts are uneven, so a bounded run only stays collision
    free while every worker sends fewer than ``count / workers`` requests. Run
    with a single worker process (``GUIDELLM__MAX_WORKER_PROCESSES=1``) when a
    bounded pool must be exhausted exactly once.

    Example:
    ::
        generator = UniqueHeaderGenerator(
            templates={"X-RATELIMIT": "tenant-{index}"},
            workers=10,
        )
        headers = generator.next_headers(worker=3)
        # {"X-RATELIMIT": "tenant-3"}
    """

    def __init__(
        self,
        templates: dict[str, str],
        count: int | None = None,
        values: list[str] | None = None,
        workers: int = 1,
    ):
        """
        :param templates: Header name to value template, using the placeholders
            listed in :data:`UNIQUE_HEADER_PLACEHOLDERS`
        :param count: Size of the value space; indices wrap within it. Defaults to
            the length of ``values`` when those are given, otherwise unbounded
        :param values: Literal values selected by index for the ``{value}``
            placeholder
        :param workers: Number of worker processes sharing the value space
        """
        self.templates = templates
        self.values = values
        self.count = count if count is not None else (len(values) if values else None)
        self.workers = max(1, workers)
        self._sequences: dict[int, int] = {}

    def next_headers(self, worker: int = 0) -> dict[str, str]:
        """
        Render the next value for every configured header.

        :param worker: Rank of the worker process issuing the request
        :return: Header names mapped to freshly rendered values
        """
        worker = max(worker, 0) % self.workers
        sequence = self._sequences.get(worker, 0)
        self._sequences[worker] = sequence + 1
        index = self._next_index(worker, sequence)

        fields: dict[str, Any] = {
            "index": index,
            "seq": sequence,
            "worker": worker,
            "uuid": uuid.uuid4(),
        }
        if self.values:
            fields["value"] = self.values[index % len(self.values)]

        return {
            name: template.format(**fields) for name, template in self.templates.items()
        }

    def _next_index(self, worker: int, sequence: int) -> int:
        """
        :return: Index for the given worker rank and per-worker sequence number
        """
        # Stride by worker count so no two workers can produce the same index
        index = sequence * self.workers + worker

        return index if self.count is None else index % self.count


def validate_unique_header_templates(
    templates: dict[str, str] | None,
    has_values: bool,
) -> None:
    """
    Check that header templates only reference supported placeholders.

    :param templates: Header name to value template mapping
    :param has_values: Whether literal values are configured, which enables the
        ``{value}`` placeholder
    :raises ValueError: If a template references an unknown placeholder or uses
        malformed format syntax
    """
    if not templates:
        return

    probe: dict[str, Any] = {
        "index": 0,
        "seq": 0,
        "worker": 0,
        "uuid": uuid.uuid4(),
    }
    if has_values:
        probe["value"] = ""

    for name, template in templates.items():
        try:
            template.format(**probe)
        except (KeyError, IndexError, ValueError) as err:
            raise ValueError(
                f"Invalid template for header '{name}': {template!r}. Supported "
                f"placeholders are {', '.join(UNIQUE_HEADER_PLACEHOLDERS)} "
                "('{value}' requires values to be configured); escape literal "
                "braces as '{{' and '}}'. Error: "
                f"{err}"
            ) from err


def build_headers(
    api_key: SecretStr | str | None,
    existing_headers: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """
    Build headers with bearer authentication for OpenAI-compatible requests.

    Merges the Authorization bearer token (if ``api_key`` is set) with any
    existing headers. User-provided headers take precedence over the bearer token.

    :param api_key: Optional API key for Bearer authentication
    :param existing_headers: Optional headers to merge in
    :return: Headers dict, or ``None`` if there are no headers to send
    """
    headers: dict[str, str] = {}
    if api_key:
        if isinstance(api_key, SecretStr):
            api_key = api_key.get_secret_value()
        headers["Authorization"] = f"Bearer {api_key}"
    if existing_headers:
        headers = {**headers, **existing_headers}
    return headers or None


def resolve_validate_kwargs(
    validate_backend: bool | str | dict[str, Any],
    target: str,
    api_routes: dict[str, str],
) -> dict[str, Any] | None:
    """
    Build ``httpx`` request keyword arguments from backend validation settings.

    ``validate_backend`` may be ``False``/equivalent (skip validation), ``True``
    (default ``GET`` against the ``/health`` route key), a route key present in
    ``api_routes`` (resolved to ``{target}/{path}``), a full URL string, or a
    ``dict`` that includes ``url`` and optionally ``method`` (default ``GET``).

    :return: Keyword arguments suitable for ``httpx.AsyncClient.request``, or
        ``None`` when validation is turned off.
    """
    raw = validate_backend
    if not raw:
        return None

    if raw is True:
        raw = "/health"

    if isinstance(raw, str):
        url = f"{target}/{api_routes[raw]}" if raw in api_routes else raw
        request_kwargs: dict[str, Any] = {"method": "GET", "url": url}
    elif isinstance(raw, dict):
        request_kwargs = raw
    else:
        request_kwargs = raw

    if not isinstance(request_kwargs, dict) or "url" not in request_kwargs:
        raise ValueError(
            "validate_backend must be a boolean, string, or dictionary and contain "
            f"a target URL. Got: {request_kwargs}"
        )

    if "method" not in request_kwargs:
        request_kwargs["method"] = "GET"

    return request_kwargs


def format_ws_error(err: Any) -> str:
    """
    Format a WebSocket error payload into a human-readable message.

    :param err: Error value from a realtime ``error`` event frame.
    :return: Message suitable for :class:`RuntimeError`.
    """
    if isinstance(err, dict):
        msg = err.get("message") or err.get("msg")
        code = err.get("code")
        parts = [str(p) for p in (code, msg) if p]
        if parts:
            return ": ".join(parts)
        try:
            raw = json.dumps(err)
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return text[:500]
        except (TypeError, ValueError):
            return repr(err)
    if err is None or err == "":
        return "WebSocket error"
    return str(err)
