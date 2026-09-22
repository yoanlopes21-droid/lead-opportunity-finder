import { useState, type FormEvent } from 'react'
import { createCommercialRelationshipFromLead, updateCommercialRelationship } from '../api'
import type { CommercialLead, CommercialRelationship, CommercialRelationshipInput, RelationshipStatus } from '../types'


export const relationshipLabels: Record<RelationshipStatus, string> = {
  contacted: 'Contacté', awaiting_reply: 'En attente de retour', follow_up: 'À relancer', interested: 'Intéressé',
  meeting_scheduled: 'Rendez-vous pris', proposal_sent: 'Proposition envoyée', client: 'Client',
  no_current_need: 'Pas de besoin actuellement', refused: 'Refus', wrong_contact: 'Mauvais interlocuteur',
  do_not_contact: 'Ne plus contacter',
}

const contactDateStatuses = new Set<RelationshipStatus>(['contacted', 'awaiting_reply', 'proposal_sent', 'refused', 'wrong_contact'])
const nextActionStatuses = new Set<RelationshipStatus>(['contacted', 'awaiting_reply', 'follow_up', 'interested', 'meeting_scheduled', 'proposal_sent', 'no_current_need', 'refused'])
const outcomeStatuses = new Set<RelationshipStatus>(['no_current_need', 'refused', 'wrong_contact', 'do_not_contact'])
function localValue(value: string | null | undefined) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}
const nowValue = () => localValue(new Date().toISOString())
const asIso = (value: string) => value ? new Date(value).toISOString() : undefined

type Props = {
  lead?: CommercialLead
  relationship?: CommercialRelationship
  initialStatus?: RelationshipStatus
  onClose: () => void
  onSaved: (relationship: CommercialRelationship) => void
}

export function RelationshipModal({ lead, relationship, initialStatus = 'contacted', onClose, onSaved }: Props) {
  const [status, setStatus] = useState<RelationshipStatus>(relationship?.status ?? initialStatus)
  const [lastContactAt, setLastContactAt] = useState(localValue(relationship?.last_contact_at) || nowValue())
  const [nextActionAt, setNextActionAt] = useState(localValue(relationship?.next_action_at))
  const [note, setNote] = useState(relationship?.note ?? '')
  const [outcome, setOutcome] = useState(relationship?.outcome ?? '')
  const [contactReference, setContactReference] = useState(
    relationship?.contact_point_id ? `contact:${relationship.contact_point_id}` : relationship?.person_contact_id ? `person:${relationship.person_contact_id}` : '',
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const companyName = relationship?.company_name_snapshot ?? lead?.official_name ?? lead?.company_name ?? ''

  async function submit(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError(null)
    const selectedContact = lead?.contacts.find((item) => contactReference === `contact:${item.id}`)
    const input: CommercialRelationshipInput = {
      status,
      last_contact_at: contactDateStatuses.has(status) ? asIso(lastContactAt) : undefined,
      next_action_at: nextActionStatuses.has(status) ? asIso(nextActionAt) : undefined,
      note: note || undefined,
      outcome: outcomeStatuses.has(status) ? outcome || undefined : undefined,
      contact_point_id: contactReference.startsWith('contact:') ? Number(contactReference.split(':')[1]) : undefined,
      person_contact_id: contactReference.startsWith('person:') ? Number(contactReference.split(':')[1]) : undefined,
      used_channel: selectedContact?.type ?? relationship?.used_channel ?? undefined,
    }
    try {
      const saved = relationship
        ? await updateCommercialRelationship(relationship.id, input)
        : await createCommercialRelationshipFromLead(lead!.company_key, input)
      onSaved(saved)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Impossible d’enregistrer ce suivi.')
    } finally { setSaving(false) }
  }

  const nextLabel = status === 'meeting_scheduled' ? 'Date du rendez-vous' : ['no_current_need', 'refused'].includes(status) ? 'Date de recontact' : status === 'follow_up' ? 'Prochaine relance' : 'Prochaine action / relance'
  const nextRequired = status === 'follow_up' || status === 'meeting_scheduled'

  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section className="relationship-modal" role="dialog" aria-modal="true" aria-labelledby="relationship-modal-title">
      <header><div><p className="eyebrow">SUIVI COMMERCIAL</p><h2 id="relationship-modal-title">{companyName}</h2>{lead?.siren && <p>SIREN {lead.siren}</p>}</div><button type="button" className="modal-close" onClick={onClose} aria-label="Fermer">×</button></header>
      <form onSubmit={(event) => void submit(event)}>
        <label>Statut<select value={status} onChange={(event) => setStatus(event.target.value as RelationshipStatus)}>{Object.entries(relationshipLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {contactDateStatuses.has(status) && <label>Date du dernier contact<input type="datetime-local" value={lastContactAt} onChange={(event) => setLastContactAt(event.target.value)} required /></label>}
        {nextActionStatuses.has(status) && <label>{nextLabel}<input type="datetime-local" value={nextActionAt} onChange={(event) => setNextActionAt(event.target.value)} required={nextRequired} /></label>}
        {outcomeStatuses.has(status) && <label>Motif / résultat <span>(facultatif)</span><input value={outcome} onChange={(event) => setOutcome(event.target.value)} maxLength={500} /></label>}
        {lead && (lead.contacts.length > 0 || lead.people.length > 0) && <label>Contact ou canal réellement utilisé <span>(facultatif)</span><select value={contactReference} onChange={(event) => setContactReference(event.target.value)}><option value="">Non renseigné</option>{lead.contacts.filter((item) => !item.stale && item.verification_status !== 'rejected').map((item) => <option key={`contact-${item.id}`} value={`contact:${item.id}`}>{item.type === 'email' ? 'Email' : item.type === 'phone' ? 'Téléphone' : item.type === 'website' ? 'Site web' : 'Canal'} — {item.value}</option>)}{lead.people.map((item) => <option key={`person-${item.id}`} value={`person:${item.id}`}>{item.display_name}{item.role_title ? ` — ${item.role_title}` : ''}</option>)}</select></label>}
        <label>Note courte <span>(facultatif)</span><textarea value={note} onChange={(event) => setNote(event.target.value)} rows={4} maxLength={2000} /></label>
        {status === 'client' && <p className="modal-policy-note">Le statut Client crée une exclusion forte des nouvelles opportunités.</p>}
        {status === 'do_not_contact' && <p className="modal-policy-note warning">« Ne plus contacter » crée une exclusion forte. Utilisez ce statut uniquement sur décision explicite.</p>}
        {status === 'wrong_contact' && <p className="modal-policy-note">L’entreprise reste suivie et n’est pas exclue définitivement. Un autre interlocuteur pourra être recherché plus tard.</p>}
        {error && <p className="inline-error" role="alert">{error}</p>}
        <footer><button type="button" className="secondary-button" onClick={onClose}>Annuler</button><button type="submit" className="primary-button" disabled={saving}>{saving ? 'Enregistrement…' : 'Enregistrer'}</button></footer>
      </form>
    </section>
  </div>
}
