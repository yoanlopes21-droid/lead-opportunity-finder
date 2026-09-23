import { useCallback, useEffect, useState } from 'react'
import { dismissRecruitmentSignal, fetchRecruitmentSignals, promoteRecruitmentSignal } from '../api'
import type { RecruitmentSignalPage } from '../types'

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : 'Une erreur inattendue est survenue.'
}

export function RecruitmentSignals() {
  const [page, setPage] = useState<RecruitmentSignalPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)

  const load = useCallback(async () => {
    try { setPage(await fetchRecruitmentSignals()); setError(null) }
    catch (value) { setError(errorMessage(value)) }
  }, [])
  useEffect(() => { void load() }, [load])

  async function act(id: number, action: 'promote' | 'dismiss') {
    setBusyId(id); setNotice(null); setError(null)
    try {
      const result = action === 'promote' ? await promoteRecruitmentSignal(id) : await dismissRecruitmentSignal(id)
      setNotice(result.message); await load()
    } catch (value) { setError(errorMessage(value)); await load() }
    finally { setBusyId(null) }
  }

  return <section className="signals-page"><div className="section-heading"><div><p className="eyebrow">REVUE HUMAINE</p><h2>Signaux de recrutement</h2><p className="section-intro">Les résultats incomplets restent ici. Une promotion exige une entreprise, un poste, une preuve URL et une localisation explicite dans le 94.</p></div><button className="secondary-button" type="button" onClick={() => void load()}>Actualiser</button></div>
    {page && <div className="signal-summary"><span><strong>{page.new_count}</strong> nouveaux</span><span><strong>{page.review_needed_count}</strong> à vérifier</span><span><strong>{page.total}</strong> affichés</span></div>}
    {notice && <p className="success-notice" role="status">{notice}</p>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!page && !error && <div className="state-card">Chargement des signaux…</div>}
    {page?.items.length === 0 && <div className="state-card"><h3>Aucun signal à examiner</h3><p>Lancez un scan Open Web depuis Sources.</p></div>}
    <div className="signal-list">{page?.items.map((signal) => <article className="signal-card" key={signal.id}><header><div><div className="signal-badges"><span className={`status-badge ${signal.status === 'new' ? 'active' : 'expired'}`}>{signal.status}</span><span className={`page-type-badge ${signal.page_type}`}>{signal.page_type_label}</span></div><h3>{signal.job_title ?? signal.title ?? 'Intitulé non identifié'}</h3><p>{signal.company_name ?? 'Entreprise non identifiée'} · {signal.location_label ?? signal.commune ?? 'Localisation à vérifier'}</p></div><span className="confidence-badge">{signal.confidence == null ? 'confiance —' : `confiance ${Math.round(signal.confidence * 100)} %`}</span></header>{signal.snippet && <p className="signal-snippet">{signal.snippet}</p>}<dl className="signal-details"><div><dt>Source</dt><dd>{signal.source}</dd></div><div><dt>Découverte</dt><dd>{signal.discovery_provider}</dd></div><div><dt>Pourquoi</dt><dd>{signal.detection_reason ?? '—'}</dd></div><div><dt>Département</dt><dd>{signal.department_code ?? 'non confirmé'}</dd></div></dl>{!signal.is_promotable && <p className="promotion-blockers" role="note">Promotion indisponible : {signal.promotion_blockers.join(' · ')}</p>}<footer><a className="secondary-button" href={signal.source_url} target="_blank" rel="noreferrer">Voir la preuve</a><button className="primary-button" type="button" disabled={busyId === signal.id || !signal.is_promotable} title={signal.is_promotable ? undefined : signal.promotion_blockers.join(' · ')} onClick={() => void act(signal.id, 'promote')}>Promouvoir</button><button className="danger-button" type="button" disabled={busyId === signal.id} onClick={() => void act(signal.id, 'dismiss')}>Ignorer</button></footer></article>)}</div>
  </section>
}
