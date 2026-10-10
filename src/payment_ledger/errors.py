"""Application failures with explicit public meanings."""


class AuthenticationFailed(Exception):
    pass


class AlreadyExists(Exception):
    pass


class ResourceNotFound(Exception):
    pass


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Authentication rate limit exceeded")
