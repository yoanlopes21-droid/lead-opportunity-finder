import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import React from 'react'
import renderer, { act } from 'react-test-renderer'
import { build } from 'esbuild'

const output = resolve('node_modules/.cache/interaction-panel-test.mjs')
await mkdir(resolve('node_modules/.cache'), { recursive: true })
await build({ entryPoints: [resolve('src/components/InteractionPanel.tsx')], outfile: output,
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime'] })
const { InteractionPanel } = await import(pathToFileURL(output).href)

const event = (outcome = 'wrong_contact') => ({ id: 1, company_key: 'acme sas', siren: null,
  happened_at: new Date().toISOString(), channel: 'phone', outcome, resulting_status: 'wrong_contact',
  offer_code: 'starter', job_title: 'Technicien', contacted_person: null, note: null,
  next_action: null, next_action_at: null, priority_expressed: null, created_at: new Date().toISOString() })
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const button = (root, label) => root.findAllByType('button').find((node) => node.children.includes(label))
const texts = (node) => JSON.stringify(node.toJSON())

async function mount({ enabled = true, history = [] } = {}) {
  const requests = []
  let resolvePost
  globalThis.fetch = (url, init) => {
    requests.push({ url, init })
    if (init?.method === 'POST') return new Promise((resolve) => { resolvePost = resolve })
    return Promise.resolve(response({ items: history }))
  }
  const saved = []
  let tree
  await act(async () => { tree = renderer.create(React.createElement(InteractionPanel,
    { companyKey: 'acme sas', offerCode: 'starter', enabled, onSaved: (item) => saved.push(item) })) })
  return { tree, requests, saved, finish: (value, status = 201) => resolvePost(response(value, status)) }
}

test('history loads lazily and opening does not create a contact', async () => {
  const view = await mount({ history: [event()] })
  assert.match(texts(view.tree), /Mauvais interlocuteur/)
  assert.equal(view.requests.length, 1)
  await act(async () => { button(view.tree.root, 'Enregistrer le résultat').props.onClick() })
  assert.match(texts(view.tree), /Canal utilisé/)
  assert.equal(view.requests.length, 1)
  view.tree.unmount()
})

test('explicit submit shows proposed status, loading and success without reload', async () => {
  const view = await mount()
  await act(async () => { button(view.tree.root, 'Enregistrer le résultat').props.onClick() })
  const selects = view.tree.root.findAllByType('select')
  await act(async () => { selects[0].props.onChange({ target: { value: 'phone' } }); selects[1].props.onChange({ target: { value: 'wrong_contact' } }) })
  assert.match(texts(view.tree), /Statut appliqué/)
  assert.match(texts(view.tree), /Mauvais interlocuteur/)
  await act(async () => { view.tree.root.findByType('form').props.onSubmit({ preventDefault() {} }) })
  assert.equal(view.requests.filter((item) => item.init?.method === 'POST').length, 1)
  assert.match(texts(view.tree), /Enregistrement…/)
  assert.equal(button(view.tree.root, 'Enregistrement…').props.disabled, true)
  const sent = JSON.parse(view.requests[1].init.body)
  assert.equal(sent.channel, 'phone')
  assert.equal(sent.outcome, 'wrong_contact')
  assert.equal(sent.offer_code, 'starter')
  await act(async () => { view.finish({ interaction: event(), relationship: { status: 'wrong_contact' } }); await Promise.resolve() })
  assert.match(texts(view.tree), /Résultat enregistré/)
  assert.equal(view.saved.length, 1)
  assert.equal(view.requests.filter((item) => item.init?.method === 'POST').length, 1)
  view.tree.unmount()
})

test('server error keeps the form open and disabled prospecting prevents opening', async () => {
  const view = await mount()
  await act(async () => { button(view.tree.root, 'Enregistrer le résultat').props.onClick() })
  const selects = view.tree.root.findAllByType('select')
  await act(async () => { selects[0].props.onChange({ target: { value: 'phone' } }); selects[1].props.onChange({ target: { value: 'wrong_contact' } }) })
  await act(async () => { view.tree.root.findByType('form').props.onSubmit({ preventDefault() {} }) })
  await act(async () => { view.finish({ detail: 'Erreur simulée' }, 422); await Promise.resolve() })
  assert.match(texts(view.tree), /Erreur simulée/)
  assert.ok(view.tree.root.findByType('form'))
  assert.equal(view.saved.length, 0)
  view.tree.unmount()

  const blocked = await mount({ enabled: false })
  assert.equal(button(blocked.tree.root, 'Enregistrer le résultat').props.disabled, true)
  assert.equal(blocked.tree.root.findAllByType('form').length, 0)
  blocked.tree.unmount()
})

test('callback keeps the user supplied next action and date', async () => {
  const view = await mount()
  await act(async () => { button(view.tree.root, 'Enregistrer le résultat').props.onClick() })
  const selects = view.tree.root.findAllByType('select')
  await act(async () => { selects[0].props.onChange({ target: { value: 'phone' } }); selects[1].props.onChange({ target: { value: 'callback_requested' } }); selects[2].props.onChange({ target: { value: 'Rappeler' } }) })
  assert.match(texts(view.tree), /Ajoutez une date de rappel/)
  const dateInputs = view.tree.root.findAllByType('input').filter((node) => node.props.type === 'datetime-local')
  await act(async () => { dateInputs[1].props.onChange({ target: { value: '2026-10-01T14:00' } }) })
  await act(async () => { view.tree.root.findByType('form').props.onSubmit({ preventDefault() {} }) })
  const sent = JSON.parse(view.requests[1].init.body)
  assert.equal(sent.next_action, 'Rappeler')
  assert.ok(sent.next_action_at.startsWith('2026-10-01T'))
  view.tree.unmount()
})
