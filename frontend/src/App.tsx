import { useEffect, useState } from 'react'

type Summary = {
  app_name: string
  territory: string
  external_connectors_enabled: number
  contact_automation_enabled: boolean
}

export default function App() {
  const [summary, setSummary] = useState<Summary | null>(null)
  const [apiOnline, setApiOnline] = useState(false)

  useEffect(() => {
    fetch('http://127.0.0.1:8000/api/v1/summary')
      .then((response) => response.json())
      .then((data: Summary) => { setSummary(data); setApiOnline(true) })
      .catch(() => setApiOnline(false))
  }, [])

  return (
    <main>
      <header>
        <p className="eyebrow">OUTIL LOCAL · QUALIFICATION DE LEADS</p>
        <h1>Vos prochains recrutements commencent ici.</h1>
        <p className="intro">Un espace local pour identifier et qualifier des opportunités commerciales dans le Val-de-Marne.</p>
      </header>
      <section className="status-card">
        <div><span className={apiOnline ? 'dot online' : 'dot'} /> API locale {apiOnline ? 'connectée' : 'à démarrer'}</div>
        <strong>{summary?.territory ?? 'Val-de-Marne (94)'}</strong>
      </section>
      <section className="grid">
        <article><span>01</span><h2>Rechercher</h2><p>Les sources seront activées progressivement, en commençant par les données autorisées et traçables.</p></article>
        <article><span>02</span><h2>Qualifier</h2><p>Chaque opportunité réunira des faits, leurs preuves, et une priorité clairement expliquée.</p></article>
        <article><span>03</span><h2>Prospecter</h2><p>Le système recommandera un canal. Aucun message ou contact ne sera envoyé automatiquement.</p></article>
      </section>
      <section className="coming-soon"><h2>Socle prêt</h2><p>La recherche de leads, les connecteurs et les exports seront ajoutés dans les prochaines étapes.</p></section>
    </main>
  )
}
