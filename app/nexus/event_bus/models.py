from __future__ import annotations
import hashlib, json, uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@dataclass
class NexusEvent:
    event_type: str
    source: str
    payload: dict[str, Any]
    entity_type: str = ""
    entity_id: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    actor: str = "system"
    schema_version: int = 1
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=utc_now)

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        canon = json.dumps({"event_type": self.event_type, "entity_id": self.entity_id, "payload": self.payload, "occurred_at": self.occurred_at}, sort_keys=True, default=str)
        d["event_hash"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        d["payload_json"] = json.dumps(self.payload, ensure_ascii=False, default=str)
        return d
