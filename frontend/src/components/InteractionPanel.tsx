import { useEffect, useState } from 'react'
import { createCommercialInteraction, fetchInteractionHistory } from '../api'
import { availableOutcomes, channelLabels, outcomeLabels, outcomeStatuses, relationshipLabels } from '../interactionRules'
import type { CommercialInteraction, InteractionChannel, InteractionOutcome, InteractionCreate, InteractionSaved } from '../types'

const dateLabel = (value: string) => new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
const localNow = () => {
  const date = new Date()
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}

export function InteractionPanel({ companyKey, offerCode, enabled, onSaved }: {
  companyKey: string; offerCode: string | null; enabled: boolean
  onSaved: (result: InteractionSaved) => void
}) {
  const [items, setItems] = useState<CommercialInteraction[]>([])
  const [open, setOpen] = useState(false)
  const [channel, setChannel] = useState<InteractionChannel | ''>('')
  const [outcome, setOutcome] = useState<InteractionOutcome | ''>('')
  const [happenedAt, setHappenedAt] = useState(localNow)
  const [person, setPerson] = useState('')
  const [note, setNote] = useState('')
  const [priority, setPriority] = useState('')
  const [nextAction, setNextAction] = useState('')
  const [nextAt, setNextAt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [requestId, setRequestId] = useState(() => crypto.randomUUID())
  useEffect(() => { fetchInteractionHistory(companyKey).then((page) => setItems(page.items)).catch(() => setError('Historique indisponible.')) }, [companyKey])
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!channel || !outcome || busy) return
    setBusy(true); setError(null)
    const input: InteractionCreate = {
      request_id: requestId, company_key: companyKey, happened_at: new Date(happenedAt).toISOString(),
      channel, outcome, offer_code: offerCode ?? undefined,
      contacted_person: person.trim() || undefined, note: note.trim() || undefined,
      next_action: nextAction || undefined, next_action_at: nextAt ? new Date(nextAt).toISOString() : undefined,
      priority_expressed: ['no_answer', 'email_sent', 'switchboard'].includes(outcome) ? undefined : priority.trim() || undefined,
    }
    try {
      const saved = await createCommercialInteraction(input)
      setItems((current) => [saved.interaction, ...current.filter((item) => item.id !== saved.interaction.id)])
      setNotice('Résultat enregistré dans le suivi commercial.')
      setOpen(false); setChannel(''); setOutcome(''); setPerson(''); setNote(''); setPriority(''); setNextAction(''); setNextAt('')
      setHappenedAt(localNow()); setRequestId(crypto.randomUUID()); onSaved(saved)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Enregistrement impossible.') }
    finally { setBusy(false) }
  }
  return <section className="approach-card interaction-panel"><div className="approach-toolbar"><div><p className="contact-eyebrow">SUIVI COMMERCIAL</p><h3>Historique</h3></div><button type="button" className="primary-button" disabled={!enabled || busy} onClick={() => { setOpen(!open); setNotice(null); setError(null) }}>Enregistrer le résultat</button></div>
    {!enabled && <p>Prospection suspendue : aucun nouveau résultat ne peut être enregistré ici.</p>}
    {notice && <p className="success-notice" role="status">{notice}</p>}{error && <p className="error-state" role="alert">{error}</p>}
    {open && <form className="interaction-form" onSubmit={(event) => void submit(event)}><p>Enregistrez uniquement une action réellement effectuée.</p>
      <div className="interaction-fields"><label>Canal utilisé<select required value={channel} onChange={(event) => { setChannel(event.target.value as InteractionChannel | ''); setOutcome('') }}><option value="">Choisir</option>{Object.entries(channelLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>Résultat<select required value={outcome} onChange={(event) => setOutcome(event.target.value as InteractionOutcome | '')}><option value="">Choisir</option>{channel && availableOutcomes(channel).map((value) => <option key={value} value={value}>{outcomeLabels[value]}</option>)}</select></label>
      <label>Date et heure réelles<input required type="datetime-local" value={happenedAt} max={localNow()} onChange={(event) => setHappenedAt(event.target.value)} /></label>
      <label>Personne ou fonction contactée <span>(facultatif)</span><input value={person} maxLength={255} onChange={(event) => setPerson(event.target.value)} /></label>
      <label>Prochaine action <span>(facultatif)</span><select value={nextAction} onChange={(event) => setNextAction(event.target.value)}><option value="">Aucune</option><option value="Rappeler">Rappeler</option><option value="Envoyer un email">Envoyer un email</option><option value="Faire un point">Faire un point</option><option value="Préparer une proposition">Préparer une proposition</option><option value="Autre">Autre</option></select></label>
      <label>Date de prochaine action <span>(facultatif)</span><input type="datetime-local" value={nextAt} onChange={(event) => setNextAt(event.target.value)} /></label></div>
      {outcome === 'callback_requested' && !nextAt && <p className="approach-readiness-note">Ajoutez une date de rappel si elle a été convenue. Aucune date ne sera inventée.</p>}
      {outcome === 'meeting_scheduled' && !nextAt && <p className="approach-readiness-note">Indiquez la date du rendez-vous pour enregistrer ce résultat.</p>}
      <details className="interaction-extra"><summary>Notes et contexte (facultatif)</summary><div><label>Note<textarea rows={2} value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} /></label>
      {!['no_answer', 'email_sent', 'switchboard'].includes(outcome) && <label>Priorité exprimée par le prospect<input value={priority} maxLength={500} onChange={(event) => setPriority(event.target.value)} /></label>}</div></details>
      {outcome && <p className="interaction-proposed">Statut appliqué : <strong>{relationshipLabels[outcomeStatuses[outcome]]}</strong></p>}
      <div className="approach-copy-actions"><button className="primary-button" type="submit" disabled={busy || !outcome || !channel || (outcome === 'meeting_scheduled' && !nextAt)}>{busy ? 'Enregistrement…' : 'Confirmer le résultat'}</button><button type="button" className="secondary-button" onClick={() => setOpen(false)} disabled={busy}>Annuler</button></div>
    </form>}
    {items.length === 0 ? <p>Aucun contact enregistré.</p> : <ol className="interaction-history">{items.map((item) => <li key={item.id}><strong>{dateLabel(item.happened_at)} · {channelLabels[item.channel]} · {outcomeLabels[item.outcome]}</strong>{item.contacted_person && <span>Interlocuteur : {item.contacted_person}</span>}{item.note && <span>{item.note}</span>}{item.next_action && <span>Suite : {item.next_action}{item.next_action_at && ` · ${dateLabel(item.next_action_at)}`}</span>}</li>)}</ol>}
  </section>
}
