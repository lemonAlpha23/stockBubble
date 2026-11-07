"""Domain level error types.

Every error carries a stable ``code`` and an HTTP ``status`` so the API layer can
map failures without knowing which adapter raised them.
"""

from __future__ import annotations


class BubbleError(Exception):
    """Base class for every expected failure in the application."""

    code: str = "internal_error"
    status: int = 500

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_payload(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


class UniverseNotFound(BubbleError):
    code = "universe_not_found"
    status = 404


class UniverseConfigError(BubbleError):
    code = "universe_config_error"
    status = 500


class ProviderNotFound(BubbleError):
    code = "provider_not_found"
    status = 500


class UnsupportedWindow(BubbleError):
    code = "unsupported_window"
    status = 422


class UnsupportedSizeBasis(BubbleError):
    code = "unsupported_size_basis"
    status = 422


class ProviderUnavailable(BubbleError):
    """A remote data source failed (network, throttling, malformed payload)."""

    code = "provider_unavailable"
    status = 503

    def __init__(self, provider: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(f"[{provider}] {message}", hint=hint or "上游数据源暂时不可用，请稍后重试")
        self.provider = provider


class ProviderRateLimited(ProviderUnavailable):
    """The provider requested a cooldown before another attempt."""

    code = "provider_rate_limited"
    status = 429

    def __init__(self, provider: str, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(
            provider,
            "行情数据源请求频率受限",
            hint=f"请在 {self.retry_after} 秒后重试；切换时间范围也可能受到同一限流影响",
        )


class HistoryUnavailable(BubbleError):
    code = "history_unavailable"
    status = 502


class SymbolNotFound(BubbleError):
    code = "symbol_not_found"
    status = 404
