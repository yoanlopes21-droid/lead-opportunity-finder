import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE_URL, fetchSearchRun, resumeSearchRun, stopSearchRun } from '../api'
import type { SearchRun } from '../types'

type SearchProgressProps = { runId: number; initialRun: SearchRun | null; onRunChange: (run: SearchRun) => void; onResults: () => void }

const statusLabels: Record<string, string> = { queued: 'En attente', running: 'En cours', stopping: 'Arrêt en cours', stopped: 'Arrêtée', completed: 'Terminée', failed: 'Interrompue par une erreur' }
const stepLabels: Record<string, string> = { reusing_cached_contactability: 'Réutilisation des données existantes…', official_web_enrichment: 'Recherche du site officiel et vérification des coordonnées…', no_reusable_web_signal: 'Candidat sans donnée web réutilisable…', candidate_finished: 'Analyse des opportunités…', completed: 'Recherche terminée', stopped: 'Recherche arrêtée', failed: 'La recherche a rencontré une erreur' }
const terminalStatuses = new Set(['stopped', 'completed', 'failed'])

function timestamp(value: string) {
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)
  return new Date(hasTimezone ? value : `${value}Z`).getTime()
}

function elapsed(run: SearchRun, now: number) {
  const start = timestamp(run.started_at ?? run.created_at)
  const end = run.finished_at ? timestamp(run.finished_at) : now
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000))
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return minutes ? `${minutes} min ${seconds.toString().padStart(2, '0')} s` : `${seconds} s`
}

