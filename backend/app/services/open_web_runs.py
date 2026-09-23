"""Persistent, bounded Open Web discovery orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BraveUsageEvent, CollectionRun
from app.services.brave_usage import BraveBudgetPolicy, BraveUsageService
from app.services.collection.open_web import (
    BRAVE_DISCOVERY_PROVIDER,
    CachedSearchClient,
    DiscoveryQueryPlan,
    OpenWebJobDiscoveryProvider,
)
from app.services.collection.providers import JobOfferProviderCollector
from app.services.contactability.providers.official_web.brave_client import BraveSearchClient
from app.services.persistence.offers import CollectionRunStatus, create_collection_run


class OpenWebRunError(ValueError):
    pass


def create_open_web_run(
    session: Session,
    settings: Settings,
    *,
    target_signal_count: int,
    brave_max_requests: int,
) -> tuple[CollectionRun, bool]:
    if settings.brave_search_api_key is None or not settings.brave_search_api_key.get_secret_value().strip():
        raise OpenWebRunError("Brave Search n’est pas configuré localement.")
    active = session.scalar(select(CollectionRun).where(
        CollectionRun.source == BRAVE_DISCOVERY_PROVIDER,
        CollectionRun.scope_type == "department",
        CollectionRun.scope_value == "94",
        CollectionRun.status.in_((CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)),
    ).order_by(CollectionRun.id.desc()))
    if active is not None:
        return active, False
    usage = BraveUsageService(session, _policy(settings)).snapshot()
    available = max(0, usage.monthly_remaining - max(0, settings.brave_search_monthly_reserve))
    allowed_cap = min(brave_max_requests, available)
    if allowed_cap < 1:
        raise OpenWebRunError("Budget Brave indisponible après application de la réserve mensuelle.")
    run = create_collection_run(session, BRAVE_DISCOVERY_PROVIDER, "department", "94")
    run.status = CollectionRunStatus.QUEUED
    run.target_signal_count = target_signal_count
    run.brave_hard_cap = allowed_cap
    session.commit()
    return run, True


def run_open_web_discovery(session: Session, run_id: int, settings: Settings) -> None:
    run = session.get(CollectionRun, run_id)
    if run is None or run.status not in {CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING}:
        return
    before = _run_brave_usage(session, run_id)
    try:
        usage = BraveUsageService(session, _policy(settings))
        client = BraveSearchClient(
            api_key=settings.brave_search_api_key.get_secret_value(),
            usage_service=usage,
            base_url=settings.brave_search_api_url,
            timeout_seconds=settings.brave_search_timeout_seconds,
            requests_per_second=settings.brave_search_requests_per_second,
            run_hard_cap=run.brave_hard_cap,
        )
        client.set_run_id(run.id)
        cached = CachedSearchClient(session, client, ttl=timedelta(hours=24))

        def should_stop() -> bool:
            session.refresh(run, attribute_names=["stop_requested"])
            return bool(run.stop_requested)

        provider = OpenWebJobDiscoveryProvider(
            cached,
            DiscoveryQueryPlan(max_queries=run.brave_hard_cap or 1),
            max_signals=run.target_signal_count,
            should_stop=should_stop,
        )
        JobOfferProviderCollector(provider).collect(session, run=run)
        session.refresh(run)
        run.completion_reason = (
            "stopped_by_user" if run.stop_requested
            else "target_reached" if run.target_signal_count and run.signals_found >= run.target_signal_count
            else "query_plan_completed"
        )
    except Exception as exc:
        session.rollback()
        failed = session.get(CollectionRun, run_id)
        if failed is not None:
            failed.error_summary = failed.error_summary or (str(exc).strip() or "Open Web a échoué.")[:1000]
    finally:
        current = session.get(CollectionRun, run_id)
        if current is not None:
            current.brave_requests_used = max(0, _run_brave_usage(session, run_id) - before)
            session.commit()


def request_open_web_stop(session: Session, run: CollectionRun) -> CollectionRun:
    if run.status not in {CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING}:
        raise OpenWebRunError("Ce run Open Web est déjà terminé.")
    run.stop_requested = True
    session.commit()
    session.refresh(run)
    return run


def recover_orphaned_open_web_runs(session: Session) -> int:
    runs = session.scalars(select(CollectionRun).where(
        CollectionRun.source == BRAVE_DISCOVERY_PROVIDER,
        CollectionRun.scope_type == "department",
        CollectionRun.status.in_((CollectionRunStatus.QUEUED, CollectionRunStatus.RUNNING)),
    )).all()
    for run in runs:
        run.status = CollectionRunStatus.FAILED
        run.finished_at = datetime.now(timezone.utc)
        run.error_summary = "Le scan Open Web a été interrompu par l’arrêt du serveur."
    if runs:
        session.commit()
    return len(runs)


def _policy(settings: Settings) -> BraveBudgetPolicy:
    return BraveBudgetPolicy(
        monthly_request_budget=settings.brave_search_monthly_request_budget,
        default_run_hard_cap=settings.brave_search_default_run_hard_cap,
        estimated_price_per_1000_usd=settings.brave_search_estimated_price_per_1000_usd,
        estimated_monthly_free_credit_usd=settings.brave_search_estimated_monthly_free_credit_usd,
    )


def _run_brave_usage(session: Session, run_id: int) -> int:
    return int(session.scalar(select(func.coalesce(func.sum(BraveUsageEvent.quantity), 0)).where(
        BraveUsageEvent.provider == BRAVE_DISCOVERY_PROVIDER,
        BraveUsageEvent.run_id == run_id,
        BraveUsageEvent.counted_for_budget.is_(True),
    )) or 0)
