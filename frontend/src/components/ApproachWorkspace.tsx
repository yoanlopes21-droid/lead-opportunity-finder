import { useEffect, useRef, useState } from 'react'
import { fetchApproachPack, fetchCommercialCatalogOffers } from '../api'
import type { CommercialApproachPack, CommercialCatalogOfferSummary, CommercialLead, EmailDraft, ObjectionResponse } from '../types'
import { canCopyEmail, canCopyObjection, canCopyPhone, copyReason, hasEditedEmail, initialEmailEdits, scriptText } from '../approachRules'
import type { EmailEdit } from '../approachRules'
import { InteractionPanel } from './InteractionPanel'
import { relationshipLabels } from '../interactionRules'
import type { RelationshipStatus } from '../types'

type Tab = 'prepare' | 'call' | 'write' | 'proof'
const tabs: { id: Tab; label: string }[] = [{ id: 'prepare', label: 'Préparer' }, { id: 'call', label: 'Appeler' }, { id: 'write', label: 'Écrire' }, { id: 'proof', label: 'Preuves et réglages' }]
const statusLabels: Record<string, string> = { ready_for_call: 'Prêt à contacter', routing_required: 'Passer par le standard', prepared_channel_missing: 'Canal à trouver', verify_contact: 'Contact à vérifier', verify_offer: 'Offre à vérifier', verify_employer: 'Employeur à vérifier', intermediary_not_employer: 'Intermédiaire', suspended: 'Prospection suspendue' }
const communicationLabels: Record<string, string> = { communicable: 'Contenu utilisable', prepared_no_channel: 'Brouillon préparé, canal absent', verify_contact: 'Contact à vérifier', blocked: 'Communication bloquée' }
const phoneLabels: Record<string, string> = { phone_decision_maker: 'Responsable du recrutement', phone_director: 'Direction', phone_hr: 'RH ou recrutement', phone_manager: 'Manager métier', phone_gatekeeper: 'Standard ou accueil', phone_wrong_person: 'Mauvais interlocuteur', phone_busy: 'Personne pressée', phone_email_request: 'Demande d’email', phone_refusal: 'Refus clair' }
const warningPriority: Record<string, number> = { employer_attribution_not_confirmed: 0, some_contacts_not_for_recruitment_outreach: 1, inactive_relationship_history_exists: 2, legal_identity_unconfirmed: 3, generic_publisher_description: 4, channel_missing: 5 }
const emailLabels: Record<EmailDraft['type'], string> = { cold_email: 'Premier email', routing_email: 'Email au standard', email_requested_after_call: 'Email demandé après appel' }
const channelLabels: Record<string, string> = { direct_email: 'Email direct', functional_email: 'Email recrutement', company_switchboard: 'Standard entreprise', direct_phone: 'Téléphone direct', official_contact_page: 'Page contact officielle', professional_url: 'Profil professionnel', none: 'Canal non identifié' }
const explanationLabels: Record<string, string> = {
  channel_missing: 'Aucun canal professionnel utilisable identifié.',
  consultant_profile_missing: 'Profil consultant local manquant.',
  commercial_policy_missing: 'Politique commerciale locale manquante.',
  confirm_direct_employer_before_outreach: 'Confirmer l’employeur direct avant tout contact.',
  employer_attribution_not_confirmed: 'L’employeur direct n’est pas confirmé.',
  legal_identity_unconfirmed: 'Identité juridique à confirmer.',
  generic_publisher_description: 'Description d’annonce générique : vérifier l’employeur.',
  inactive_relationship_history_exists: 'Un ancien historique commercial existe : le consulter avant contact.',
  some_contacts_not_for_recruitment_outreach: 'Certaines coordonnées ne sont pas destinées au recrutement.',
  verify_old_publication_before_current_opening_claim: 'Vérifier que l’annonce ancienne est encore active.',
  publication_over_30_days: 'Annonce de plus de 30 jours à vérifier.',
  client_outreach_before_readiness_cleared: 'Attendre la levée du blocage avant tout contact.',
  validate_price_discount_guarantee_and_exclusivity_manually: 'Valider manuellement prix, remise, garantie et exclusivité.',
  candidate_available_without_evidence: 'Ne pas promettre de candidat disponible sans preuve.',
  unapproved_price_or_discount: 'Ne pas annoncer de prix ou remise non validés.',
  job_listing_count_as_vacancy_count: 'Ne pas présenter le nombre d’annonces comme un nombre de postes.',
  unproven_recruitment_difficulty: 'Ne pas présumer de difficultés de recrutement.',
  unproven_urgency: 'Ne pas affirmer une urgence sans preuve.',
  unproven_vacancy_count: 'Ne pas annoncer un nombre de postes non confirmé.',
  confirmed_hr_recipient: 'Ne pas présenter le destinataire comme un RH confirmé.',
  known_contact_channel: 'Ne pas prétendre disposer d’un canal confirmé.',
  direct_employer_claim: 'Ne pas présenter cet acteur comme employeur direct.',
  first_contact_claim_without_review: 'Ne pas parler de premier contact sans consulter l’historique.',
  prior_exact_role_recruitment_without_reference: 'Ne pas affirmer une mission passée sur ce poste sans référence.',
  guarantee_included_or_complimentary_without_manual_decision: 'Ne pas promettre de garantie incluse ou offerte sans décision.',
  exclusivity_without_manual_decision: 'Ne pas promettre d’exclusivité sans décision.',
  technical_or_clinical_evaluation_expertise_without_evidence: 'Ne pas revendiquer une expertise d’évaluation technique ou clinique sans preuve.',
  personal_occupation_specialty_claim: 'Ne pas revendiquer une spécialité personnelle non établie.',
  targeted_search: 'Recherche ciblée',
  profile_criteria_clarification: 'Clarification des critères du profil',
  preselection: 'Présélection',
  territorial_targeting: 'Ciblage territorial',
  local_or_mobile_occupation_in_documented_territory: 'Métier local ou mobile sur un territoire connu.',
  job_in_documented_territory_without_proven_mobility_constraint: 'Offre sur un territoire connu, sans contrainte de mobilité démontrée.',
  contact_page: 'Page contact officielle',
  approved_client_presentation: 'Présentation client approuvée, uniquement sur demande',
  approved_client_offer_sheet: 'Fiche prestation client approuvée, uniquement sur demande',
}
const readable = (value: string) => explanationLabels[value] ?? value.replace(/_/g, ' ')
const label = (value: string | null | undefined, labels: Record<string, string>) => value ? labels[value] ?? readable(value) : 'Non renseigné'
function safeUrl(value: string) { try { const url = new URL(value); return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null } catch { return null } }
function Objection({ item, copyEnabled, onCopy }: { item: ObjectionResponse; copyEnabled: boolean; onCopy: (text: string) => void }) { return <details className="approach-objection"><summary>{item.objection}</summary><div><p className="approach-client-text">{item.response}</p>{item.follow_up_question && <p className="approach-client-text"><strong>Rebond :</strong> {item.follow_up_question}</p>}<div className="approach-internal"><p><strong>Objectif interne :</strong> {readable(item.objective)}</p><p><strong>Utiliser si :</strong> {readable(item.use_when)}</p>{item.do_not_say.length > 0 && <p><strong>Ne pas affirmer :</strong> {item.do_not_say.map(readable).join(' · ')}</p>}</div><button type="button" className="secondary-button" disabled={!copyEnabled} onClick={() => onCopy(item.response)}>Copier la réponse</button></div></details> }

