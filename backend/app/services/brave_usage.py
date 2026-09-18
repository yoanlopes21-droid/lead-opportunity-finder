"""Durable local usage, estimates, and hard caps for Brave Search requests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.models import BraveUsageEvent


HISTORICAL_ADJUSTMENT_KEY = "brave_initial_pilot_2026_09_18"
HISTORICAL_PILOT_AT = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


class BraveBudgetExceeded(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class BraveBudgetPolicy:
    monthly_request_budget: int = 1000
    default_run_hard_cap: int = 40
    estimated_price_per_1000_usd: Decimal = Decimal("5.0")
    estimated_monthly_free_credit_usd: Decimal = Decimal("5.0")

    def __post_init__(self) -> None:
        if self.monthly_request_budget < 1 or self.default_run_hard_cap < 1:
            raise ValueError("Brave request caps must be positive")
        if self.estimated_price_per_1000_usd < 0 or self.estimated_monthly_free_credit_usd < 0:
            raise ValueError("Brave estimated prices must not be negative")


@dataclass(frozen=True)
class BraveBudgetSnapshot:
    monthly_budget: int
    monthly_used: int
    monthly_remaining: int
    percentage_used: Decimal
    estimated_cost_used_usd: Decimal
    estimated_credit_remaining_usd: Decimal
    # Counted requests are the local estimated provider consumption. Attempts
    # remain auditable separately because a connection can fail before Brave
    # could receive the request.
    project_total_requests: int
    project_total_attempts: int
    project_total_counted_requests: int
    current_period_start: datetime
    current_period_end: datetime
    days_remaining_in_period: int
    default_run_cap: int
    maximum_allowed_for_next_run: int
    pacing_per_day: Decimal
    status: str


def ensure_brave_usage_schema(engine: Engine) -> None:
    BraveUsageEvent.__table__.create(bind=engine, checkfirst=True)


class BraveUsageService:
    """Reserve a counted slot before a real request is dispatched."""

    def __init__(self, session: Session, policy: BraveBudgetPolicy, *, now=lambda: datetime.now(timezone.utc)) -> None:
        self.session = session
        self.policy = policy
        self.now = now

    def reserve_request(
        self, *, run_id: Optional[int], company_key: Optional[str], query: str,
        request_index: Optional[int], run_hard_cap: Optional[int] = None,
    ) -> BraveUsageEvent:
        # SQLite is the production store for this application. Its immediate
        # write transaction serializes the check-and-reserve operation across
        # sessions, so two concurrent requests cannot both pass the same cap.
        # ``commit`` preserves the service's existing commit-on-reservation
        # semantics while ending any read transaction opened by the caller.
        if self.session.bind is not None and self.session.bind.dialect.name == "sqlite":
            if self.session.in_transaction():
                self.session.commit()
            self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        observed_at = _utc(self.now())
        period = _period(observed_at)
        try:
            monthly_used = self._used(period)
            if monthly_used >= self.policy.monthly_request_budget:
                raise BraveBudgetExceeded("monthly_budget_exhausted")
            cap = run_hard_cap or self.policy.default_run_hard_cap
            if run_id is not None and self._used(period, run_id=run_id) >= cap:
                raise BraveBudgetExceeded("run_budget_exhausted")
            event = BraveUsageEvent(
                observed_at=observed_at, billing_period=period, run_id=run_id,
                company_key=company_key, query_fingerprint=_fingerprint(query), request_index=request_index,
                outcome="reserved", counted_for_budget=True, source="live_request", quantity=1,
            )
            self.session.add(event)
            # Reservation must survive a dispatch failure and be visible to another
            # run before the HTTP client is called.
            self.session.commit()
            return event
        except Exception:
            self.session.rollback()
            raise

    def record_outcome(
        self, event_id: int, outcome: str, *, counted_for_budget: Optional[bool] = None,
    ) -> None:
        event = self.session.get(BraveUsageEvent, event_id)
        if event is not None:
            event.outcome = _safe_outcome(outcome)
            if counted_for_budget is not None:
                event.counted_for_budget = counted_for_budget
            self.session.commit()

    def reconcile_pre_dispatch_failures(self, event_ids: tuple[int, ...], *, note: str) -> int:
        """Keep confirmed pre-provider attempts but remove their estimated cost.

        This is an explicit reconciliation path for a past run where external
        provider billing establishes that a transport failure happened before
        dispatch. It never deletes the audit events or alters history.
        """
        if not event_ids:
            return 0
        events = list(self.session.scalars(select(BraveUsageEvent).where(
            BraveUsageEvent.id.in_(event_ids),
            BraveUsageEvent.source == "live_request",
        )))
        for event in events:
            event.outcome = "connection_failed_pre_dispatch"
            event.counted_for_budget = False
            event.note = note[:500]
        self.session.commit()
        return len(events)

    def seed_initial_history(self) -> bool:
        if self.session.scalar(select(BraveUsageEvent.id).where(
            BraveUsageEvent.adjustment_key == HISTORICAL_ADJUSTMENT_KEY
        )) is not None:
            return False
        self.session.add(BraveUsageEvent(
            observed_at=HISTORICAL_PILOT_AT, billing_period=_period(HISTORICAL_PILOT_AT),
            run_id=None, company_key=None, query_fingerprint=None, request_index=None,
            outcome="historical_initial_pilot", counted_for_budget=True,
            source="historical_adjustment", quantity=14,
            adjustment_key=HISTORICAL_ADJUSTMENT_KEY,
            note="Initial Brave pilot: 14 successful searches.",
        ))
        self.session.commit()
        return True

    def snapshot(self, *, at: Optional[datetime] = None) -> BraveBudgetSnapshot:
        observed_at = _utc(at or self.now())
        period = _period(observed_at)
        used = self._used(period)
        remaining = max(0, self.policy.monthly_request_budget - used)
        cost = _money(Decimal(used) * self.policy.estimated_price_per_1000_usd / Decimal(1000))
        credit = max(Decimal("0"), self.policy.estimated_monthly_free_credit_usd - cost)
        start, end = _period_bounds(observed_at)
        days = max(1, (end.date() - observed_at.date()).days + 1)
        percentage = _money(Decimal(used) * Decimal(100) / Decimal(self.policy.monthly_request_budget))
        if remaining == 0:
            status = "exhausted"
        elif percentage >= Decimal("90"):
            status = "low"
        elif percentage >= Decimal("70"):
            status = "elevated"
        else:
            status = "healthy"
        return BraveBudgetSnapshot(
            monthly_budget=self.policy.monthly_request_budget, monthly_used=used,
            monthly_remaining=remaining, percentage_used=percentage,
            estimated_cost_used_usd=cost, estimated_credit_remaining_usd=_money(credit),
            project_total_requests=self._used(None),
            project_total_attempts=self._attempts(None),
            project_total_counted_requests=self._used(None), current_period_start=start,
            current_period_end=end, days_remaining_in_period=days,
            default_run_cap=self.policy.default_run_hard_cap,
            maximum_allowed_for_next_run=min(remaining, self.policy.default_run_hard_cap),
            pacing_per_day=_money(Decimal(remaining) / Decimal(days)), status=status,
        )

    def _used(self, period: Optional[str], *, run_id: Optional[int] = None) -> int:
        statement = select(func.coalesce(func.sum(BraveUsageEvent.quantity), 0)).where(
            BraveUsageEvent.provider == "brave_search", BraveUsageEvent.counted_for_budget.is_(True),
        )
        if period is not None:
            statement = statement.where(BraveUsageEvent.billing_period == period)
        if run_id is not None:
            statement = statement.where(BraveUsageEvent.run_id == run_id)
        return int(self.session.scalar(statement) or 0)

    def _attempts(self, period: Optional[str]) -> int:
        statement = select(func.coalesce(func.sum(BraveUsageEvent.quantity), 0)).where(
            BraveUsageEvent.provider == "brave_search",
        )
        if period is not None:
            statement = statement.where(BraveUsageEvent.billing_period == period)
        return int(self.session.scalar(statement) or 0)


def _period(value: datetime) -> str:
    return _utc(value).strftime("%Y-%m")


def _period_bounds(value: datetime) -> tuple[datetime, datetime]:
    value = _utc(value)
    start = value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1) - timedelta(microseconds=1)
    else:
        end = start.replace(month=start.month + 1) - timedelta(microseconds=1)
    return start, end


def _fingerprint(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _safe_outcome(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)[:80] or "unknown"


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
