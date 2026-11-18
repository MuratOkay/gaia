"""Runs that run themselves (Phase 7C; P5-12, P7-6, P7-7; D98).

``queue`` (jobs in the metadata store, claimed with ``SKIP LOCKED``), ``schedules`` (daily or
weekly), ``signal`` (when work must stop: cancellation, the run window, the token budget),
``kinds`` (refresh, drift watch, the calibration sample), ``worker`` (``make worker``).
"""
