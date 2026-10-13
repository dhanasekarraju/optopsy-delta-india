"""Delta India data adapter; DOES NOT alter or execute Optopsy strategies."""
from .client import DeltaClient, DeltaError, SignatureError, sign_message
from .policy import CapitalPolicy, CapitalPolicyError
from .snapshot import SnapshotError, snapshot_to_rows, write_snapshot

__all__ = [
    "CapitalPolicy",
    "CapitalPolicyError",
    "DeltaClient",
    "DeltaError",
    "SignatureError",
    "SnapshotError",
    "sign_message",
    "snapshot_to_rows",
    "write_snapshot",
]
