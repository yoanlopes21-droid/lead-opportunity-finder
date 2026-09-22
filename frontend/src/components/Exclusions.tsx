import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { createCommercialExclusion, deleteCommercialExclusion, fetchCommercialExclusions, importCommercialExclusionCsv, previewCommercialExclusionCsv } from '../api'
import type { CommercialExclusion, ExclusionImportPreview, ExclusionImportReport, ExclusionType } from '../types'


const typeLabels: Record<string, string> = {
  current_client: 'Client actuel', recent_prospect: 'Prospect récent', manual_exclusion: 'Exclusion manuelle',
}
const today = new Date().toISOString().slice(0, 10)
const formatDate = (value: string | null) => value
  ? new Intl.DateTimeFormat('fr-FR', { day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(value))
  : '—'

export function Exclusions({ embedded = false }: { embedded?: boolean }) {
  const [items, setItems] = useState<CommercialExclusion[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [exclusionType, setExclusionType] = useState<ExclusionType>('current_client')
  const [siren, setSiren] = useState('')
  const [reason, setReason] = useState('')
  const [startsAt, setStartsAt] = useState(today)
  const [expiresAt, setExpiresAt] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [csvContent, setCsvContent] = useState('')
  const [csvName, setCsvName] = useState('')
  const [preview, setPreview] = useState<ExclusionImportPreview | null>(null)
  const [importReport, setImportReport] = useState<ExclusionImportReport | null>(null)
  const [importError, setImportError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setItems((await fetchCommercialExclusions({ type: typeFilter, status: statusFilter, search })).items) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de charger les exclusions.') }
    finally { setLoading(false) }
  }, [typeFilter, statusFilter, search])

  useEffect(() => { void load() }, [load])

  async function submit(event: FormEvent) {
    event.preventDefault(); setSaving(true); setFormError(null); setNotice(null)
    try {
      await createCommercialExclusion({
        company_name: companyName,
        exclusion_type: exclusionType,
        siren: siren || undefined,
        reason: reason || undefined,
        starts_at: startsAt ? `${startsAt}T00:00:00Z` : undefined,
        expires_at: exclusionType === 'recent_prospect' && expiresAt ? `${expiresAt}T00:00:00Z` : undefined,
      })
      setCompanyName(''); setSiren(''); setReason(''); setStartsAt(today); setExpiresAt('')
      setNotice('Exclusion ajoutée.'); await load()
    } catch (requestError) {
      setFormError(requestError instanceof Error ? requestError.message : 'Impossible d’ajouter cette exclusion.')
    } finally { setSaving(false) }
  }

  async function remove(item: CommercialExclusion) {
    if (!window.confirm(`Supprimer l’exclusion de ${item.company_name_snapshot} ?`)) return
    setError(null)
    try { await deleteCommercialExclusion(item.id); setNotice('Exclusion supprimée.'); await load() }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de supprimer cette exclusion.') }
  }

  async function selectCsv(file: File | undefined) {
    setPreview(null); setImportReport(null); setImportError(null); setCsvContent(''); setCsvName(file?.name ?? '')
    if (!file) return
    try { setCsvContent(await file.text()) }
    catch { setImportError('Impossible de lire ce fichier CSV.') }
  }

  async function previewCsv() {
    setImporting(true); setImportError(null); setImportReport(null)
    try { setPreview(await previewCommercialExclusionCsv(csvContent)) }
    catch (requestError) { setPreview(null); setImportError(requestError instanceof Error ? requestError.message : 'Le CSV est invalide.') }
    finally { setImporting(false) }
  }

  async function confirmImport() {
    setImporting(true); setImportError(null)
    try {
      const report = await importCommercialExclusionCsv(csvContent)
      setImportReport(report); setPreview(report); await load()
    } catch (requestError) { setImportError(requestError instanceof Error ? requestError.message : 'Impossible d’importer ce CSV.') }
    finally { setImporting(false) }
  }

  return <section className="exclusions-page" aria-labelledby="exclusions-title">
    {!embedded && <div className="section-heading"><div><p className="eyebrow">RÈGLES COMMERCIALES</p><h2 id="exclusions-title">Exclusions</h2><p className="section-intro">Les entreprises actives dans cette liste ne sont pas sélectionnées par une nouvelle recherche.</p></div></div>}

    {notice && <p className="success-notice" role="status">{notice}</p>}
    <div className="exclusion-layout">
      <section className="form-card import-card" aria-labelledby="import-title"><div><p className="eyebrow">IMPORT CSV</p><h3 id="import-title">Importer une liste</h3></div><p>Colonnes : <code>company_name</code>, <code>exclusion_type</code>, puis éventuellement <code>siren</code>, <code>reason</code>, <code>starts_at</code>, <code>expires_at</code>. Les en-têtes français équivalents sont acceptés.</p>
        <label className="file-field">Fichier CSV<input type="file" accept=".csv,text/csv" onChange={(event) => void selectCsv(event.target.files?.[0])} /></label>
        {csvName && <p className="selected-file">Fichier sélectionné : {csvName}</p>}
        <button type="button" className="secondary-button" disabled={!csvContent || importing} onClick={() => void previewCsv()}>{importing && !preview ? 'Analyse…' : 'Analyser le fichier'}</button>
        {importError && <p className="inline-error" role="alert">{importError}</p>}
        {preview && <div className="import-summary"><strong>{preview.valid_count} prête{preview.valid_count > 1 ? 's' : ''} à importer</strong><span>{preview.duplicate_count} doublon{preview.duplicate_count > 1 ? 's' : ''} · {preview.invalid_count} invalide{preview.invalid_count > 1 ? 's' : ''}</span></div>}
        {preview && <div className="preview-table-wrap"><table className="preview-table"><thead><tr><th>Ligne</th><th>Entreprise</th><th>Type</th><th>Résultat</th></tr></thead><tbody>{preview.rows.map((row) => <tr key={row.line_number} className={!row.valid ? 'invalid' : row.duplicate ? 'duplicate' : ''}><td>{row.line_number}</td><td>{row.company_name ?? '—'}</td><td>{typeLabels[row.exclusion_type ?? ''] ?? 'Type invalide'}</td><td>{row.errors.length ? row.errors.join(' ') : row.duplicate ? 'Doublon — ignoré' : 'Valide'}</td></tr>)}</tbody></table></div>}
        {preview && preview.valid_count > 0 && !importReport && <button type="button" className="primary-button" disabled={importing} onClick={() => void confirmImport()}>{importing ? 'Import…' : `Confirmer l’import de ${preview.valid_count} ligne${preview.valid_count > 1 ? 's' : ''}`}</button>}
        {importReport && <p className="success-notice" role="status">{importReport.added_count} ajoutée{importReport.added_count > 1 ? 's' : ''}, {importReport.ignored_count} ignorée{importReport.ignored_count > 1 ? 's' : ''}.</p>}
      </section>
      <details className="external-add"><summary>Ajouter une entreprise externe</summary><form className="form-card exclusion-form" onSubmit={(event) => void submit(event)}>
        <div><p className="eyebrow">ACTION SECONDAIRE</p><h3>Ajouter une exclusion externe</h3><p className="external-warning">Cette identité n’est pas reliée à une entreprise vérifiée. Préférez l’action depuis une carte lead ou l’import CSV.</p></div>
        <label>Entreprise<input value={companyName} onChange={(event) => setCompanyName(event.target.value)} required minLength={3} maxLength={500} /></label>
        <label>Type d’exclusion<select value={exclusionType} onChange={(event) => { setExclusionType(event.target.value as ExclusionType); if (event.target.value !== 'recent_prospect') setExpiresAt('') }}><option value="current_client">Client actuel</option><option value="recent_prospect">Prospect récent historique</option><option value="manual_exclusion">Exclusion manuelle</option></select></label>
        <label>SIREN <span>(facultatif)</span><input value={siren} onChange={(event) => setSiren(event.target.value.replace(/\D/g, '').slice(0, 9))} inputMode="numeric" pattern="\d{9}" placeholder="9 chiffres" /></label>
        {!siren && companyName && <p className="matching-warning">Sans SIREN, le rapprochement reposera uniquement sur le nom normalisé exact. Aucun SIREN ne sera inventé.</p>}
        <label>Raison <span>(facultatif)</span><textarea value={reason} onChange={(event) => setReason(event.target.value)} rows={3} /></label>
        <div className="date-fields"><label>Date de début<input type="date" value={startsAt} onChange={(event) => setStartsAt(event.target.value)} required /></label>{exclusionType === 'recent_prospect' && <label>Date d’expiration<input type="date" value={expiresAt} min={startsAt} onChange={(event) => setExpiresAt(event.target.value)} /></label>}</div>
        {formError && <p className="inline-error" role="alert">{formError}</p>}
        <button className="primary-button" type="submit" disabled={saving}>{saving ? 'Ajout…' : 'Ajouter l’exclusion'}</button>
      </form></details>
    </div>

    <section className="exclusion-list-section"><div className="section-heading"><div><p className="eyebrow">LISTE</p><h2>Exclusions enregistrées</h2></div><button type="button" className="refresh-button" onClick={() => void load()} disabled={loading}>Recharger</button></div>
      <div className="exclusion-filters"><label>Rechercher<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Nom de l’entreprise" /></label><label>Type<select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="">Tous</option><option value="current_client">Client actuel</option><option value="recent_prospect">Prospect récent</option><option value="manual_exclusion">Exclusion manuelle</option></select></label><label>Statut<select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">Tous</option><option value="active">Actif</option><option value="expired">Expiré</option></select></label></div>
      {loading && items.length === 0 && <div className="state-card" role="status">Chargement des exclusions…</div>}
      {error && <div className="state-card error-state" role="alert"><p>{error}</p><button type="button" onClick={() => void load()}>Réessayer</button></div>}
      {!loading && !error && items.length === 0 && <div className="state-card"><h3>Aucune exclusion</h3><p>Aucune ligne ne correspond aux filtres actuels.</p></div>}
      {!error && items.length > 0 && <div className="exclusion-list">{items.map((item) => <article className="exclusion-card" key={item.id}><div className="exclusion-card-main"><div><span className={`status-badge ${item.active ? 'active' : 'expired'}`}>{item.active ? 'Actif' : 'Expiré'}</span><h3>{item.company_name_snapshot}</h3><p>{typeLabels[item.exclusion_type]}</p></div><button type="button" className="danger-button" onClick={() => void remove(item)}>Supprimer</button></div><dl><div><dt>SIREN</dt><dd>{item.siren ?? 'Non renseigné'}</dd></div><div><dt>Raison</dt><dd>{item.reason ?? 'Non renseignée'}</dd></div><div><dt>Début</dt><dd>{formatDate(item.starts_at)}</dd></div><div><dt>Expiration</dt><dd>{formatDate(item.expires_at)}</dd></div></dl>{item.matching_basis === 'company_key' && <p className="matching-warning">Rapprochement par nom normalisé exact uniquement.</p>}</article>)}</div>}
    </section>
  </section>
}
