import { useState, type FormEvent } from 'react'
import { addHumanContact, addNeedVerification } from '../api'
import { parisInputNow, parisLocalToIso } from '../parisTime'

export function DossierEvidenceActions({ relationshipId, needId, needActive, onChanged }: {
  relationshipId: number; needId: string | null; needActive: boolean
  onChanged: () => void
}) {
  const [contactType, setContactType] = useState<'phone' | 'email' | 'professional_url' | 'other'>('phone')
  const [contactValue, setContactValue] = useState('')
  const [personName, setPersonName] = useState('')
  const [roleTitle, setRoleTitle] = useState('')
  const [provenance, setProvenance] = useState<'switchboard' | 'contact_person' | 'other'>('switchboard')
  const [contactAt, setContactAt] = useState(parisInputNow)
  const [verificationAt, setVerificationAt] = useState(parisInputNow)
  const [verificationChannel, setVerificationChannel] = useState('phone')
  const [verificationNote, setVerificationNote] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function saveContact(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(null); setNotice(null)
    try {
      await addHumanContact(relationshipId, {
        channel_type: contactType, value: contactValue,
        person_name: personName.trim() || undefined, role_title: roleTitle.trim() || undefined,
        verified_at: parisLocalToIso(contactAt), provenance,
      })
      setContactValue(''); setPersonName(''); setRoleTitle('')
      setNotice('Coordonnée professionnelle enregistrée avec sa provenance humaine.'); onChanged()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Enregistrement impossible.') }
    finally { setBusy(false) }
  }

  async function saveVerification(event: FormEvent) {
    event.preventDefault()
    if (!needId) return
    setBusy(true); setError(null); setNotice(null)
    try {
      await addNeedVerification(relationshipId, {
        need_id: needId, verified_at: parisLocalToIso(verificationAt),
        channel: verificationChannel, note: verificationNote.trim() || undefined,
      })
      setVerificationNote('')
      setNotice('Confirmation du besoin enregistrée pour ce besoin uniquement.'); onChanged()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Enregistrement impossible.') }
    finally { setBusy(false) }
  }

  return <section className="approach-card dossier-evidence-actions"><p className="contact-eyebrow">PREUVES HUMAINES</p><h3>Informations réellement obtenues</h3>
    {notice && <p className="success-notice" role="status">{notice}</p>}{error && <p className="inline-error" role="alert">{error}</p>}
    <details><summary>Ajouter une coordonnée professionnelle</summary><form onSubmit={(event) => void saveContact(event)} className="interaction-form"><div className="interaction-fields">
      <label>Canal<select value={contactType} onChange={(event) => setContactType(event.target.value as typeof contactType)}><option value="phone">Téléphone</option><option value="email">Email</option><option value="professional_url">Profil professionnel</option><option value="other">Autre</option></select></label>
      <label>Coordonnée<input required value={contactValue} onChange={(event) => setContactValue(event.target.value)} /></label>
      <label>Nom <span>(facultatif)</span><input value={personName} onChange={(event) => setPersonName(event.target.value)} /></label>
      <label>Fonction <span>(facultatif)</span><input value={roleTitle} onChange={(event) => setRoleTitle(event.target.value)} /></label>
      <label>Provenance<select value={provenance} onChange={(event) => setProvenance(event.target.value as typeof provenance)}><option value="switchboard">Standard</option><option value="contact_person">Interlocuteur</option><option value="other">Autre</option></select></label>
      <label>Vérifiée le <span>(Europe/Paris)</span><input type="datetime-local" required value={contactAt} onChange={(event) => setContactAt(event.target.value)} /></label>
    </div><button type="submit" className="primary-button" disabled={busy}>{busy ? 'Enregistrement…' : 'Enregistrer la coordonnée'}</button></form></details>
    {needId && needActive && <details><summary>Confirmer que ce besoin existe toujours</summary><form onSubmit={(event) => void saveVerification(event)} className="interaction-form"><p>Cette preuve lève uniquement une vérification d’ancienneté du besoin. Elle ne confirme ni l’employeur ni son identité.</p><div className="interaction-fields">
      <label>Canal<select value={verificationChannel} onChange={(event) => setVerificationChannel(event.target.value)}><option value="phone">Téléphone</option><option value="email">Email</option><option value="professional_network">Réseau professionnel</option><option value="other">Autre</option></select></label>
      <label>Confirmé le <span>(Europe/Paris)</span><input type="datetime-local" required value={verificationAt} onChange={(event) => setVerificationAt(event.target.value)} /></label>
      <label>Note <span>(facultatif)</span><input value={verificationNote} onChange={(event) => setVerificationNote(event.target.value)} /></label>
    </div><button type="submit" className="primary-button" disabled={busy}>{busy ? 'Enregistrement…' : 'Enregistrer la confirmation'}</button></form></details>}
  </section>
}
