class AliceBlueError(Exception):
    """Base exception for all Alice Blue API interactions."""
    pass


class IPNotWhitelistedError(AliceBlueError):
    """Raised when Alice Blue rejects an API call due to IP restriction / mismatch."""
    def __init__(self, account_id: str, ip: str, message: str = "IP address is not whitelisted by broker"):
        super().__init__(f"Account {account_id}: {message} (IP: {ip})")
        self.account_id = account_id
        self.ip = ip


class AuthenticationError(AliceBlueError):
    """Raised when authentication credentials or token generation fails."""
    def __init__(self, account_id: str, message: str):
        super().__init__(f"Account {account_id} Auth Failed: {message}")
        self.account_id = account_id


class SessionExpiredError(AuthenticationError):
    """Raised when an active session has expired and requires re-login."""
    pass


class RateLimitExceededError(AliceBlueError):
    """Raised when Alice Blue API rate limit is exceeded."""
    pass


class BrokerOrderError(AliceBlueError):
    """Raised when an order placement or modification is rejected by the broker."""
    def __init__(self, account_id: str, reason: str, broker_response: dict | None = None):
        super().__init__(f"Order failed for account {account_id}: {reason}")
        self.account_id = account_id
        self.reason = reason
        self.broker_response = broker_response or {}
