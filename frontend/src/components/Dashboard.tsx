import { useCallback, useEffect, useRef, useState } from 'react'
import { createJobOfferRefreshRun, fetchActiveJobOfferRefreshRun, fetchCommercialLeads, fetchJobOfferRefreshRun, fetchJobSourceBoards, fetchLatestOpenWebRun, fetchRecruitmentSignals } from '../api'
import type { CommercialLeadPage, JobOfferRefreshRun, JobSourceBoard, RecruitmentSignalPage, SourceRefreshRun } from '../types'
import { BraveUsageWidget } from './BraveUsageWidget'
import { LeadCard } from './LeadCard'

function timestamp(value: string) {
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return new Date(hasTimezone ? value : `${value}Z`).getTime()
}

export function Dashboard({ onSources, onSignals }: { onSources: () => void; onSignals: () => void }) {
  const [page, setPage] = useState<CommercialLeadPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [notice, setNotice] = useState<string | null>(null)
  const [refreshRun, setRefreshRun] = useState<JobOfferRefreshRun | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [boards, setBoards] = useState<JobSourceBoard[]>([])
  const [openWebRun, setOpenWebRun] = useState<SourceRefreshRun | null>(null)
  const [signals, setSignals] = useState<RecruitmentSignalPage | null>(null)
  const refreshStartInFlight = useRef(false)
  const refreshing = refreshRun?.status === 'queued' || refreshRun?.status === 'running'

  const loadPage = useCallback(async (offset: number) => {
    setIsLoading(true); setError(null)
    try { setPage(await fetchCommercialLeads(offset)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de joindre l’API locale.') }
    finally { setIsLoading(false) }
  }, [])

  useEffect(() => { void loadPage(0) }, [loadPage])
  useEffect(() => { void fetchActiveJobOfferRefreshRun().then(setRefreshRun).catch(() => {}) }, [])
  useEffect(() => { void Promise.all([fetchJobSourceBoards(), fetchLatestOpenWebRun(), fetchRecruitmentSignals()]).then(([nextBoards, nextRun, nextSignals]) => { setBoards(nextBoards); setOpenWebRun(nextRun); setSignals(nextSignals) }).catch(() => {}) }, [])

  useEffect(() => {
    if (!refreshRun || !refreshing) return
    let cancelled = false
    let timer: number
    const poll = async () => {
      try {
        const next = await fetchJobOfferRefreshRun(refreshRun.id)
        if (cancelled) return
        setRefreshRun(next)
        setRefreshError(null)
        if (next.status === 'completed') void loadPage(0)
      } catch {
        if (!cancelled) setRefreshError('La progression est temporairement indisponible. Nouvelle tentative automatique…')
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void poll(), 1200)
      }
    }
    timer = window.setTimeout(() => void poll(), 700)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [refreshRun, refreshing, loadPage])

  async function startRefresh() {
    if (refreshStartInFlight.current || refreshing) return
    refreshStartInFlight.current = true
    setRefreshError(null)
    try {
      const run = await createJobOfferRefreshRun()
      setRefreshRun(run)
    } catch { setRefreshError('Impossible de démarrer la mise à jour. Vérifiez que l’API locale est disponible.') }
    finally { refreshStartInFlight.current = false }
  }

  const elapsedEnd = refreshRun?.finished_at ? timestamp(refreshRun.finished_at) : Date.now()
  const elapsed = refreshRun ? Math.max(0, Math.floor((elapsedEnd - timestamp(refreshRun.started_at)) / 1000)) : 0
  const elapsedLabel = `${Math.floor(elapsed / 60)} min ${String(elapsed % 60).padStart(2, '0')} s`

  const rangeStart = page && page.total > 0 ? page.offset + 1 : 0
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0
  const canGoPrevious = Boolean(page && page.offset > 0)
  const canGoNext = Boolean(page && page.offset + page.items.length < page.total)

  return <>
    {notice && <p className="success-notice" role="status">{notice}</p>}
    <section className="summary-grid" aria-label="Résumé des leads">
      <article><span>Leads prioritaires</span><strong>{page?.total ?? '—'}</strong><small>résultat de la recherche actuelle</small></article>
      <article><span>Département</span><strong>94</strong><small>Val-de-Marne</small></article>
      <article><span>Affichés</span><strong>{page?.items.length ?? '—'}</strong><small>sur cette page</small></article>
    </section>
    <BraveUsageWidget />
    <section className="offer-refresh-panel" aria-labelledby="offer-refresh-title">
      <div><p className="eyebrow">SOURCE OFFICIELLE</p><h2 id="offer-refresh-title">Mettre à jour les offres</h2>
        {!refreshRun && <p>Interroge France Travail pour actualiser les offres actives. Aucun crédit Brave n’est utilisé.</p>}
        {refreshRun && <p className="refresh-status">{refreshRun.status === 'completed' ? 'Mise à jour terminée.' : refreshRun.status === 'failed' ? 'La mise à jour a échoué ; les offres actives existantes sont conservées.' : 'Collecte France Travail en cours. Aucun enrichissement de contacts n’est lancé.'}</p>}
      </div>
      <button type="button" className="primary-button" onClick={() => void startRefresh()} disabled={refreshing}>{refreshing ? 'Mise à jour en cours…' : 'Mettre à jour les offres'}</button>
      {refreshError && <p className="inline-error" role="alert">{refreshError}</p>}
      {refreshRun && <dl className="refresh-metrics">
        <div><dt>État</dt><dd>{refreshRun.status}</dd></div><div><dt>Temps écoulé</dt><dd>{elapsedLabel}</dd></div>
        <div><dt>Offres reçues</dt><dd>{refreshRun.offers_received}</dd></div><div><dt>Pages traitées</dt><dd>{refreshRun.pages_processed}</dd></div>
        <div><dt>Nouvelles offres</dt><dd>{refreshRun.offers_new}</dd></div><div><dt>Offres mises à jour</dt><dd>{refreshRun.offers_updated}</dd></div>
        <div><dt>Offres désactivées</dt><dd>{refreshRun.offers_deactivated}</dd></div><div><dt>Fenêtres traitées</dt><dd>{refreshRun.temporal_windows}</dd></div>
      </dl>}
      {refreshRun?.status === 'completed' && <p className="refresh-result">{refreshRun.active_offer_count ?? '—'} offres actives · {refreshRun.active_opportunity_count ?? '—'} opportunités actives après recomposition.</p>}
      {refreshRun?.status === 'failed' && refreshRun.error_summary && <p className="inline-error" role="alert">{refreshRun.error_summary}</p>}
    </section>
    <section className="multi-source-overview" aria-labelledby="source-overview-title"><div className="section-heading"><div><p className="eyebrow">COUVERTURE MULTI-SOURCE</p><h2 id="source-overview-title">Sources supplémentaires</h2></div><div className="source-actions"><button className="secondary-button" type="button" onClick={onSources}>Gérer les sources</button><button className="secondary-button" type="button" onClick={onSignals}>Examiner les signaux</button></div></div><div className="source-overview-grid"><article><span>Sites carrière / ATS</span><strong>{boards.filter((board) => board.enabled).length}</strong><small>boards actifs · {boards.reduce((total, board) => total + board.active_offer_count, 0)} offres actives</small></article><article><span>Dernier Open Web</span><strong>{openWebRun?.signals_found ?? '—'}</strong><small>signaux · {openWebRun?.brave_requests_used ?? 0} requêtes Brave consommées</small></article><article><span>À examiner</span><strong>{signals ? signals.new_count + signals.review_needed_count : '—'}</strong><small>signaux nouveaux ou incomplets</small></article></div></section>
    <section className="lead-section" aria-labelledby="lead-list-title"><div className="section-heading"><div><p className="eyebrow">LISTE PRIORISÉE</p><h2 id="lead-list-title">Opportunités à contacter</h2></div>
      <button type="button" className="refresh-button" onClick={() => void loadPage(page?.offset ?? 0)} disabled={isLoading}>{isLoading && page ? 'Actualisation…' : 'Recharger la liste'}</button>
    </div>
      {isLoading && !page && <div className="state-card" role="status">Chargement des opportunités locales…</div>}
      {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void loadPage(page?.offset ?? 0)}>Réessayer</button></div>}
      {page && !error && page.items.length === 0 && <div className="state-card"><h3>Aucun lead à afficher</h3><p>Aucune opportunité ne correspond à cette recherche pour le moment.</p></div>}
      {page && !error && page.items.length > 0 && <div className="lead-list">{page.items.map((lead) => <LeadCard key={lead.company_key} lead={lead} onRelationshipSaved={(message) => { setNotice(message); void loadPage(page.offset) }} />)}</div>}
    </section>
    {page && !error && <nav className="pagination" aria-label="Pagination des leads"><button type="button" onClick={() => void loadPage(Math.max(0, page.offset - page.limit))} disabled={!canGoPrevious || isLoading}>Page précédente</button><p>{rangeStart}–{rangeEnd} sur {page.total}</p><button type="button" onClick={() => void loadPage(page.offset + page.limit)} disabled={!canGoNext || isLoading}>Page suivante</button></nav>}
  </>
}
