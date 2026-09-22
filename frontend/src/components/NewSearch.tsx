import { useState } from 'react'
import { createSearchRun } from '../api'
import type { SearchRun } from '../types'

type NewSearchProps = { activeRun: SearchRun | null; isRestoringRun: boolean; onCreated: (run: SearchRun) => void; onOpenRun: () => void }

const activeStatuses = new Set(['queued', 'running', 'stopping'])

export function NewSearch({ activeRun, isRestoringRun, onCreated, onOpenRun }: NewSearchProps) {
  const [goal, setGoal] = useState(20)
  const [braveCap, setBraveCap] = useState(10)
  const [isCreating, setIsCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const hasActiveRun = Boolean(activeRun && activeStatuses.has(activeRun.status))

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (isCreating || hasActiveRun) return
    if (goal < 10 || goal > 30 || braveCap < 0 || braveCap > 40) {
      setError('Vérifiez l’objectif et le budget Brave avant de lancer la recherche.')
      return
    }
    setIsCreating(true); setError(null)
    try { onCreated(await createSearchRun({ department: '94', requested_actionable_leads: goal, brave_hard_cap: braveCap })) }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : 'Impossible de créer la recherche.') }
    finally { setIsCreating(false) }
  }

  if (isRestoringRun) return <div className="state-card" role="status">Vérification de la recherche mémorisée…</div>

  if (hasActiveRun && activeRun) return <section className="form-card active-run-card">
    <p className="eyebrow">RECHERCHE ACTIVE</p><h2>Une recherche est déjà en cours</h2>
    <p>{activeRun.current_actionable_leads} lead{activeRun.current_actionable_leads > 1 ? 's' : ''} exploitable{activeRun.current_actionable_leads > 1 ? 's' : ''} trouvé{activeRun.current_actionable_leads > 1 ? 's' : ''} sur {activeRun.requested_actionable_leads} demandé{activeRun.requested_actionable_leads > 1 ? 's' : ''}.</p>
    <button type="button" className="primary-button" onClick={onOpenRun}>Revenir à la recherche</button>
  </section>

  return <section className="form-card" aria-labelledby="new-search-title">
    <p className="eyebrow">RECHERCHE COMMERCIALE</p><h2 id="new-search-title">Nouvelle recherche</h2>
    <p className="form-intro">Définissez un objectif : le moteur s’arrêtera automatiquement dès qu’il aura trouvé assez de leads exploitables.</p>
    <form onSubmit={(event) => void submit(event)}>
      <label>Département <input value="94" disabled aria-describedby="department-help" /><small id="department-help">Val-de-Marne uniquement dans cette version.</small></label>
      <label>Objectif de leads exploitables <select value={goal} onChange={(event) => setGoal(Number(event.target.value))}>{[10, 15, 20, 25, 30].map((value) => <option key={value} value={value}>{value} leads</option>)}</select></label>
      <label>Budget Brave maximum <select value={braveCap} onChange={(event) => setBraveCap(Number(event.target.value))}>{[0, 5, 10, 20, 30, 40].map((value) => <option key={value} value={value}>{value} requêtes</option>)}</select><small>Il s’agit d’un plafond par run, pas d’un volume forcément consommé.</small></label>
      <aside className="search-explanation"><p>Les contacts et données en cache sont réutilisés en priorité.</p><p>Brave n’est appelé que si un enrichissement est nécessaire.</p><p>La recherche s’arrête dès que l’objectif est atteint.</p></aside>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <button type="submit" className="primary-button" disabled={isCreating}>{isCreating ? 'Lancement…' : 'Lancer la recherche'}</button>
    </form>
  </section>
}
