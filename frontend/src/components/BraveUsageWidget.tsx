import { useEffect, useState } from 'react'
import { fetchBraveUsage } from '../api'
import type { BraveUsage } from '../types'

const statusLabel: Record<string, string> = { healthy: 'Sain', elevated: 'À surveiller', low: 'Faible', exhausted: 'Épuisé' }
const money = new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })

export function BraveUsageWidget() {
  const [usage, setUsage] = useState<BraveUsage | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetchBraveUsage().then(setUsage).catch(() => setError(true))
  }, [])

  return <aside className="brave-widget" aria-label="Estimation locale Brave Search">
    <div><p className="widget-kicker">ESTIMATION LOCALE</p><h2>Brave Search</h2></div>
    {usage && <div className="brave-metrics">
      <strong>{usage.monthly_used} <span>/ {usage.monthly_budget} recherches</span></strong>
      <p>{usage.monthly_remaining} restantes · {money.format(usage.estimated_cost_used_usd)} utilisés</p>
      <p>Crédit estimé restant : {money.format(usage.estimated_credit_remaining_usd)}</p>
      <small>Statut : {statusLabel[usage.status] ?? usage.status} · reset estimé le {new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'short' }).format(new Date(usage.current_period_end))}</small>
    </div>}
    {error && <p className="widget-error">Estimation Brave indisponible. La liste de leads reste accessible.</p>}
    {!usage && !error && <p className="widget-loading">Chargement de l’estimation…</p>}
  </aside>
}
