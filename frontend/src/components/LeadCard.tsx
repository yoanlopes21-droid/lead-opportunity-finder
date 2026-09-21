import type { ActiveJobOffer, CommercialLead, ContactPoint, PersonContact, Provenance, ScoreReason } from '../types'

type LeadCardProps = { lead: CommercialLead }

const categoryMeta: Record<string, { label: string; tone: string }> = {
  '🔥 priorité très forte': { label: 'Priorité très forte', tone: 'very-high' }, '🟢 bon prospect': { label: 'Bon prospect', tone: 'good' }, '🟠 à surveiller / priorité moyenne': { label: 'À surveiller', tone: 'medium' }, '⚪ faible priorité': { label: 'Faible priorité', tone: 'low' },
}
const channelLabels: Record<string, string> = { direct_email: 'Email direct', functional_email: 'Email recrutement', company_switchboard: 'Standard entreprise', direct_phone: 'Téléphone direct', official_contact_page: 'Page contact officielle', professional_url: 'Profil professionnel', none: 'Aucun canal exploitable' }
const targetLabels: Record<string, string> = { hr: 'Responsable RH / recrutement', recruitment: 'Service recrutement', director: 'Direction / gérance', company_general: 'Standard entreprise', intermediary: 'Intermédiaire', unresolved: 'Cible non identifiée' }
const scopeLabels: Record<string, string> = { company: 'Entreprise', local: 'Local', intermediary: 'Intermédiaire', unknown: 'Portée à confirmer' }
const webLabels: Record<string, string> = { high_confidence: 'Site officiel vérifié', review_needed: 'Vérification du site à revoir', ambiguous: 'Site ambigu', rejected: 'Aucun site officiel fiable identifié' }
const sectorLabels: Record<string, string> = { private: 'Secteur privé', public: 'Secteur public', nonprofit: 'Association / organisme à but non lucratif', unknown: 'Secteur non précisé' }
const confidenceLabels: Record<string, string> = { high_confidence: 'Confiance élevée', confirmed: 'Confiance élevée', high: 'Confiance élevée', medium: 'Confiance moyenne', low: 'Confiance faible', review_needed: 'À vérifier', ambiguous: 'Ambigu', rejected: 'Rejeté', unresolved: 'À enrichir', unverified: 'À vérifier' }
const providerLabels: Record<string, string> = { official_web: 'Site officiel', offer_description: 'Offre d’emploi', societe_com: 'Societe.com' }
const reasonLabels: Record<string, string> = { competing_domains: 'Plusieurs domaines concurrents', third_party_commercial_aggregator: 'Site tiers / comparateur', third_party_directory: 'Annuaire tiers', official_page_contact: 'Page contact', official_page_person_role: 'Page équipe', page_contact: 'Page contact', role_rh: 'Page équipe', email_public: 'Coordonnée publique' }
const fmtDate = (value: string | null) => value ? new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(value)) : null
const label = (value: string, labels: Record<string, string>) => labels[value] ?? (value.includes('_') ? value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) : value)
const contactLabel = (contact: ContactPoint) => ({ email: 'Email', phone: 'Téléphone', website: 'Site web', professional_url: 'Profil professionnel' }[contact.type] ?? contact.type)
function hrefFor(contact: ContactPoint) { if (contact.type === 'email') return `mailto:${contact.value}`; if (contact.type === 'phone') return `tel:${contact.value}`; return contact.type === 'website' || contact.type === 'professional_url' ? contact.value : undefined }

