"""QR ticket reading and validation."""

from app.ticket.qr_scanner import QRDetection, QRScanner, extract_ticket_id
from app.ticket.ticket_validator import (
    DEFAULT_TICKETS_PATH,
    TicketRecord,
    TicketValidator,
    ValidationResult,
    normalize_ticket_id,
)

__all__ = [
    "QRDetection",
    "QRScanner",
    "extract_ticket_id",
    "DEFAULT_TICKETS_PATH",
    "TicketRecord",
    "TicketValidator",
    "ValidationResult",
    "normalize_ticket_id",
]
