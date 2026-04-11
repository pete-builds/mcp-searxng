"""Exception hierarchy for mcp-searxng."""


class SearxngError(Exception):
    """Base exception for all SearXNG operations."""


class SearxngConnectionError(SearxngError):
    """Network-level failure: timeout, DNS, connection refused."""

    def __init__(self, url: str, detail: str = ""):
        self.url = url
        super().__init__(
            f"Connection failed for {url}: {detail}" if detail else f"Connection failed for {url}"
        )


class SearxngAPIError(SearxngError):
    """SearXNG returned a non-2xx HTTP status."""

    def __init__(self, status_code: int, url: str, detail: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(
            f"HTTP {status_code} from {url}: {detail}"
            if detail
            else f"HTTP {status_code} from {url}"
        )


class SearxngParseError(SearxngError):
    """Response body could not be decoded or was missing expected structure."""

    def __init__(self, url: str, detail: str = ""):
        self.url = url
        super().__init__(
            f"Invalid response from {url}: {detail}" if detail else f"Invalid response from {url}"
        )