export function ApproachWorkspace({ lead, onClose, onRelationshipSaved }: { lead: CommercialLead; onClose: () => void; onRelationshipSaved?: (message: string) => void }) {
  const [pack, setPack] = useState<CommercialApproachPack | null>(null)
  const [offers, setOffers] = useState<CommercialCatalogOfferSummary[]>([])
  const [selectedOffer, setSelectedOffer] = useState('')
  const [tab, setTab] = useState<Tab>('prepare')
  const [phoneType, setPhoneType] = useState('')
  const [emailType, setEmailType] = useState<EmailDraft['type']>('cold_email')
  const [edits, setEdits] = useState<Record<string, EmailEdit>>({})
  const [allObjections, setAllObjections] = useState(false)
  const [search, setSearch] = useState('')
  const [wide, setWide] = useState(false)
  const [loading, setLoading] = useState(true)
  const [recomposing, setRecomposing] = useState(false)
  const [confirmRecompose, setConfirmRecompose] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [copyNotice, setCopyNotice] = useState<string | null>(null)
  const [savedStatus, setSavedStatus] = useState<RelationshipStatus | null>(null)
  const savedRef = useRef(false)
  const closeButton = useRef<HTMLButtonElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const loaded = useRef(false)
  const closeRef = useRef(onClose)
  const objectionsRef = useRef(allObjections)
  const closeWorkspace = () => { if (savedRef.current) onRelationshipSaved?.(`${lead.company_name} est maintenant dans le suivi commercial.`); onClose() }
  closeRef.current = closeWorkspace
  objectionsRef.current = allObjections
  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null
    closeButton.current?.focus()
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { if (objectionsRef.current) setAllObjections(false); else closeRef.current() } }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = overflow; previousFocus.current?.focus() }
  }, [])
  async function load(offerCode?: string, replace = false) {
    setError(null)
    if (replace) setRecomposing(true); else setLoading(true)
    try {
      const [next, catalog] = await Promise.all([fetchApproachPack(lead.company_key, lead.department, offerCode), fetchCommercialCatalogOffers()])
      setPack(next); setOffers(catalog.filter((item) => item.enabled_for_prospecting)); setSelectedOffer(next.internal.selected_offer ?? '')
      setPhoneType(next.status === 'routing_required' ? 'phone_gatekeeper' : 'phone_decision_maker')
      setEmailType(next.status === 'routing_required' ? 'routing_email' : 'cold_email')
      setEdits(initialEmailEdits(next)); setConfirmRecompose(false); loaded.current = true
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de charger l’approche commerciale.') }
    finally { setLoading(false); setRecomposing(false) }
  }
  useEffect(() => { if (!loaded.current) { loaded.current = true; void load() } }, [])
  const email = pack?.email.find((item) => item.type === emailType)
  const edited = email ? edits[email.type] ?? { subject: email.subject ?? '', body: email.body ?? '' } : { subject: '', body: '' }
  const dirty = pack ? hasEditedEmail(pack, edits) : false
  const phone = pack?.phone.find((item) => item.type === phoneType)
  const priority = pack?.priority_objections.map((code) => pack.objections.find((item) => item.code === code)).filter((item): item is ObjectionResponse => Boolean(item)) ?? []
  const filtered = pack?.objections.filter((item) => `${item.objection} ${item.response}`.toLocaleLowerCase('fr').includes(search.toLocaleLowerCase('fr'))) ?? []
  const headerWarnings = pack?.evidence.warnings.filter((item) => item !== 'validate_price_discount_guarantee_and_exclusivity_manually').sort((a, b) => (warningPriority[a] ?? 10) - (warningPriority[b] ?? 10)).slice(0, 2) ?? []
  const preferred = lead.contacts.find((item) => item.id === lead.contact_strategy.preferred_contact_point_id)
  const channel = label(lead.contact_strategy.preferred_channel, channelLabels)
  const selectedName = offers.find((item) => item.code === pack?.internal.selected_offer)?.display_name ?? pack?.internal.selected_offer ?? 'Non configurée'
  const emailReady = Boolean(pack && email && canCopyEmail(pack, email, edited))
  const phoneReady = Boolean(pack && phone && canCopyPhone(pack, phone))
  const objectionReady = Boolean(pack && canCopyObjection(pack))
  async function copy(value: string, name: string) { try { await navigator.clipboard.writeText(value); setCopyNotice(`Copie effectuée : ${name.toLocaleLowerCase('fr')}.`) } catch { setCopyNotice('La copie a échoué. Vérifiez l’accès au presse-papiers.') } }
  async function recompose() { if (!pack || recomposing) return; if (dirty) { setConfirmRecompose(true); return } await load(selectedOffer || undefined, true) }
  function edit(field: keyof EmailEdit, value: string) { setEdits((current) => ({ ...current, [emailType]: { ...edited, [field]: value } })) }
  return <div className="approach-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeWorkspace() }}><section className={`approach-drawer ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="approach-title" onKeyDown={(event) => {
    if (event.key !== 'Tab') return
    const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], summary')).filter((item) => item.offsetParent !== null)
    const first = focusable[0], last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }}>
    <header className="approach-header"><div><p className="eyebrow">ESPACE COMMERCIAL · PRÉPARATION</p><h2 id="approach-title">{lead.company_name}</h2><p>{pack?.communication_title ?? 'Préparation de l’approche'}{pack?.entry_offer.location && <> · {pack.entry_offer.location}</>}</p>{savedStatus && <p className="success-notice" role="status">Statut commercial : {relationshipLabels[savedStatus]}</p>}</div><div className="approach-header-actions"><button type="button" className="secondary-button" onClick={() => setWide(!wide)}>{wide ? 'Réduire' : 'Agrandir'}</button><button ref={closeButton} type="button" className="modal-close" onClick={closeWorkspace} aria-label="Fermer le panneau">×</button></div></header>
    {pack && <div className={`approach-status ${pack.communication_status}`}><div><strong>{label(pack.status, statusLabels)}</strong><span>{label(pack.communication_status, communicationLabels)}</span></div><div><small>Interlocuteur</small><strong>{pack.commercial_angle.target_role ?? 'À identifier'}</strong></div><div><small>Canal</small><strong>{channel}{preferred && <> · {preferred.value}</>}</strong></div><div><small>Offre commerciale</small><strong>{selectedName}</strong></div>{headerWarnings.length > 0 && <p>{headerWarnings.map(readable).join(' · ')}</p>}</div>}
    <nav className="prospection-tabs approach-tabs" aria-label="Vues de préparation">{tabs.map((item) => <button type="button" key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} aria-current={tab === item.id ? 'page' : undefined}>{item.label}</button>)}</nav>
    <div className="approach-content">{loading && <div className="state-card" role="status">Préparation du pack commercial…</div>}{error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void load()}>Réessayer</button></div>}{!loading && !error && !pack && <div className="state-card">Aucune approche disponible pour cette entreprise.</div>}
    {pack && tab === 'prepare' && <InteractionPanel companyKey={lead.company_key} offerCode={pack.internal.selected_offer} enabled={pack.communication_status !== 'blocked'} onSaved={(saved) => { savedRef.current = true; setSavedStatus(saved.relationship.status); void load(selectedOffer || undefined, true) }} />}{pack && tab === 'prepare' && <div className="approach-grid"><section className="approach-card"><p className="contact-eyebrow">POURQUOI MAINTENANT</p><h3>Besoin observé</h3><p>{pack.communication_status === 'blocked' ? 'L’offre observée sert à préparer les vérifications. Aucun contact client ne doit être initié dans cet état.' : pack.commercial_angle.contact_rationale ? 'Une offre d’emploi observée donne un point d’entrée concret.' : 'Le contexte doit être vérifié avant tout contact.'}</p><p><strong>Offre d’entrée :</strong> {pack.communication_title}</p>{pack.entry_offer.title !== pack.communication_title && <p><strong>Titre source :</strong> {pack.entry_offer.title}</p>}<p><strong>Localisation :</strong> {pack.entry_offer.location ?? 'Non précisée'}</p></section><section className="approach-card"><p className="contact-eyebrow">ANGLE COMMERCIAL</p><h3>{pack.commercial_angle.active_angle ? pack.commercial_angle.primary_angle ? readable(pack.commercial_angle.primary_angle) : 'À déterminer' : 'Aucun angle actif'}</h3>{pack.commercial_angle.active_angle ? <p><strong>Apport principal :</strong> {pack.commercial_angle.primary_value_proposition ? readable(pack.commercial_angle.primary_value_proposition) : 'À préciser'}</p> : <p>La prospection est suspendue pour cette entreprise.</p>}{pack.commercial_angle.active_angle && pack.commercial_angle.specialty_match === 'strong_specialty_match' && pack.commercial_angle.specialty_label && <p><strong>Spécialité :</strong> {pack.commercial_angle.specialty_label}</p>}{pack.commercial_angle.active_angle && pack.commercial_angle.territorial_relevance === 'useful' && <p><strong>Territoire :</strong> {readable(pack.commercial_angle.territorial_reason)}</p>}</section><section className="approach-card"><p className="contact-eyebrow">CHEMIN DE CONTACT</p><h3>{pack.commercial_angle.target_role ?? 'Interlocuteur à identifier'}</h3><p><strong>Canal :</strong> {channel}{preferred && <> · {preferred.value}</>}</p>{lead.contact_strategy.fallback_channels.length > 0 && <p><strong>Alternatives :</strong> {lead.contact_strategy.fallback_channels.map((item) => label(item, channelLabels)).join(' · ')}</p>}<p><strong>État :</strong> {label(pack.commercial_angle.readiness, statusLabels)}</p></section><section className="approach-card"><p className="contact-eyebrow">AVANT D’AGIR</p><h3>Vérifications</h3>{pack.evidence.warnings.length ? <ul>{pack.evidence.warnings.map((item) => <li key={item}>{readable(item)}</li>)}</ul> : <p>Aucune vérification supplémentaire signalée.</p>}</section></div>}
    {pack && tab === 'call' && <div className="approach-stack"><div className="approach-toolbar"><label>Situation d’appel<select value={phoneType} onChange={(event) => setPhoneType(event.target.value)}>{pack.phone.map((item) => <option key={item.type} value={item.type}>{label(item.type, phoneLabels)}</option>)}</select></label><button type="button" className="primary-button" disabled={!phoneReady} onClick={() => phone && void copy(scriptText(phone, priority), 'Script')}>Copier le script</button></div>{phone && <>{!phoneReady && <p className="approach-readiness-note">{copyReason(pack, phone.ready_to_copy)}</p>}{[["A. OUVERTURE", phone.opening], ["B. SUITE IMMÉDIATE", phone.first_30_seconds], ["C. SUITE CONVERSATIONNELLE", phone.continuation]].map(([title, content]) => content && <section className="approach-card approach-script" key={title}><p className="contact-eyebrow">{title}</p><p>{content}</p></section>)}{phone.qualification_questions.length > 0 && <section className="approach-card"><p className="contact-eyebrow">D. QUESTIONS RESTANTES</p><div className="approach-questions">{phone.qualification_questions.map((question) => <p key={question}>{question}</p>)}</div></section>}{phone.meeting_transition && <section className="approach-card approach-script"><p className="contact-eyebrow">E. TRANSITION RDV</p><p>{phone.meeting_transition_after_response ?? phone.meeting_transition}</p></section>}</>}{priority.length > 0 && <section className="approach-card"><p className="contact-eyebrow">F. OBJECTIONS PRIORITAIRES</p>{priority.map((item) => <Objection key={item.code} item={item} copyEnabled={objectionReady} onCopy={(value) => void copy(value, 'Réponse')} />)}<button type="button" className="text-button" onClick={() => setAllObjections(!allObjections)}>{allObjections ? 'Masquer toutes les objections' : `Voir toutes les objections (${pack.objections.length})`}</button></section>}{allObjections && <section className="approach-card"><h3>Bibliothèque des objections</h3><label>Rechercher<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Mots clés" /></label>{filtered.length ? filtered.map((item) => <Objection key={item.code} item={item} copyEnabled={objectionReady} onCopy={(value) => void copy(value, 'Réponse')} />) : <p>Aucune objection trouvée.</p>}</section>}{!phone?.opening && <div className="state-card">Aucun script client disponible dans cet état. Consultez les vérifications.</div>}</div>}
    {pack && tab === 'write' && <div className="approach-stack"><div className="approach-toolbar"><label>Type d’email<select value={emailType} onChange={(event) => setEmailType(event.target.value as EmailDraft['type'])}>{pack.email.map((item) => <option key={item.type} value={item.type}>{emailLabels[item.type]}</option>)}</select></label><span>{emailReady ? 'Prêt à copier' : 'Non prêt à copier'}</span></div>{email && <>{!emailReady && <p className="approach-readiness-note">{copyReason(pack, email.ready_to_copy, email.required_event)}</p>}{(email.subject || email.body) ? <section className="approach-card approach-email"><p className="contact-eyebrow">BROUILLON LOCAL · AUCUN ENVOI</p><label>Objet<input value={edited.subject} onChange={(event) => edit('subject', event.target.value)} /></label><label>Corps du mail<textarea rows={14} value={edited.body} onChange={(event) => edit('body', event.target.value)} /></label>{dirty && <p className="approach-edit-note">Vos modifications restent jusqu’à la fermeture ou une recomposition confirmée.</p>}<div className="approach-copy-actions"><button type="button" className="secondary-button" disabled={!emailReady} onClick={() => void copy(edited.subject, 'Objet')}>Copier l’objet</button><button type="button" className="secondary-button" disabled={!emailReady} onClick={() => void copy(edited.body, 'Mail')}>Copier le mail</button><button type="button" className="primary-button" disabled={!emailReady} onClick={() => void copy(`Objet : ${edited.subject}\n\n${edited.body}`, 'Objet et mail')}>Copier objet + mail</button></div></section> : <div className="state-card">{pack.communication_status === 'blocked' ? 'Aucun mail client disponible tant que la communication est bloquée.' : email.required_event ? 'Une demande réelle après appel est nécessaire pour cette variante.' : 'Aucun mail client disponible dans cet état.'}</div>}<p className="approach-attachment">Pièce jointe recommandée : {email.attachment_recommendation === 'none' ? 'Aucune' : readable(email.attachment_recommendation)}. Aucun document n’est joint automatiquement.</p></>}</div>}
    {pack && tab === 'proof' && <div className="approach-stack"><section className="approach-card"><p className="contact-eyebrow">PREUVES / FIABILITÉ · USAGE INTERNE</p><h3>Faits utilisés</h3>{pack.evidence.claims_used.length ? <ul className="approach-evidence">{pack.evidence.claims_used.map((claim, index) => <li key={`${claim.source_reference}-${index}`}><strong>{claim.claim}</strong><small>Source : {readable(claim.source_reference)} · {readable(claim.confidence)}</small>{safeUrl(claim.source_reference) && <a href={safeUrl(claim.source_reference)!} target="_blank" rel="noreferrer">Ouvrir la preuve</a>}</li>)}</ul> : <p>Aucun fait client utilisable dans cet état.</p>}{pack.entry_offer.description_excerpt && <p><strong>Détail d’annonce :</strong> {pack.entry_offer.description_excerpt}</p>}{pack.evidence.sources.length > 0 && <details className="approach-source-details"><summary>Références utilisées ({pack.evidence.sources.length})</summary><ul>{pack.evidence.sources.map((source) => <li key={source}>{safeUrl(source) ? <a href={safeUrl(source)!} target="_blank" rel="noreferrer">Annonce ou source en ligne</a> : source.startsWith('starter.') || source.startsWith('enhanced.') ? 'Catalogue commercial local' : source.startsWith('specialties:') || source.startsWith('territories:') ? 'Profil consultant local' : readable(source)}</li>)}</ul></details>}{pack.entry_offer.source_urls.map((url) => safeUrl(url) && <p key={url}><a href={safeUrl(url)!} target="_blank" rel="noreferrer">Voir l’annonce source</a></p>)}</section><section className="approach-card"><h3>Points de vigilance et interdictions</h3><div className="approach-grid"><div><strong>À vérifier</strong><ul>{pack.evidence.warnings.map((item) => <li key={item}>{readable(item)}</li>)}</ul></div><div><strong>Ne pas affirmer</strong><ul>{pack.evidence.do_not_claim.map((item) => <li key={item}>{readable(item)}</li>)}</ul></div></div></section><section className="approach-card"><p className="contact-eyebrow">RÉGLAGES COMMERCIAUX</p><h3>Offre sélectionnée : {selectedName}</h3><div className="approach-toolbar"><label>Choix manuel de l’offre<select value={selectedOffer} onChange={(event) => { setSelectedOffer(event.target.value); setConfirmRecompose(false) }}><option value="">Aucune offre sélectionnée</option>{offers.map((item) => <option key={item.code} value={item.code}>{item.display_name}</option>)}</select></label><button type="button" className="primary-button" disabled={!selectedOffer || recomposing || selectedOffer === pack.internal.selected_offer} onClick={() => void recompose()}>{recomposing ? 'Recomposition…' : 'Recomposer'}</button></div>{confirmRecompose && <div className="approach-confirm" role="alert"><p>Recomposer remplacera vos modifications de l’objet et du corps.</p><button type="button" className="primary-button" onClick={() => void load(selectedOffer || undefined, true)}>Recomposer et remplacer</button><button type="button" className="secondary-button" onClick={() => setConfirmRecompose(false)}>Conserver mes modifications</button></div>}<p className="approach-edit-note">Le changement prend effet après recomposition. Les décisions commerciales restent manuelles.</p><div className="approach-manual-grid"><span>Prix : décision manuelle</span><span>Remise : décision manuelle</span><span>Garantie : décision manuelle</span><span>Exclusivité : décision manuelle</span></div><p>Présentation demandée : document client uniquement. Détail de prestation demandé : document client approprié. Aucun document n’est envoyé ici.</p></section></div>}
    </div>{copyNotice && <div className="approach-copy-notice" role="status">{copyNotice}</div>}
  </section></div>
}
