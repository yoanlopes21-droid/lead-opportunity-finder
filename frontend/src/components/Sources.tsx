import { FormEvent, useCallback, useEffect, useState } from 'react'
import {
  createJobSourceBoard, createOpenWebRun, deleteJobSourceBoard, fetchJobSourceBoards,
  fetchLatestOpenWebRun, refreshJobSourceBoard, stopOpenWebRun, updateJobSourceBoard,
} from '../api'
import type { JobSourceBoard, JobSourceBoardCreate, SourceRefreshRun } from '../types'
import { BraveUsageWidget } from './BraveUsageWidget'

const INITIAL: JobSourceBoardCreate = {
  provider_id: 'greenhouse', display_name: '', board_identifier: '', company_name_hint: '', enabled: true,
}

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : 'Une erreur inattendue est survenue.'
}

function runDuration(run: SourceRefreshRun | null) {
  if (!run) return '—'
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now()
  const seconds = Math.max(0, Math.floor((end - new Date(run.started_at).getTime()) / 1000))
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, '0')} s`
}

export function Sources({ onSignals }: { onSignals: () => void }) {
  const [boards, setBoards] = useState<JobSourceBoard[]>([])
  const [openRun, setOpenRun] = useState<SourceRefreshRun | null>(null)
  const [form, setForm] = useState<JobSourceBoardCreate>(INITIAL)
  const [targetSignals, setTargetSignals] = useState(20)
  const [requestCap, setRequestCap] = useState(8)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const [nextBoards, nextRun] = await Promise.all([fetchJobSourceBoards(), fetchLatestOpenWebRun()])
      setBoards(nextBoards); setOpenRun(nextRun); setError(null)
    } catch (value) { setError(errorMessage(value)) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const active = openRun?.status === 'queued' || openRun?.status === 'running' || boards.some((board) => board.last_refresh_status === 'queued' || board.last_refresh_status === 'running')
    if (!active) return
    const timer = window.setInterval(() => void load(), 1200)
    return () => window.clearInterval(timer)
  }, [boards, openRun, load])

  async function addBoard(event: FormEvent) {
    event.preventDefault(); setError(null); setNotice(null)
    try {
      await createJobSourceBoard(form); setForm(INITIAL); setNotice('Board ATS ajouté. Il reste inactif côté réseau jusqu’à un rafraîchissement manuel.'); await load()
    } catch (value) { setError(errorMessage(value)) }
  }

  async function toggleBoard(board: JobSourceBoard) {
    try { await updateJobSourceBoard(board.id, { enabled: !board.enabled }); await load() }
    catch (value) { setError(errorMessage(value)) }
  }

  async function removeBoard(board: JobSourceBoard) {
    if (!window.confirm(`Supprimer la configuration « ${board.display_name} » ? Les offres observées sont conservées.`)) return
    try { await deleteJobSourceBoard(board.id); setNotice('Configuration supprimée ; les observations historiques sont conservées.'); await load() }
    catch (value) { setError(errorMessage(value)) }
  }

  async function refreshBoard(board: JobSourceBoard) {
    try { await refreshJobSourceBoard(board.id); setNotice(`Rafraîchissement de ${board.display_name} lancé.`); await load() }
    catch (value) { setError(errorMessage(value)) }
  }

  async function startOpenWeb() {
    try { setOpenRun(await createOpenWebRun(targetSignals, requestCap)); setNotice('Scan Open Web lancé avec un budget borné.'); setError(null) }
    catch (value) { setError(errorMessage(value)) }
  }

  async function stopOpenWeb() {
    if (!openRun) return
    try { setOpenRun(await stopOpenWebRun(openRun.id)); setNotice('Arrêt demandé ; le scan s’arrêtera entre deux requêtes.') }
    catch (value) { setError(errorMessage(value)) }
  }

  const openActive = openRun?.status === 'queued' || openRun?.status === 'running'

  return <section className="sources-page">
    <div className="section-heading"><div><p className="eyebrow">SOURCES D’OFFRES</p><h2>Sites carrière et Open Web</h2><p className="section-intro">Configurez quelques boards employeurs publics, puis lancez uniquement les collectes utiles.</p></div></div>
    {notice && <p className="success-notice" role="status">{notice}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <BraveUsageWidget />

    <div className="source-layout">
      <form className="source-card board-form" onSubmit={(event) => void addBoard(event)}>
        <p className="eyebrow">CONFIGURATION ATS</p><h3>Ajouter un site carrière</h3>
        <label>Provider<select value={form.provider_id} onChange={(event) => setForm({ ...form, provider_id: event.target.value as JobSourceBoardCreate['provider_id'] })}><option value="greenhouse">Greenhouse</option><option value="lever">Lever</option></select></label>
        <label>Nom affiché<input required value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} placeholder="Ex. ACME Carrières" /></label>
        <label>Entreprise<input required value={form.company_name_hint} onChange={(event) => setForm({ ...form, company_name_hint: event.target.value })} placeholder="Nom explicite de l’employeur" /></label>
        <label>Identifiant ou URL publique<input required value={form.board_identifier} onChange={(event) => setForm({ ...form, board_identifier: event.target.value })} placeholder={form.provider_id === 'greenhouse' ? 'boards.greenhouse.io/entreprise' : 'jobs.lever.co/entreprise'} /><small>Seuls les domaines publics reconnus du provider sont acceptés.</small></label>
        <button className="primary-button" type="submit">Ajouter le board</button>
      </form>

      <article className="source-card open-web-card">
        <p className="eyebrow">DÉCOUVERTE BORNÉE</p><h3>Open Web · Val-de-Marne</h3>
        <p>Le système génère un petit plan de requêtes : sites employeurs, jobboards indexés et ATS publics. Aucun résultat faible n’est automatiquement transformé en offre.</p>
        <div className="source-fields"><label>Signaux ciblés<input type="number" min="1" max="50" value={targetSignals} onChange={(event) => setTargetSignals(Number(event.target.value))} /></label><label>Requêtes Brave max<input type="number" min="1" max="10" value={requestCap} onChange={(event) => setRequestCap(Number(event.target.value))} /></label></div>
        <div className="source-actions"><button className="primary-button" type="button" disabled={openActive} onClick={() => void startOpenWeb()}>{openActive ? 'Scan en cours…' : 'Démarrer le scan'}</button>{openActive && <button className="secondary-button" type="button" onClick={() => void stopOpenWeb()}>Arrêter</button>}<button className="secondary-button" type="button" onClick={onSignals}>Examiner les signaux</button></div>
        {openRun && <dl className="source-metrics"><div><dt>État</dt><dd>{openRun.status}</dd></div><div><dt>Durée</dt><dd>{runDuration(openRun)}</dd></div><div><dt>Requêtes consommées</dt><dd>{openRun.brave_requests_used}</dd></div><div><dt>Signaux trouvés</dt><dd>{openRun.signals_found}</dd></div><div><dt>Offres promues</dt><dd>{openRun.signals_promoted}</dd></div><div><dt>Raison de fin</dt><dd>{openRun.completion_reason ?? '—'}</dd></div></dl>}
        {openRun?.error_summary && <p className="inline-error">{openRun.error_summary}</p>}
      </article>
    </div>

    <section className="board-list-section"><div className="section-heading"><div><p className="eyebrow">BOARDS EMPLOYEURS</p><h2>Sites carrière configurés</h2></div><button className="secondary-button" type="button" onClick={() => void load()}>Actualiser</button></div>
      {loading && <div className="state-card">Chargement des sources…</div>}
      {!loading && boards.length === 0 && <div className="state-card"><h3>Aucun board configuré</h3><p>Ajoutez un board Greenhouse ou Lever public pour commencer.</p></div>}
      <div className="board-list">{boards.map((board) => {
        const busy = board.last_refresh_status === 'queued' || board.last_refresh_status === 'running'
        return <article className="board-card" key={board.id}><div><span className={`status-badge ${board.enabled ? 'active' : 'expired'}`}>{board.enabled ? 'actif' : 'désactivé'}</span><h3>{board.display_name}</h3><p>{board.provider_id} · {board.board_identifier} · {board.company_name_hint}</p></div><div className="board-actions"><button className="secondary-button" type="button" disabled={!board.enabled || busy} onClick={() => void refreshBoard(board)}>{busy ? 'En cours…' : 'Rafraîchir'}</button><button className="secondary-button" type="button" onClick={() => void toggleBoard(board)}>{board.enabled ? 'Désactiver' : 'Activer'}</button><button className="danger-button" type="button" disabled={busy} onClick={() => void removeBoard(board)}>Supprimer</button></div><dl className="source-metrics"><div><dt>Offres actives</dt><dd>{board.active_offer_count}</dd></div><div><dt>Reçues</dt><dd>{board.last_offers_received}</dd></div><div><dt>Nouvelles</dt><dd>{board.last_offers_new}</dd></div><div><dt>Mises à jour</dt><dd>{board.last_offers_updated}</dd></div><div><dt>Désactivées</dt><dd>{board.last_offers_deactivated}</dd></div><div><dt>Durée</dt><dd>{board.last_duration_seconds == null ? '—' : `${board.last_duration_seconds.toFixed(1)} s`}</dd></div><div><dt>Dernier état</dt><dd>{board.last_refresh_status ?? 'jamais lancé'}</dd></div><div><dt>Dernier refresh</dt><dd>{board.last_refresh_at ? new Date(board.last_refresh_at).toLocaleString('fr-FR') : '—'}</dd></div></dl>{board.last_error && <p className="inline-error">{board.last_error}</p>}</article>
      })}</div>
    </section>
  </section>
}
