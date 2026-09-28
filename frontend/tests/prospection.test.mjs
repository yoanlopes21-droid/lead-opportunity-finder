import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import React from 'react'
import renderer, { act } from 'react-test-renderer'
import { build } from 'esbuild'

const output = resolve('node_modules/.cache/prospection-test.mjs')
await mkdir(resolve('node_modules/.cache'), { recursive: true })
await build({ entryPoints: [resolve('src/components/Prospection.tsx')], outfile: output,
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime'] })
const { Prospection } = await import(pathToFileURL(output).href)
globalThis.document = { activeElement: null, body: { style: { overflow: '' } }, addEventListener() {}, removeEventListener() {} }

const relationship = { id: 7, company_key: 'acme', siren: null, company_name_snapshot: 'ACME', status: 'follow_up', last_contact_at: '2026-09-28T10:00:00Z', next_action_at: null, next_action: 'Rappeler', note: null, outcome: null, contact_point_id: null, person_contact_id: null, used_channel: 'phone', hard_exclusion_id: null, is_active: true, created_at: '2026-09-27T10:00:00Z', updated_at: '2026-09-28T10:00:00Z', follow_up_timing: null }
const dossier = { relationship, active_lead: null, needs: [{ need_id: 'france_travail:old', title: 'Technicien', location: 'Créteil', source: 'france_travail', source_url: null, active: false, first_seen_at: null, last_seen_at: '2026-09-28T10:00:00Z', newly_observed: false }], selected_need_id: 'france_travail:old', can_prepare_new_outreach: false, can_record_interaction: true, communication_status: 'historical_only' }
const response = (body) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
const button = (root, label) => root.findAllByType('button').find((item) => item.children.includes(label))
const text = (tree) => JSON.stringify(tree.toJSON())

function mockFetch(requests) {
  globalThis.fetch = (url) => {
    requests.push(String(url))
    if (String(url).includes('/dossier')) return Promise.resolve(response(dossier))
    if (String(url).includes('/commercial-configuration/offers')) return Promise.resolve(response([]))
    if (String(url).includes('/commercial-interactions/')) return Promise.resolve(response({ items: [] }))
    if (String(url).includes('/commercial-relationships')) return Promise.resolve(response({ items: [relationship], total: 1 }))
    throw new Error(`Unexpected request: ${url}`)
  }
}

test('Prospection and Follow-up both open the complete existing dossier', async () => {
  const requests = []; mockFetch(requests)
  let tree
  await act(async () => { tree = renderer.create(React.createElement(Prospection)); await Promise.resolve() })
  await act(async () => { button(tree.root, 'Ouvrir le dossier').props.onClick(); await Promise.resolve(); await Promise.resolve() })
  assert.match(text(tree), /ESPACE COMMERCIAL/)
  assert.match(text(tree), /Besoin historique/)
  const close = tree.root.findAllByType('button').find((item) => item.props['aria-label'] === 'Fermer le panneau')
  await act(async () => { close.props.onClick(); await Promise.resolve() })
  await act(async () => { button(tree.root, 'À relancer').props.onClick(); await Promise.resolve() })
  await act(async () => { button(tree.root, 'Ouvrir le dossier').props.onClick(); await Promise.resolve(); await Promise.resolve() })
  assert.match(text(tree), /Enregistrer le résultat/)
  assert.ok(requests.some((url) => url.includes('view=follow_up')))
  tree.unmount()
})

test('planning view identifies active dossiers without a date', async () => {
  const requests = []; mockFetch(requests)
  let tree
  await act(async () => { tree = renderer.create(React.createElement(Prospection)); await Promise.resolve() })
  await act(async () => { button(tree.root, 'À planifier').props.onClick(); await Promise.resolve() })
  assert.match(text(tree), /Suite à planifier/)
  assert.ok(requests.some((url) => url.includes('view=planning')))
  tree.unmount()
})
