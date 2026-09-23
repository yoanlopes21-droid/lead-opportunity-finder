import { useCallback, useEffect, useState } from 'react'
import { fetchSearchRun } from './api'
import { Dashboard } from './components/Dashboard'
import { NewSearch } from './components/NewSearch'
import { Prospection } from './components/Prospection'
import { SearchProgress } from './components/SearchProgress'
import { SearchResults } from './components/SearchResults'
import { Sources } from './components/Sources'
import { RecruitmentSignals } from './components/RecruitmentSignals'
import type { SearchRun } from './types'

type View = 'dashboard' | 'new-search' | 'prospection' | 'sources' | 'signals' | 'progress' | 'results'
const RUN_STORAGE_KEY = 'lead-opportunity-finder.active-search-run-id'

function storedRunId() {
  const value = window.localStorage.getItem(RUN_STORAGE_KEY)
  const id = value ? Number(value) : NaN
  return Number.isInteger(id) && id > 0 ? id : null
}

export default function App() {
  const initialRunId = storedRunId()
  const [view, setView] = useState<View>(initialRunId ? 'progress' : 'dashboard')
  const [runId, setRunId] = useState<number | null>(initialRunId)
  const [run, setRun] = useState<SearchRun | null>(null)

  const rememberRun = useCallback((next: SearchRun) => {
    setRun(next); setRunId(next.id); window.localStorage.setItem(RUN_STORAGE_KEY, String(next.id))
  }, [])

  useEffect(() => {
    if (!runId) return
    fetchSearchRun(runId).then(rememberRun).catch(() => {
      window.localStorage.removeItem(RUN_STORAGE_KEY); setRunId(null); setRun(null); setView('dashboard')
    })
  }, [runId, rememberRun])

  function showNewSearch() { setView('new-search') }
  function openRun() { if (runId) setView('progress'); else setView('new-search') }

  return (
    <main className="dashboard">
      <header className="page-header"><div><p className="eyebrow">OUTIL LOCAL · OPPORTUNITÉS COMMERCIALES</p><h1>Lead Opportunity Finder</h1><p className="intro">Priorisez les opportunités commerciales locales à partir de besoins de recrutement observés.</p></div>
        <button type="button" className="new-search-button" onClick={showNewSearch}>Nouvelle recherche</button>
      </header>
      <nav className="app-navigation" aria-label="Navigation principale">
        <button type="button" className={view === 'dashboard' ? 'active' : ''} onClick={() => setView('dashboard')}>Dashboard</button>
        <button type="button" className={view === 'new-search' ? 'active' : ''} onClick={showNewSearch}>Nouvelle recherche</button>
        <button type="button" className={view === 'prospection' ? 'active' : ''} onClick={() => setView('prospection')}>Prospection</button>
        <button type="button" className={view === 'sources' ? 'active' : ''} onClick={() => setView('sources')}>Sources</button>
        <button type="button" className={view === 'signals' ? 'active' : ''} onClick={() => setView('signals')}>Signaux</button>
        {runId && <button type="button" className={view === 'progress' || view === 'results' ? 'active' : ''} onClick={openRun}>Recherche nº {runId}</button>}
      </nav>
      {view === 'dashboard' && <Dashboard onSources={() => setView('sources')} onSignals={() => setView('signals')} />}
      {view === 'new-search' && <NewSearch activeRun={run} isRestoringRun={Boolean(runId && !run)} onCreated={(next) => { rememberRun(next); setView('progress') }} onOpenRun={openRun} />}
      {view === 'prospection' && <Prospection />}
      {view === 'sources' && <Sources onSignals={() => setView('signals')} />}
      {view === 'signals' && <RecruitmentSignals />}
      {view === 'progress' && runId && <SearchProgress runId={runId} initialRun={run} onRunChange={rememberRun} onResults={() => setView('results')} />}
      {view === 'results' && runId && <SearchResults runId={runId} run={run} onProgress={() => setView('progress')} />}
    </main>
  )
}
