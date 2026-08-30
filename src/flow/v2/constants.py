"""Flow 2.0 constants."""

from __future__ import annotations

EX_USAGE = 64
EX_DATAERR = 65
EX_RUNTIME = 70
EX_NEEDS_HELP = 75
EX_SIGINT = 130
EX_SIGTERM = 143

FLOW_EXIT_MIN = 0
FLOW_EXIT_MAX = 63
SCHEMA_VERSION = 2
METADATA_START = "<!-- flow:metadata"
METADATA_END = "-->"

IMPLICIT_KEEP_WORKING = "keep-working"
IMPLICIT_NEEDS_HELP = "needs-help"
IMPLICIT_FINISH = "finish"
RESERVED_STATE_NAMES = {IMPLICIT_FINISH, IMPLICIT_KEEP_WORKING, IMPLICIT_NEEDS_HELP}

VALID_MODES = {"yolo", "full-auto", "workspace-write", "danger-full-access"}
VALID_THINKING = {"none", "minimal", "low", "medium", "high", "xhigh"}
