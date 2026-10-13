"""Delta India data adapter; DOES NOT alter or execute Optopsy strategies."""
from .client import DeltaClient, DeltaError, SignatureError, sign_message
from .snapshot import SnapshotError, snapshot_to_rows, write_snapshot
from .policy import CapitalPolicy, CapitalPolicyError

__all__ = [
    "DeltaClient", "DeltaError", "SignatureError", "sign_message",
    "SnapshotError", "snapshot_to_rows", "write_snapshot",
    "CapitalPolicy", "CapitalPolicyError",
]
