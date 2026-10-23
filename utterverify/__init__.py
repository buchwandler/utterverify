"""UtterVerify public API."""

try:
    from ._version import __version__
except ImportError:
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("utterverify")
    except PackageNotFoundError:
        __version__ = "0.1.dev0"

from .bundle import init_bundle_config, verify_bundle
from .config import VerifyConfig, ensure_config, load_config, write_config
from .core import verify_renderplan
from .types import BundleVerifyResult, SegmentOutcome, UnitVerifyResult

__all__ = [
    "BundleVerifyResult",
    "SegmentOutcome",
    "UnitVerifyResult",
    "VerifyConfig",
    "__version__",
    "ensure_config",
    "init_bundle_config",
    "load_config",
    "verify_bundle",
    "verify_renderplan",
    "write_config",
]
