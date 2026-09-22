import { useCallback, useEffect, useRef, useState } from 'react'
import { createJobOfferRefreshRun, fetchActiveJobOfferRefreshRun, fetchCommercialLeads, fetchJobOfferRefreshRun } from '../api'
import type { CommercialLeadPage, JobOfferRefreshRun } from '../types'
import { BraveUsageWidget } from './BraveUsageWidget'
import { LeadCard } from './LeadCard'

export function Dashboard() {
  const [page, setPage] = useState<CommercialLeadPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [refreshRun, setRefreshRun] = useState<JobOfferRefreshRun | null>(null)
  const [refreshError, setRefreshError] = useState<string | null>(null)
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

  useEffect(() => {
    if (!refreshRun || !refreshing) return
    const timer = window.setInterval(() => {
      fetchJobOfferRefreshRun(refreshRun.id).then((next) => {
        setRefreshRun(next)
        if (next.status === 'completed') void loadPage(0)
      }).catch((requestError) => setRefreshError(requestError instanceof Error ? requestError.message : 'Impossible de suivre la mise à jour.'))
    }, 700)
    return () => window.clearInterval(timer)
  }, [refreshRun, refreshing, loadPage])

  async function startRefresh() {
    if (refreshStartInFlight.current || refreshing) return
    refreshStartInFlight.current = true
    setRefreshError(null)
    try {
      const run = await createJobOfferRefreshRun()
      setRefreshRun(run)
    } catch (requestError) { setRefreshError(requestError instanceof Error ? requestError.message : 'Impossible de démarrer la mise à jour.') }
    finally { refreshStartInFlight.current = false }
  }

  const elapsed = refreshRun ? Math.max(0, Math.floor((Date.now() - new Date(refreshRun.started_at).getTime()) / 1000)) : 0
  const elapsedLabel = `${Math.floor(elapsed / 60)} min ${String(elapsed % 60).padStart(2, '0')} s`

  const rangeStart = page && page.total > 0 ? page.offset + 1 : 0
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0
  const canGoPrevious = Boolean(page && page.offset > 0)
  const canGoNext = Boolean(page && page.offset + page.items.length < page.total)

  return <>
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
      {refreshRun?.status === 'completed' && <p className="refresh-result">{refreshRun.active_offer_count ?? 0} offres actives · {refreshRun.active_opportunity_count ?? 0} opportunités actives après recomposition.</p>}
      {refreshRun?.status === 'failed' && refreshRun.error_summary && <p className="inline-error" role="alert">{refreshRun.error_summary}</p>}
    </section>
    <section className="lead-section" aria-labelledby="lead-list-title"><div className="section-heading"><div><p className="eyebrow">LISTE PRIORISÉE</p><h2 id="lead-list-title">Opportunités à contacter</h2></div>
      <button type="button" className="refresh-button" onClick={() => void loadPage(page?.offset ?? 0)} disabled={isLoading}>{isLoading && page ? 'Actualisation…' : 'Recharger la liste'}</button>
    </div>
      {isLoading && !page && <div className="state-card" role="status">Chargement des opportunités locales…</div>}
      {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void loadPage(page?.offset ?? 0)}>Réessayer</button></div>}
      {page && !error && page.items.length === 0 && <div className="state-card"><h3>Aucun lead à afficher</h3><p>Aucune opportunité ne correspond à cette recherche pour le moment.</p></div>}
      {page && !error && page.items.length > 0 && <div className="lead-list">{page.items.map((lead) => <LeadCard key={lead.company_key} lead={lead} />)}</div>}
    </section>
    {page && !error && <nav className="pagination" aria-label="Pagination des leads"><button type="button" onClick={() => void loadPage(Math.max(0, page.offset - page.limit))} disabled={!canGoPrevious || isLoading}>Page précédente</button><p>{rangeStart}–{rangeEnd} sur {page.total}</p><button type="button" onClick={() => void loadPage(page.offset + page.limit)} disabled={!canGoNext || isLoading}>Page suivante</button></nav>}
  </>
}