function ReasonList({ title, reasons, variant = 'neutral' }: { title: string; reasons: ScoreReason[]; variant?: string }) {
  return reasons.length ? <div className={`reason-list ${variant}`}><span>{title}</span><ul>{reasons.map((reason) => <li key={reason.code}>{reason.message}</li>)}</ul></div> : null
}
function provenanceDescription(item: Provenance) {
  if (item.reason === 'official_page_contact') return 'Coordonnées professionnelles publiées sur la page contact.'
  if (item.reason === 'official_page_person_role') return 'Fonction professionnelle publiée sur la page équipe.'
  if (item.reason === 'email_public') return 'Coordonnée professionnelle publiée dans l’offre.'
  return item.short_excerpt ? `${item.short_excerpt.slice(0, 150)}${item.short_excerpt.length > 150 ? '…' : ''}` : null
}
function ProvenanceList({ items }: { items: Provenance[] }) {
  const unique = items.filter((item, index, rows) => rows.findIndex((other) => `${other.source_url}|${other.reason}|${other.provider}` === `${item.source_url}|${item.reason}|${item.provider}`) === index)
  return unique.length ? <ul className="provenance-list">{unique.map((item) => <li key={item.id}><strong>{label(item.provider, providerLabels)}{item.reason && <> — {label(item.reason, reasonLabels)}</>}</strong>{item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">Ouvrir la source</a>}{provenanceDescription(item) && <small>{provenanceDescription(item)}</small>}</li>)}</ul> : <p className="muted">Aucune provenance compacte disponible.</p>
}
function ContactValue({ contact }: { contact: ContactPoint }) { const href = hrefFor(contact); return href ? <a href={href} target={contact.type === 'website' || contact.type === 'professional_url' ? '_blank' : undefined} rel="noreferrer">{contact.value}</a> : <span>{contact.value}</span> }
function JobOfferRow({ offer }: { offer: ActiveJobOffer }) {
  const location = offer.display_location ?? 'Localisation non précisée'
  const details = [location, offer.contract_type, fmtDate(offer.published_at)].filter(Boolean)
  return <li><div><strong>{offer.title}</strong><span>{details.join(' · ') || 'Date non précisée'}{offer.age_days !== null && offer.age_days >= 0 && <> · il y a {offer.age_days} j</>}{offer.salary && <small>Salaire : {offer.salary}</small>}</span></div>{offer.source_url && <a href={offer.source_url} target="_blank" rel="noreferrer">Voir l’offre</a>}</li>
}
function Person({ person, contacts }: { person: PersonContact; contacts: ContactPoint[] }) {
  const linked = contacts.filter((contact) => person.contact_point_ids.includes(contact.id))
  return <article className="person"><strong>{person.display_name}</strong><span>{person.role_title ?? label(person.relevance, targetLabels)} · {label(person.scope, scopeLabels)} · {label(person.confidence, confidenceLabels)}</span>{linked.map((contact) => <ContactValue key={contact.id} contact={contact} />)}{person.warnings.map((warning) => <small key={warning}>{warning}</small>)}<details><summary>Provenance</summary><ProvenanceList items={person.provenance} /></details></article>
}

export function LeadCard({ lead }: LeadCardProps) {
  const category = categoryMeta[lead.category] ?? { label: lead.category, tone: 'low' }
  const strategy = lead.contact_strategy
  const contact = lead.contacts.find((item) => item.id === strategy.preferred_contact_point_id)
  const person = lead.people.find((item) => item.id === strategy.preferred_person_contact_id)
  const unresolved = strategy.preferred_channel === 'none'
  const alternatives = lead.contacts.filter((item) => item.id !== strategy.preferred_contact_point_id && !item.stale && item.verification_status !== 'rejected').filter((item, index, rows) => {
    const source = item.type === 'website' || item.type === 'professional_url' ? (item.value || item.evidence[0]?.source_url) : `${item.type}:${item.value}`
    return rows.findIndex((other) => (other.type === 'website' || other.type === 'professional_url' ? (other.value || other.evidence[0]?.source_url) : `${other.type}:${other.value}`) === source) === index
  })
  const website = lead.contactability_summary.official_web
  const highlights = lead.positive_reasons.slice(0, 2)
  const signals = lead.latent_signals.filter((signal) => signal.active).slice(0, 2)
  const relationship = lead.employer_relationship_status

  return <article className="lead-card">
    <header className="lead-card-header"><div><p className={`category-badge ${category.tone}`}><span aria-hidden="true">{lead.category.slice(0, 2)}</span> {category.label}</p><h2>{lead.company_name}</h2>{lead.official_name && lead.official_name !== lead.company_name && <p className="official-name">{lead.official_name}</p>}</div><div className="score" aria-label={`Score commercial ${lead.total_score} sur 100`}><small>Score commercial</small><strong>{lead.total_score}</strong><span>/100</span></div></header>
    {relationship === 'intermediary' && <p className="relationship-note"><strong>Cabinet / intermédiaire.</strong> Les coordonnées sont celles de l’intermédiaire.</p>}{relationship === 'intermediary_suspected' && <p className="relationship-note warning"><strong>Intermédiaire possible.</strong> Vérifiez la relation employeur avant contact.</p>}
    <dl className="lead-facts"><div><dt>Localisation</dt><dd>{lead.primary_location ?? 'Non précisée'}</dd></div><div><dt>Secteur</dt><dd>{label(lead.entity_sector_type, sectorLabels)}</dd></div>{lead.employee_range && <div><dt>Effectif</dt><dd>{lead.employee_range === 'unknown' ? 'Non précisé' : lead.employee_range}</dd></div>}<div><dt>Offres d’emploi actives</dt><dd>{lead.active_offer_count}</dd></div><div><dt>Diversité de rôles</dt><dd>{lead.role_diversity}</dd></div>{fmtDate(lead.newest_offer_date) && <div><dt>Offre la plus récente</dt><dd>{fmtDate(lead.newest_offer_date)}</dd></div>}</dl>
    {lead.representative_roles.length > 0 && <div className="roles">{lead.representative_roles.slice(0, 4).map((role) => <span key={role}>{role}</span>)}</div>}
    {website.verified_site_status && <p className={`website-status ${website.verified_site_status}`}>{label(website.verified_site_status, webLabels)}{website.verified_domain && <> · {website.verified_domain}</>}{website.warnings[0] && <small>{label(website.warnings[0], reasonLabels)}</small>}</p>}
    {lead.active_job_offers.length > 0 && <section className="job-offers"><h3>Besoins de recrutement <span>— {lead.active_offer_count} offre{lead.active_offer_count > 1 ? 's' : ''} active{lead.active_offer_count > 1 ? 's' : ''}</span></h3><ul>{lead.active_job_offers.slice(0, 3).map((offer) => <JobOfferRow key={`${offer.source}:${offer.offer_id}`} offer={offer} />)}</ul>{lead.active_job_offers.length > 3 && <details><summary>Voir les {lead.active_job_offers.length} offres</summary><ul>{lead.active_job_offers.slice(3).map((offer) => <JobOfferRow key={`${offer.source}:${offer.offer_id}`} offer={offer} />)}</ul></details>}</section>}
    {lead.local_opportunities.length > 1 && <section className="local-opportunities"><h3>Implantations / besoins</h3><ul>{lead.local_opportunities.slice(0, 5).map((item) => <li key={item.local_key}>{item.location_label ?? item.commune ?? 'Localité non précisée'} <span>— {item.active_offer_count} offre{item.active_offer_count > 1 ? 's' : ''}</span>{item.contact_point_ids.length > 0 && <small>Coordonnée locale disponible</small>}</li>)}</ul>{lead.local_opportunities.length > 5 && <p>+ {lead.local_opportunities.length - 5} autres localités</p>}<p>Les localités décrivent des besoins observés, pas nécessairement des établissements juridiques.</p></section>}
    <section className={`recommended-contact ${unresolved ? 'unresolved' : ''}`}><div><p className="contact-eyebrow">CONTACT RECOMMANDÉ</p><h3>{unresolved ? 'Aucun canal exploitable identifié' : person?.display_name ?? label(strategy.target_type, targetLabels)}</h3>{!unresolved && <p className="contact-channel">{label(strategy.preferred_channel, channelLabels)}{contact && <> · <ContactValue contact={contact} /></>}</p>}{person?.role_title && <p className="muted">{person.role_title}</p>}<p className="contact-hint">{strategy.short_context}</p></div><div className="contact-meta"><span>{unresolved ? 'À enrichir' : label(strategy.confidence, confidenceLabels)}</span><small>{label(strategy.scope, scopeLabels)}</small></div></section>
    <details className="contact-details"><summary>Pourquoi ce contact ?</summary><div className="contact-detail-grid"><section><h3>Contexte avant contact</h3><p><strong>Cible :</strong> {label(strategy.target_type, targetLabels)}</p><p><strong>Canal :</strong> {label(strategy.preferred_channel, channelLabels)} · {label(strategy.scope, scopeLabels)}</p><p>{strategy.short_context}</p>{strategy.fallback_channels.length > 0 && <p><strong>Alternatives prévues :</strong> {strategy.fallback_channels.map((channel) => label(channel, channelLabels)).join(', ')}</p>}</section><section><h3>Informations à vérifier</h3>{strategy.warnings.length === 0 && strategy.missing_information.length === 0 ? <p className="muted">Aucun point de vigilance signalé.</p> : <ul className="warning-list">{[...strategy.warnings, ...strategy.missing_information].map((message) => <li key={message}>{message}</li>)}</ul>}</section><section><h3>Provenance</h3><ProvenanceList items={strategy.evidence} /></section></div>
      {alternatives.length > 0 && <section className="alternatives"><h3>Coordonnées alternatives</h3><div>{alternatives.map((item) => <article key={item.id}><span>{contactLabel(item)} · {label(item.scope, scopeLabels)}</span><ContactValue contact={item} />{item.warnings[0] && <small>{item.warnings[0]}</small>}<details><summary>Source</summary><ProvenanceList items={item.evidence} /></details></article>)}</div></section>}
      {lead.people.length > 0 && <section className="people"><h3>Personnes identifiées</h3>{lead.people.map((item) => <Person key={item.id} person={item} contacts={lead.contacts} />)}</section>}
    </details>
    {(highlights.length > 0 || signals.length > 0 || lead.adjustments.length > 0 || lead.penalties.length > 0) && <details className="score-explanation"><summary>Pourquoi ce score ?</summary><div className="explanation-content"><ReasonList title="Points forts" reasons={highlights} />{signals.length > 0 && <div className="reason-list"><span>Signaux observés</span><ul>{signals.map((signal) => <li key={signal.name}>{signal.explanation}</li>)}</ul></div>}<ReasonList title="Ajustements" reasons={lead.adjustments} /><ReasonList title="Points de vigilance" reasons={lead.penalties} variant="penalty" /></div></details>}
  </article>
}
