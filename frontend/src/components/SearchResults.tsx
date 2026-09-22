import { useCallback, useEffect, useState } from 'react'
import { fetchSearchRunResults } from '../api'
import type { CommercialLeadPage, SearchRun } from '../types'
import { LeadCard } from './LeadCard'

type SearchResultsProps = { runId: number; run: SearchRun | null; onProgress: () => void }

export function SearchResults({ runId, run, onProgress }: SearchResultsProps) {
  const [page, setPage] = useState<CommercialLeadPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [notice, setNotice] = useState<string | null>(null)
  const load = useCallback(async () => {
    setIsLoading(true); setError(null)
    try { setPage(await fetchSearchRunResults(runId)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de charger les résultats.') }
    finally { setIsLoading(false) }
  }, [runId])
  useEffect(() => { void load() }, [load])

  return <section className="results-page" aria-labelledby="results-title">
    {notice && <p className="success-notice" role="status">{notice}</p>}
    <div className="section-heading"><div><p className="eyebrow">RECHERCHE Nº {runId}</p><h2 id="results-title">Résultats de la recherche</h2><p className="results-summary">{page ? `${page.total} lead${page.total > 1 ? 's' : ''} exploitable${page.total > 1 ? 's' : ''}` : 'Leads exploitables'}{run ? ` sur un objectif de ${run.requested_actionable_leads}` : ''}</p></div><button type="button" className="secondary-button" onClick={onProgress}>Voir la progression</button></div>
    {isLoading && <div className="state-card" role="status">Chargement des résultats…</div>}
    {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void load()}>Réessayer</button></div>}
    {page && !error && page.items.length === 0 && <div className="state-card"><h3>Aucun lead exploitable</h3><p>Cette recherche n’a pas encore produit de résultat actionnable.</p></div>}
    {page && !error && page.items.length > 0 && <div className="lead-list">{page.items.map((lead) => <LeadCard key={lead.company_key} lead={lead} onRelationshipSaved={(message) => { setNotice(message); void load() }} />)}</div>}
  </section>
}
