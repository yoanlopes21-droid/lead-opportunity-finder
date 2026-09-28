import { useCallback, useEffect, useState } from 'react'
import { fetchCommercialDossier, fetchCommercialRelationships, fetchInteractionHistory, reopenCommercialOpportunity } from '../api'
import type { CommercialDossier, CommercialInteraction, CommercialRelationship } from '../types'
import { Exclusions } from './Exclusions'
import { RelationshipModal, relationshipLabels } from './RelationshipModal'
import { channelLabels, outcomeLabels } from '../interactionRules'
import type { InteractionChannel, InteractionOutcome } from '../types'
import { ApproachWorkspace } from './ApproachWorkspace'
import { formatParisDateTime } from '../parisTime'


type Tab = 'ongoing' | 'follow_up' | 'planning' | 'clients' | 'closed' | 'excluded'
const tabs: Array<{ value: Tab; label: string }> = [
  { value: 'ongoing', label: 'En cours' }, { value: 'follow_up', label: 'À relancer' },
  { value: 'planning', label: 'À planifier' },
  { value: 'clients', label: 'Clients' }, { value: 'closed', label: 'Fermés' }, { value: 'excluded', label: 'Exclus' },
]
const timingLabels = { overdue: 'Relance en retard', today: 'À relancer aujourd’hui', upcoming: 'Prochaine relance' }

export function Prospection() {
  const [tab, setTab] = useState<Tab>('ongoing')
  const [items, setItems] = useState<CommercialRelationship[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editing, setEditing] = useState<CommercialRelationship | null>(null)
  const [historyKey, setHistoryKey] = useState<string | null>(null)
  const [history, setHistory] = useState<CommercialInteraction[]>([])
  const [dossier, setDossier] = useState<CommercialDossier | null>(null)
  const [openingDossier, setOpeningDossier] = useState<number | null>(null)

  const load = useCallback(async () => {
    if (tab === 'excluded') return
    setLoading(true); setError(null)
    try { setItems((await fetchCommercialRelationships(tab, search)).items) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de charger le suivi commercial.') }
    finally { setLoading(false) }
  }, [tab, search])
  useEffect(() => { void load() }, [load])

  async function reopen(item: CommercialRelationship) {
    if (!window.confirm(`Remettre ${item.company_name_snapshot} dans les nouvelles opportunités ?`)) return
    try {
      await reopenCommercialOpportunity(item.id)
      setNotice(`${item.company_name_snapshot} peut de nouveau apparaître dans les opportunités.`)
      await load()
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de remettre cette entreprise dans les opportunités.') }
  }

  async function showHistory(key: string) {
    if (historyKey === key) { setHistoryKey(null); return }
    setHistoryKey(key); setHistory([])
    try { setHistory((await fetchInteractionHistory(key)).items) }
    catch { setError('Impossible de charger l’historique commercial.') }
  }

  async function openDossier(id: number) {
    setOpeningDossier(id); setError(null)
    try { setDossier(await fetchCommercialDossier(id)) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible d’ouvrir le dossier commercial.') }
    finally { setOpeningDossier(null) }
  }

  return <section className="prospection-page" aria-labelledby="prospection-title">
    <div className="section-heading"><div><p className="eyebrow">SUIVI COMMERCIAL</p><h2 id="prospection-title">Prospection</h2><p className="section-intro">Retrouvez les entreprises déjà travaillées, les prochaines relances et les exclusions fortes.</p></div></div>
    <nav className="prospection-tabs" aria-label="Vues de prospection">{tabs.map((item) => <button type="button" key={item.value} className={tab === item.value ? 'active' : ''} onClick={() => { setTab(item.value); setNotice(null) }}>{item.label}</button>)}</nav>
    {notice && <p className="success-notice" role="status">{notice}</p>}
    {tab === 'excluded' ? <Exclusions embedded onOpenDossier={(id) => void openDossier(id)} /> : <>
      <div className="prospection-toolbar"><label>Rechercher une entreprise<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Nom de l’entreprise" /></label><button type="button" className="secondary-button" onClick={() => void load()} disabled={loading}>Actualiser</button></div>
      {loading && items.length === 0 && <div className="state-card" role="status">Chargement du suivi commercial…</div>}
      {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void load()}>Réessayer</button></div>}
      {!loading && !error && items.length === 0 && <div className="state-card"><h3>Aucune entreprise dans cette vue</h3><p>Une entreprise apparaît ici dès qu’une action commerciale est enregistrée depuis sa carte.</p></div>}
      {!error && items.length > 0 && <div className="relationship-list">{items.map((item) => <article className={`relationship-card status-${item.status} ${item.follow_up_timing ?? ''}`} key={item.id}>
        <div className="relationship-card-heading"><div>{item.follow_up_timing && <span className={`follow-up-badge ${item.follow_up_timing}`}>{timingLabels[item.follow_up_timing]}</span>}{tab === 'planning' && <span className="follow-up-badge planning">Suite à planifier</span>}<h3>{item.company_name_snapshot}</h3><p className="relationship-status">{relationshipLabels[item.status]}</p></div><div className="relationship-actions"><button type="button" className="primary-button" disabled={openingDossier === item.id} onClick={() => void openDossier(item.id)}>{openingDossier === item.id ? 'Ouverture…' : 'Ouvrir le dossier'}</button><button type="button" className="secondary-button" onClick={() => setEditing(item)}>Modifier le suivi</button></div></div>
        <dl><div><dt>Dernier contact</dt><dd>{formatParisDateTime(item.last_contact_at)}</dd></div><div><dt>Prochaine action</dt><dd>{item.next_action ?? '—'} · {item.next_action_at ? formatParisDateTime(item.next_action_at) : item.status === 'follow_up' ? 'Date à définir' : '—'}</dd></div><div><dt>Contact / canal</dt><dd>{item.used_channel ? channelLabels[item.used_channel as InteractionChannel] ?? item.used_channel : item.contact_point_id || item.person_contact_id ? 'Référence enregistrée' : 'Non renseigné'}</dd></div></dl>
        {(item.note || item.outcome) && <p className="relationship-note-text">{item.note ?? outcomeLabels[item.outcome as InteractionOutcome] ?? item.outcome}</p>}
        <button type="button" className="text-button" onClick={() => void showHistory(item.company_key)}>{historyKey === item.company_key ? 'Masquer l’historique' : 'Voir l’historique'}</button>
        {historyKey === item.company_key && <div className="relationship-history">{history.length ? <ol>{history.map((event) => <li key={event.id}><strong>{formatParisDateTime(event.happened_at)} · {channelLabels[event.channel]} · {outcomeLabels[event.outcome]}</strong>{event.job_title && <span> · {event.job_title}</span>}{event.contacted_person && <span> · {event.contacted_person}</span>}{event.note && <p>{event.note}</p>}{event.next_action && <small>Suite : {event.next_action}{event.next_action_at && ` · ${formatParisDateTime(event.next_action_at)}`}</small>}</li>)}</ol> : <p>Aucun contact enregistré.</p>}</div>}
        <button type="button" className="text-button administrative-action" onClick={() => void reopen(item)}>Réouvrir comme nouvelle opportunité (correction)</button>
      </article>)}</div>}
    </>}
    {editing && <RelationshipModal relationship={editing} onClose={() => setEditing(null)} onSaved={(saved) => { setEditing(null); setNotice(`${saved.company_name_snapshot} a été mis à jour.`); void load() }} />}
    {dossier && <ApproachWorkspace dossier={dossier} onClose={() => { setDossier(null); void load() }} onRelationshipSaved={(message) => setNotice(message)} />}
  </section>
}
