import { useEffect, useState } from 'react'
import { getJson } from './api'
import './App.css'

type Health = 'checking' | 'ok' | 'down'

function useHealth(path: string): Health {
  const [state, setState] = useState<Health>('checking')
  useEffect(() => {
    getJson<{ status: string }>(path)
      .then((r) => setState(r.status === 'ok' ? 'ok' : 'down'))
      .catch(() => setState('down'))
  }, [path])
  return state
}

function Row({ label, state }: { label: string; state: Health }) {
  const dot = state === 'ok' ? '🟢' : state === 'down' ? '🔴' : '⚪️'
  return (
    <li style={{ listStyle: 'none', margin: '0.25rem 0' }}>
      {dot} {label} — <code>{state}</code>
    </li>
  )
}

function App() {
  const api = useHealth('/health')
  const graph = useHealth('/health/graph')

  return (
    <main style={{ maxWidth: 640, margin: '4rem auto', padding: '0 1rem', fontFamily: 'system-ui' }}>
      <h1>Nebula</h1>
      <p>Agentic research graph — skeleton. Backend wiring check:</p>
      <ul style={{ padding: 0 }}>
        <Row label="API (/health)" state={api} />
        <Row label="Neo4j (/health/graph)" state={graph} />
      </ul>
      <p style={{ color: '#888', fontSize: '0.9rem' }}>
        Graph shows red until <code>make db-up</code> starts Neo4j. See CLAUDE.md for the build sequence.
      </p>
    </main>
  )
}

export default App
