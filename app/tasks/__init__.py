"""Arq workers for loupe-backend."""

from app.tasks.catalog_sync import catalog_sync
from app.tasks.scan_processor import arq_process_scan, process_scan

__all__ = ["arq_process_scan", "catalog_sync", "process_scan"]
