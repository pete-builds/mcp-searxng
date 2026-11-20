"""MCP server wrapping a self-hosted SearXNG instance."""

from importlib.metadata import PackageNotFoundError, version

# Read from the installed package metadata, so pyproject.toml stays the only
# place the version is written. The image installs the package with pip, so
# the metadata is always present there. The fallback only fires when running
# from a source tree that was never installed.
try:
    __version__ = version("mcp-searxng")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"
