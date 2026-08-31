from typing import Optional

from pydantic import BaseModel


class PingRow(BaseModel):
    ts: float
    target: str
    sent: int
    received: int
    loss_pct: float
    min_ms: Optional[float] = None
    avg_ms: Optional[float] = None
    max_ms: Optional[float] = None
    mdev_ms: Optional[float] = None


class SpeedRow(BaseModel):
    ts: float
    download_mbps: Optional[float] = None
    upload_mbps: Optional[float] = None
    idle_latency_ms: Optional[float] = None
    loaded_latency_avg_ms: Optional[float] = None
    loaded_latency_max_ms: Optional[float] = None
    server_name: Optional[str] = None
    error: Optional[str] = None


class ModemRow(BaseModel):
    ts: float
    direction: str
    channel_id: Optional[str] = None
    port_id: Optional[str] = None
    frequency: Optional[str] = None
    modulation: Optional[str] = None
    signal_strength_dbmv: Optional[float] = None
    snr_db: Optional[float] = None
    corrected: Optional[int] = None
    uncorrectable: Optional[int] = None
    error: Optional[str] = None


class HttpRow(BaseModel):
    ts: float
    url: str
    status_code: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class IngestPayload(BaseModel):
    ping: list[PingRow] = []
    speed: list[SpeedRow] = []
    modem: list[ModemRow] = []
    http: list[HttpRow] = []
