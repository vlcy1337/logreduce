"""Exception types used by logReduce."""

from __future__ import annotations


class LogReduceError(Exception):
    """Base class for all expected logReduce errors."""


class ConfigError(LogReduceError):
    """The configuration file is invalid."""


class DiscoveryError(LogReduceError):
    """Input files could not be discovered safely."""


class ProcessingError(LogReduceError):
    """A file could not be processed."""


class MergeError(LogReduceError):
    """Processed files could not be merged."""
