"""独立选题中心服务的错误类型。"""

from __future__ import annotations


class MigrationError(RuntimeError):
    """可安全返回给 HTTP 客户端的业务错误。"""

    code = "migration_error"
    http_status = 500
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable


class ValidationError(MigrationError):
    code = "invalid_input"
    http_status = 422


class NotFoundError(MigrationError):
    code = "not_found"
    http_status = 404


class NotMigratedError(MigrationError):
    code = "not_migrated"
    http_status = 501


class ProviderError(MigrationError):
    code = "provider_error"
    http_status = 502
    retryable = True


class PersistenceError(MigrationError):
    code = "persistence_error"
    http_status = 503
