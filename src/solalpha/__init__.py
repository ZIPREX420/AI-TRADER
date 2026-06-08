"""solalpha — production-grade Solana alpha trading system."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("solalpha")
except PackageNotFoundError:  # editable install before metadata exists
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
