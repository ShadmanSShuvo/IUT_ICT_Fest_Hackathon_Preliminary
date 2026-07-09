"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def _round_half_up(cents: float) -> int:
    return int(cents + 0.5)


def log_refund(db: Session, booking: Booking, percent: int) -> RefundLog:
    amount_cents = _round_half_up(booking.price_cents * (percent / 100.0))
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
