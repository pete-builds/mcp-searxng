"""Health check script for Docker HEALTHCHECK."""

import sys
import urllib.error
import urllib.request


def check():
    try:
        resp = urllib.request.urlopen("http://localhost:3702/sse", timeout=5)
        # SSE endpoint returns 200 with text/event-stream
        if resp.status == 200:
            sys.exit(0)
    except (urllib.error.URLError, OSError):
        pass
    sys.exit(1)


if __name__ == "__main__":
    check()
