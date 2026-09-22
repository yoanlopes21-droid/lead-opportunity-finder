"""Persistent orchestration for deliberate commercial searches.

Collection refresh and commercial discovery intentionally do not meet here:
this service only consumes persisted opportunities and contactability facts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.orm import Session

from app.models import BraveUsageEvent, ObservedJobOffer, SearchRun, SearchRunItem, WebsiteCandidateRecord
from app.services.commercial_leads.service import CommercialLead, CommercialLeadQuery, list_commercial_leads
from app.services.contactability.contracts import ContactType, VerificationStatus
from app.services.contactability.relevance import ChannelRelevance
from app.services.contactability.strategy import PreferredChannel
from app.services.contactability.providers.official_web.contracts import WebsiteVerificationStatus
from app.services.opportunities.company import normalize_company_key


class SearchRunStatus:
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class SearchRunItemStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    SKIPPED = "skipped"
    REUSED = "reused"
    ENRICHED = "enriched"
    ACTIONABLE = "actionable"
    UNRESOLVED = "unresolved"
    EXCLUDED = "excluded"
    ERROR = "error"


class SearchRunCompletionReason:
    TARGET_REACHED = "target_reached"
    CANDIDATES_EXHAUSTED = "candidates_exhausted"
    STOPPED = "stopped"
    FAILED = "failed"


_TERMINAL = {
    SearchRunItemStatus.SKIPPED, SearchRunItemStatus.REUSED,
    SearchRunItemStatus.ENRICHED, SearchRunItemStatus.ACTIONABLE,
    SearchRunItemStatus.UNRESOLVED, SearchRunItemStatus.EXCLUDED,
}


class SearchEnricher(Protocol):
    """One bounded company attempt. Implementations must never invoke Societe.com."""

    def enrich(self, session: Session, lead: CommercialLead, run: SearchRun) -> None:
        ...


class OfficialWebSearchEnricher:
    """Production adapter for official-web only; Societe.com is never imported or called."""

    def enrich(self, session: Session, lead: CommercialLead, run: SearchRun) -> None:
        # Imports stay local so unit tests can use an in-memory fake without
        # constructing HTTP clients or requiring optional credentials.
        from app.cli.contact_enrichment import official_web_provider
        from app.services.company_enrichment.contracts import MatchStatus
        from app.services.contactability.batch import ContactEnrichmentBatchOrchestrator
        from app.services.contactability.contracts import ContactScope
        from app.services.contactability.targets import ContactIdentityContext, build_contact_targets

        provider = official_web_provider(session, run_hard_cap=run.brave_hard_cap)
        targets = tuple(target for target in build_contact_targets(
            lead, ContactIdentityContext(match_status=(MatchStatus.HIGH_CONFIDENCE if lead.siren else None)),
        ) if target.scope in {ContactScope.COMPANY, ContactScope.INTERMEDIARY})
        if not targets:
            return
        provider.set_run_id(run.id)
        ContactEnrichmentBatchOrchestrator(provider).run(session, targets)


@dataclass(frozen=True)
class SearchRunProgress:
    id: int
    status: str
    department: str
    requested_actionable_leads: int
    current_actionable_leads: int
    candidates_considered: int
    candidates_enriched: int
    brave_requests_used: int
    brave_hard_cap: int
    current_company_key: Optional[str]
    current_company_name: Optional[str]
    current_step: Optional[str]
    completion_reason: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    error_summary: Optional[str]


def ensure_search_run_schema(engine: Engine) -> None:
    """Create additive tables and upgrade the legacy local SearchRun shape."""
    SearchRun.__table__.create(bind=engine, checkfirst=True)
    if engine.dialect.name == "sqlite":
        existing = {column["name"] for column in inspect(engine).get_columns("search_runs")}
        additions = {
            "department": "VARCHAR(3) DEFAULT '94'",
            "requested_actionable_leads": "INTEGER DEFAULT 25",
            "current_actionable_leads": "INTEGER DEFAULT 0",
            "candidates_considered": "INTEGER DEFAULT 0",
            "candidates_enriched": "INTEGER DEFAULT 0",
            "brave_requests_used": "INTEGER DEFAULT 0",
            "brave_hard_cap": "INTEGER DEFAULT 40",
            "stop_requested": "BOOLEAN DEFAULT 0",
            "error_summary": "VARCHAR(1000)",
            "configuration_snapshot": "JSON DEFAULT '{}'",
            "configuration_fingerprint": "VARCHAR(64)",
            "current_company_key": "VARCHAR(500)",
            "current_company_name": "VARCHAR(500)",
            "current_step": "VARCHAR(80)",
            "completion_reason": "VARCHAR(80)",
        }
        with engine.begin() as connection:
            for name, ddl in additions.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE search_runs ADD COLUMN {name} {ddl}")
    SearchRunItem.__table__.create(bind=engine, checkfirst=True)


def is_actionable_lead(lead: CommercialLead) -> bool:
    """The single commercial counting rule, deliberately stricter than visibility."""
    strategy = lead.contact_strategy
    if not lead.is_eligible or lead.active_offer_count < 1 or strategy is None:
        return False
    if strategy.preferred_channel == PreferredChannel.NONE:
        return False
    if strategy.channel_relevance not in {ChannelRelevance.RELEVANT, ChannelRelevance.NATIONAL_FRANCE}:
        return False
    point = next((item for item in lead.contactability.contact_points if item.id == strategy.contact_point_id), None)
    if point is None or not point.is_active:
        return False
    if point.verification_status in {VerificationStatus.REJECTED, VerificationStatus.STALE}:
        return False
    if point.confidence_level in {"review_needed", "ambiguous"}:
        return False
    # Provenance must belong to the selected channel, not merely to another
    # person or coordinate present on the same lead.
    return bool(lead.contactability.evidence_by_contact_point_id.get(point.id))


class SearchRunOrchestrator:
    def __init__(
        self, enricher: Optional[SearchEnricher] = None,
        *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._enricher = enricher
        self._clock = clock

    def create(
        self, session: Session, *, department: str = "94", requested_actionable_leads: int = 25,
        brave_hard_cap: int = 40,
    ) -> SearchRun:
        if requested_actionable_leads < 1 or requested_actionable_leads > 100:
            raise ValueError("requested_actionable_leads must be between 1 and 100")
        if brave_hard_cap < 0 or brave_hard_cap > 40:
            raise ValueError("brave_hard_cap must be between 0 and 40")
        snapshot = {"department": department, "requested_actionable_leads": requested_actionable_leads,
                    "brave_hard_cap": brave_hard_cap, "societe_com": "disabled"}
        run = SearchRun(
            status=SearchRunStatus.QUEUED, territory=department, department=department,
            parameters=snapshot, configuration_snapshot=snapshot,
            configuration_fingerprint=_fingerprint(snapshot),
            requested_actionable_leads=requested_actionable_leads, brave_hard_cap=brave_hard_cap,
        )
        session.add(run)
        session.flush()
        # Membership is durable for audit/resume, but web enrichment remains strictly
        # incremental in resume(), never an eager batch of all candidates.
        leads = list_commercial_leads(session, CommercialLeadQuery(
            department_code=department, include_excluded=True, limit=None,
        ), now=self._clock()).items
        candidate_keys = self._candidate_signal_keys(session)
        ordered = sorted(
            enumerate(leads),
            key=lambda row: (self._priority(row[1], candidate_keys), row[0]),
        )
        for position, (_, lead) in enumerate(ordered):
            session.add(SearchRunItem(
                run_id=run.id, company_key=lead.company_key, company_name_snapshot=lead.company_name,
                selection_position=position, score_snapshot=lead.scoring.total_score,
                input_snapshot={"company_key": lead.company_key, "score": lead.scoring.total_score,
                                "eligible_at_creation": lead.is_eligible},
                status=(SearchRunItemStatus.PENDING if lead.is_eligible else SearchRunItemStatus.EXCLUDED),
            ))
        session.commit()
        return run

    def request_stop(self, session: Session, run_id: int) -> SearchRun:
        run = self._get(session, run_id)
        if run.status in {SearchRunStatus.QUEUED, SearchRunStatus.RUNNING, SearchRunStatus.STOPPING}:
            run.stop_requested = True
            run.status = SearchRunStatus.STOPPING
            session.commit()
        return run

    def resume(self, session: Session, run_id: int) -> SearchRun:
        run = self._get(session, run_id)
        if run.status == SearchRunStatus.COMPLETED:
            return run
        if run.status not in {SearchRunStatus.QUEUED, SearchRunStatus.STOPPED, SearchRunStatus.FAILED, SearchRunStatus.STOPPING, SearchRunStatus.RUNNING}:
            raise ValueError("search run cannot be resumed")
        # Calling resume on an already stopped run is an explicit new user
        # intent.  A still-stopping worker, however, keeps its stop request.
        if run.status == SearchRunStatus.STOPPED:
            run.stop_requested = False
        if run.stop_requested:
            return self._stop(session, run)
        for item in self._items(session, run.id):
            if item.status == SearchRunItemStatus.PROCESSING:
                item.status = SearchRunItemStatus.PENDING
                item.started_at = None
        run.status, run.started_at, run.finished_at = SearchRunStatus.RUNNING, run.started_at or self._clock(), None
        run.completion_reason = None
        self._sync_counts(session, run)
        session.commit()

        try:
            # One composed/batched read covers all cached candidates.  We only
            # recompose after an enrichment has actually changed one target.
            leads_by_key = self._leads_by_key(session, run)
            enrichable_without_discovery = self._enrichable_without_discovery(
                session, tuple(leads_by_key.values()),
            ) if run.brave_hard_cap == 0 and isinstance(self._enricher, OfficialWebSearchEnricher) else None
            for item in self._items(session, run.id):
                session.refresh(run)
                if run.stop_requested:
                    return self._stop(session, run)
                if run.current_actionable_leads >= run.requested_actionable_leads:
                    return self._complete(session, run, SearchRunCompletionReason.TARGET_REACHED)
                if item.status in _TERMINAL or item.status == SearchRunItemStatus.ERROR:
                    continue
                self._process(
                    session, run, item, leads_by_key.get(item.company_key),
                    allow_enrichment=(enrichable_without_discovery is None or item.company_key in enrichable_without_discovery),
                )
            reason = (
                SearchRunCompletionReason.TARGET_REACHED
                if run.current_actionable_leads >= run.requested_actionable_leads
                else SearchRunCompletionReason.CANDIDATES_EXHAUSTED
            )
            return self._complete(session, run, reason)
        except Exception as exc:
            run.status, run.finished_at, run.error_summary = SearchRunStatus.FAILED, self._clock(), str(exc)[:1000]
            run.current_company_key, run.current_company_name, run.current_step = None, None, "failed"
            run.completion_reason = SearchRunCompletionReason.FAILED
            session.commit()
            return run

    def progress(self, session: Session, run_id: int) -> SearchRunProgress:
        run = self._get(session, run_id)
        current_actionable_leads: Optional[int] = None
        if run.status in {SearchRunStatus.COMPLETED, SearchRunStatus.STOPPED, SearchRunStatus.FAILED}:
            # The persisted count is an audit of the decision at run time.
            # Terminal UI reads instead use the same current policy as
            # /results, without changing that historical record.
            current_actionable_leads = len(self._current_results(session, run))
        else:
            self._sync_counts(session, run, commit=False)
        values = {name: getattr(run, name) for name in SearchRunProgress.__dataclass_fields__}
        if current_actionable_leads is not None:
            values["current_actionable_leads"] = current_actionable_leads
        for name in ("created_at", "started_at", "finished_at"):
            values[name] = _as_utc(values[name])
        if values["completion_reason"] is None:
            values["completion_reason"] = self._legacy_completion_reason(run)
        return SearchRunProgress(**values)

    def results(self, session: Session, run_id: int) -> tuple[CommercialLead, ...]:
        run = self._get(session, run_id)
        return self._current_results(session, run)

    def _current_results(self, session: Session, run: SearchRun) -> tuple[CommercialLead, ...]:
        actionable = {item.company_key for item in self._items(session, run.id) if item.actionable}
        if not actionable:
            return ()
        leads = list_commercial_leads(session, CommercialLeadQuery(
            department_code=run.department, include_excluded=True, limit=None,
        ), now=self._clock()).items
        # The item flag is the historical audit decision. Results are composed
        # from current facts and current actionable policy, so a newly detected
        # relevance mismatch is not kept as a usable lead.
        return tuple(
            lead for lead in leads
            if lead.company_key in actionable and is_actionable_lead(lead)
        )

    def _process(
        self, session: Session, run: SearchRun, item: SearchRunItem, lead: Optional[CommercialLead],
        *, allow_enrichment: bool = True,
    ) -> None:
        if lead is None or not lead.is_eligible or lead.active_offer_count < 1:
            self._finish_item(session, run, item, SearchRunItemStatus.EXCLUDED if lead and not lead.is_eligible else SearchRunItemStatus.SKIPPED)
            return
        item.status, item.started_at = SearchRunItemStatus.PROCESSING, self._clock()
        run.current_company_key, run.current_company_name = item.company_key, item.company_name_snapshot
        run.current_step = "reusing_cached_contactability"
        session.commit()
        if is_actionable_lead(lead):
            item.reused_cache = True
            self._finish_item(session, run, item, SearchRunItemStatus.ACTIONABLE, actionable=True)
            return
        if not allow_enrichment:
            run.current_step = "no_reusable_web_signal"
            self._finish_item(session, run, item, SearchRunItemStatus.SKIPPED)
            return
        run.current_step = "official_web_enrichment"
        before = self._brave_used(session, run.id)
        try:
            if self._enricher is not None:
                self._enricher.enrich(session, lead, run)
            # With no configured enrichment adapter, cached facts were still
            # evaluated and the item is accurately recorded as unresolved.
        except Exception as exc:
            item.last_error_type, item.last_error_message = type(exc).__name__, str(exc)[:500]
            self._finish_item(session, run, item, SearchRunItemStatus.ERROR)
            return
        after = self._brave_used(session, run.id)
        item.brave_requests_used += max(0, after - before)
        refreshed = self._current_lead(session, run, item.company_key)
        self._finish_item(session, run, item,
                          SearchRunItemStatus.ACTIONABLE if refreshed and is_actionable_lead(refreshed) else SearchRunItemStatus.UNRESOLVED,
                          actionable=bool(refreshed and is_actionable_lead(refreshed)), enriched=True)

    def _finish_item(self, session: Session, run: SearchRun, item: SearchRunItem, status: str, *, actionable: bool = False, enriched: bool = False) -> None:
        item.status, item.actionable, item.finished_at = status, actionable, self._clock()
        if enriched:
            run.candidates_enriched += 1
        run.candidates_considered += 1
        run.current_company_key, run.current_company_name, run.current_step = None, None, "candidate_finished"
        self._sync_counts(session, run, commit=False)
        session.commit()

    def _sync_counts(self, session: Session, run: SearchRun, *, commit: bool = False) -> None:
        items = self._items(session, run.id)
        run.current_actionable_leads = sum(1 for item in items if item.actionable)
        run.qualified_leads = run.current_actionable_leads
        run.companies_analyzed = sum(1 for item in items if item.status in _TERMINAL or item.status == SearchRunItemStatus.ERROR)
        run.brave_requests_used = self._brave_used(session, run.id)
        if commit:
            session.commit()

    def _current_lead(self, session: Session, run: SearchRun, key: str) -> Optional[CommercialLead]:
        return self._leads_by_key(session, run).get(key)

    def _leads_by_key(self, session: Session, run: SearchRun) -> dict[str, CommercialLead]:
        return {lead.company_key: lead for lead in list_commercial_leads(session, CommercialLeadQuery(
            department_code=run.department, include_excluded=True, limit=None,
        ), now=self._clock()).items}

    @staticmethod
    def _candidate_signal_keys(session: Session) -> set[str]:
        keys = set(session.scalars(select(WebsiteCandidateRecord.company_key).where(
            WebsiteCandidateRecord.is_active.is_(True),
        )))
        descriptions = session.execute(select(
            ObservedJobOffer.company_name, ObservedJobOffer.description,
        ).where(
            ObservedJobOffer.is_active.is_(True),
            ObservedJobOffer.description.is_not(None),
        ))
        keys.update(
            normalize_company_key(company_name)
            for company_name, description in descriptions
            if description and ("http://" in description.casefold() or "https://" in description.casefold())
        )
        return keys

    @classmethod
    def _enrichable_without_discovery(
        cls, session: Session, leads: tuple[CommercialLead, ...],
    ) -> set[str]:
        candidate_keys = cls._candidate_signal_keys(session)
        return {
            lead.company_key for lead in leads
            if cls._has_reusable_web_signal(lead, candidate_keys)
        }

    @staticmethod
    def _has_reusable_web_signal(lead: CommercialLead, candidate_keys: set[str]) -> bool:
        if lead.company_key in candidate_keys:
            return True
        if any(
            item.status == WebsiteVerificationStatus.HIGH_CONFIDENCE
            for item in lead.contactability.verified_websites
        ):
            return True
        for point in lead.contactability.contact_points:
            if not point.is_active or point.contact_type != ContactType.WEBSITE:
                continue
            evidence = lead.contactability.evidence_by_contact_point_id.get(point.id, ())
            if any(item.provider in {"offer_description", "societe_com"} for item in evidence):
                return True
        return False

    @classmethod
    def _priority(cls, lead: CommercialLead, candidate_keys: set[str]) -> int:
        if is_actionable_lead(lead):
            return 0
        return 1 if cls._has_reusable_web_signal(lead, candidate_keys) else 2

    @staticmethod
    def _legacy_completion_reason(run: SearchRun) -> Optional[str]:
        """Derive a read-only reason for runs created before the field existed."""
        if run.status == SearchRunStatus.COMPLETED:
            return (
                SearchRunCompletionReason.TARGET_REACHED
                if run.current_actionable_leads >= run.requested_actionable_leads
                else SearchRunCompletionReason.CANDIDATES_EXHAUSTED
            )
        if run.status == SearchRunStatus.STOPPED:
            return SearchRunCompletionReason.STOPPED
        if run.status == SearchRunStatus.FAILED:
            return SearchRunCompletionReason.FAILED
        return None

    @staticmethod
    def _items(session: Session, run_id: int) -> tuple[SearchRunItem, ...]:
        return tuple(session.scalars(select(SearchRunItem).where(SearchRunItem.run_id == run_id).order_by(SearchRunItem.selection_position)))

    @staticmethod
    def _brave_used(session: Session, run_id: int) -> int:
        return int(session.scalar(select(func.coalesce(func.sum(BraveUsageEvent.quantity), 0)).where(
            BraveUsageEvent.run_id == run_id, BraveUsageEvent.counted_for_budget.is_(True),
        )) or 0)

    @staticmethod
    def _get(session: Session, run_id: int) -> SearchRun:
        run = session.get(SearchRun, run_id)
        if run is None:
            raise ValueError("search run does not exist")
        return run

    def _stop(self, session: Session, run: SearchRun) -> SearchRun:
        run.status, run.finished_at = SearchRunStatus.STOPPED, self._clock()
        run.current_company_key, run.current_company_name, run.current_step = None, None, "stopped"
        run.completion_reason = SearchRunCompletionReason.STOPPED
        session.commit()
        return run

    def _complete(self, session: Session, run: SearchRun, reason: str) -> SearchRun:
        run.status, run.finished_at = SearchRunStatus.COMPLETED, self._clock()
        run.current_company_key, run.current_company_name, run.current_step = None, None, "completed"
        run.completion_reason = reason
        self._sync_counts(session, run, commit=False)
        session.commit()
        return run


def _fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
