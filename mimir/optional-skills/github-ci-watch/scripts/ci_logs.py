"""Compatibility imports for installed CI-watch scripts and existing consumers."""
from mimir.ci_logs import LOG_EXCERPT_BYTES, capture_job_log, clean_log_tail

__all__ = ["LOG_EXCERPT_BYTES", "capture_job_log", "clean_log_tail"]