export function SearchProgress({ runId, initialRun, onRunChange, onResults }: SearchProgressProps) {
  const [run, setRun] = useState<SearchRun | null>(initialRun?.id === runId ? initialRun : null)
  const [error, setError] = useState<string | null>(null)
  const [isStopping, setIsStopping] = useState(false)
  const [isResuming, setIsResuming] = useState(false)
  const isResumingRef = useRef(false)
  const [sseFallback, setSseFallback] = useState(false)
  const [now, setNow] = useState(Date.now())

  const updateRun = useCallback((next: SearchRun) => { setRun(next); onRunChange(next) }, [onRunChange])
  const refresh = useCallback(async () => {
    try {
      const next = await fetchSearchRun(runId)
      updateRun(next); setError(null)
      if (isResumingRef.current) { isResumingRef.current = false; setIsResuming(false) }
      return next
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Impossible de charger la progression.')
      return null
    }
  }, [runId, updateRun])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!run || (!['queued', 'running', 'stopping'].includes(run.status) && !isResuming)) return
    setSseFallback(false)
    const source = new EventSource(`${API_BASE_URL}/api/v1/search-runs/${runId}/events`)
    source.addEventListener('progress', (event) => {
      const next = JSON.parse((event as MessageEvent<string>).data) as SearchRun
      updateRun(next); setError(null)
      if (terminalStatuses.has(next.status)) source.close()
    })
    source.onerror = () => { source.close(); setSseFallback(true) }
    return () => source.close()
  }, [run?.status, runId, isResuming, updateRun])

  useEffect(() => {
    if (!sseFallback && !isResuming) return
    const timer = window.setInterval(() => void refresh(), 1500)
    return () => window.clearInterval(timer)
  }, [sseFallback, isResuming, refresh])

  const percent = useMemo(() => run ? Math.min(100, Math.round((run.current_actionable_leads / run.requested_actionable_leads) * 100)) : 0, [run])

  async function requestStop() {
    if (!run || isStopping) return
    setIsStopping(true); setError(null)
    try { updateRun(await stopSearchRun(run.id)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de demander l’arrêt.'); setIsStopping(false) }
  }
  async function resume() {
    if (!run || isResuming) return
    isResumingRef.current = true; setIsResuming(true); setError(null); setSseFallback(true)
    try { updateRun(await resumeSearchRun(run.id)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de reprendre la recherche.'); isResumingRef.current = false; setIsResuming(false) }
  }

  if (!run) return <div className="state-card" role="status">Chargement de la recherche…</div>

  const isActive = ['queued', 'running', 'stopping'].includes(run.status) || isResuming
  const isPartial = run.status === 'completed' && run.current_actionable_leads < run.requested_actionable_leads
  const completionMessages: Record<string, string> = {
    target_reached: 'L’objectif de leads exploitables a été atteint.',
    candidates_exhausted: 'Tous les candidats disponibles ont été examinés avec les ressources configurées.',
    brave_run_cap_reached: 'Le budget Brave maximum du run a été atteint.',
    monthly_budget_exhausted: 'Le budget Brave mensuel disponible a été atteint.',
  }
  const stage = isResuming ? 'Reprise de la recherche…' : (stepLabels[run.current_step ?? ''] ?? (isActive ? 'Analyse des opportunités…' : statusLabels[run.status]))

  return <section className="progress-page" aria-labelledby="progress-title">
    <header className="progress-header"><div><p className="eyebrow">RECHERCHE Nº {run.id}</p><h2 id="progress-title">{run.status === 'completed' ? 'Recherche terminée' : run.status === 'stopped' ? 'Recherche arrêtée' : 'Progression de la recherche'}</h2><p className="stage-label" aria-live="polite">{stage}</p></div><span className={`status-pill ${run.status}`}>{statusLabels[isResuming ? 'queued' : run.status]}</span></header>
    <div className="goal-progress"><div><strong>{run.current_actionable_leads} / {run.requested_actionable_leads}</strong><span>leads exploitables</span></div><span>{percent} % de l’objectif</span></div>
    <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={run.requested_actionable_leads} aria-valuenow={run.current_actionable_leads} aria-label="Objectif de leads exploitables"><span style={{ width: `${percent}%` }} /></div>
    {isPartial && <div className="partial-notice"><strong>{run.current_actionable_leads} leads exploitables trouvés sur {run.requested_actionable_leads} demandés.</strong><p>{completionMessages[run.completion_reason ?? ''] ?? 'La recherche s’est terminée normalement avant l’objectif.'}</p></div>}
    <dl className="progress-metrics"><div><dt>Candidats examinés</dt><dd>{run.candidates_considered}</dd></div><div><dt>Candidats enrichis</dt><dd>{run.candidates_enriched}</dd></div><div><dt>Brave</dt><dd>{run.brave_requests_used} / {run.brave_hard_cap}<small>requêtes max</small></dd></div><div><dt>Temps écoulé</dt><dd>{elapsed(run, now)}</dd></div></dl>
    {isActive && (run.current_company_name || run.current_company_key) && <p className="current-candidate"><span>Entreprise en cours</span>{run.current_company_name ?? run.current_company_key}</p>}
    {sseFallback && isActive && <p className="connection-note">Connexion en direct interrompue : la progression est actualisée automatiquement.</p>}
    {run.error_summary && <div className="run-warning" role="alert"><strong>Détail de l’interruption</strong><p>{run.error_summary}</p></div>}
    {error && <div className="inline-error" role="alert">{error} <button type="button" onClick={() => void refresh()}>Réessayer</button></div>}
    <div className="progress-actions">
      {['queued', 'running', 'stopping'].includes(run.status) && <button type="button" className="danger-button" onClick={() => void requestStop()} disabled={isStopping || run.status === 'stopping'}>{isStopping || run.status === 'stopping' ? 'Arrêt en cours…' : 'Arrêter la recherche'}</button>}
      {(run.status === 'stopped' || run.status === 'failed') && <button type="button" className="primary-button" onClick={() => void resume()} disabled={isResuming}>{isResuming ? 'Reprise…' : 'Reprendre la recherche'}</button>}
      {terminalStatuses.has(run.status) && <button type="button" className="secondary-button" onClick={onResults}>Voir les résultats</button>}
    </div>
  </section>
}
