import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
import { resolve } from 'node:path'
import React from 'react'
import renderer, { act } from 'react-test-renderer'
import { build } from 'esbuild'

const output = resolve('node_modules/.cache/approach-workspace-test.mjs')
await mkdir(resolve('node_modules/.cache'), { recursive: true })
await build({ entryPoints: [resolve('src/components/ApproachWorkspace.tsx')], outfile: output,
  bundle: true, platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime'] })
const { ApproachWorkspace } = await import(pathToFileURL(output).href)
globalThis.document = {
  activeElement: null, body: { style: { overflow: '' } },
  addEventListener() {}, removeEventListener() {},
}

const pack = {
  status: 'ready_for_call', communication_status: 'communicable', company: 'ACME', communication_title: 'Technicien',
  entry_offer: { offer_id: 'offer-1', source: 'france_travail', title: 'Technicien', location: 'Créteil', description_excerpt: null, source_urls: ['https://source.test/1'], selection_reasons: [], warnings: [] },
  commercial_angle: { readiness: 'ready_to_contact', pack_status: 'ready_for_call', active_angle: true, contact_rationale: 'need', entry_offer_reasons: [], specialty_match: 'none', specialty_label: null, specialty_reason: '', territorial_relevance: 'none', territorial_reason: '', primary_angle: 'targeted_search', primary_value_proposition: 'targeted_search', target_role: 'RH', channel_strategy: '', internal_advice: [], reasons: [], selected_offer_code: 'starter' },
  phone: [{ type: 'phone_decision_maker', target: 'RH', opening: 'Bonjour', first_30_seconds: 'Votre besoin ?', continuation: 'Suite', qualification_questions: [], meeting_transition: 'RDV', meeting_transition_after_response: null, ready_to_copy: true }],
  email: [{ type: 'cold_email', target: 'RH', subject: 'Objet', body: 'Corps initial', attachment_recommendation: 'none', ready_to_copy: true, required_event: null }],
  priority_objections: [], objections: [], evidence: { claims_used: [], sources: [], warnings: [], do_not_claim: [] },
  internal: { advice: [], commercial_levers: [], selected_offer: 'starter', unresolved_decisions: [], attachment_if_presentation_requested: '', attachment_if_offer_details_requested: '' },
  recommended_contact: { id: 9, type: 'phone', value: '01 23 45 67 89', scope: 'company', local_key: null, reach: 'general', person_contact_id: null, role: 'general_routing', use: 'usable', confidence: 'confirmed', verification_status: 'manually_verified', commercial_relevance: 'relevant', reason_codes: [], source_urls: [] },
  recommended_channel: 'company_switchboard',
}
const lead = { company_key: 'acme', company_name: 'ACME', department: '94', contacts: [{ id: 2, value: 'rejected@example.test' }], contact_strategy: { preferred_contact_point_id: 2, preferred_channel: 'direct_email', fallback_channels: [] } }
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const text = (tree) => JSON.stringify(tree.toJSON())
const button = (root, label) => root.findAllByType('button').find((item) => item.children.includes(label))

function mockFetch() {
  globalThis.fetch = (url, init) => {
    if (String(url).includes('/approach-pack')) return Promise.resolve(response(pack))
    if (String(url).includes('/commercial-configuration/offers')) return Promise.resolve(response([{ code: 'starter', display_name: 'Starter', enabled_for_prospecting: true, is_default: true }]))
    if (String(url).includes('/commercial-interactions') && init?.method === 'POST') return Promise.resolve(response({ interaction: { id: 1, company_key: 'acme', siren: null, happened_at: '2026-09-28T10:00:00Z', channel: 'phone', outcome: 'conversation', resulting_status: 'contacted', need_id: 'france_travail:offer-1', need_source: 'france_travail', need_location: 'Créteil', need_source_url: null, need_status: null, offer_code: 'starter', job_title: 'Technicien', contacted_person: null, note: null, next_action: null, next_action_at: null, priority_expressed: null, next_action_mode: 'preserve', applied_to_current_state: true, created_at: '2026-09-28T10:00:00Z' }, relationship: { status: 'contacted' } }, 201))
    if (String(url).includes('/commercial-interactions')) return Promise.resolve(response({ items: [] }))
    throw new Error(`Unexpected request: ${url}`)
  }
}

test('result action remains available in Call and Write and uses the approach-selected contact', async () => {
  mockFetch()
  let tree
  await act(async () => { tree = renderer.create(React.createElement(ApproachWorkspace, { lead, onClose() {} })); await Promise.resolve() })
  assert.match(text(tree), /01 23 45 67 89/)
  assert.doesNotMatch(text(tree), /rejected@example\.test/)
  await act(async () => { button(tree.root, 'Appeler').props.onClick() })
  assert.ok(button(tree.root, 'Enregistrer le résultat'))
  await act(async () => { button(tree.root, 'Écrire').props.onClick() })
  assert.ok(button(tree.root, 'Enregistrer le résultat'))
  tree.unmount()
})

test('edited email survives an interaction-triggered pack refresh', async () => {
  mockFetch()
  let tree
  await act(async () => { tree = renderer.create(React.createElement(ApproachWorkspace, { lead, onClose() {} })); await Promise.resolve() })
  await act(async () => { button(tree.root, 'Écrire').props.onClick() })
  const emailBody = tree.root.findAllByType('textarea').find((item) => item.props.rows === 14)
  await act(async () => { emailBody.props.onChange({ target: { value: 'Corps édité par utilisateur' } }) })
  await act(async () => { button(tree.root, 'Enregistrer le résultat').props.onClick() })
  let selects = tree.root.findAllByType('select')
  const channel = selects.find((item) => item.props.value === '')
  await act(async () => { channel.props.onChange({ target: { value: 'phone' } }) })
  selects = tree.root.findAllByType('select')
  const outcome = selects.find((item) => item.props.required && item !== channel && item.props.value === '')
  await act(async () => { outcome.props.onChange({ target: { value: 'conversation' } }) })
  await act(async () => { tree.root.findByType('form').props.onSubmit({ preventDefault() {} }); await Promise.resolve(); await Promise.resolve() })
  assert.equal(tree.root.findAllByType('textarea').find((item) => item.props.rows === 14).props.value, 'Corps édité par utilisateur')
  tree.unmount()
})

test('historical dossier stays usable without generating new client copy', async () => {
  mockFetch()
  const dossier = { relationship: { id: 7, company_key: 'acme', company_name_snapshot: 'ACME', status: 'contacted', next_action: null, next_action_at: null }, active_lead: null, needs: [{ need_id: 'france_travail:old', title: 'Ancien poste', location: 'Créteil', source: 'france_travail', source_url: 'https://source.test/old', active: false, first_seen_at: null, last_seen_at: '2026-09-20T10:00:00Z', newly_observed: false }], selected_need_id: 'france_travail:old', can_prepare_new_outreach: false, can_record_interaction: true, communication_status: 'historical_only' }
  let tree
  await act(async () => { tree = renderer.create(React.createElement(ApproachWorkspace, { dossier, onClose() {} })); await Promise.resolve() })
  assert.match(text(tree), /Besoin historique/)
  assert.ok(button(tree.root, 'Enregistrer le résultat'))
  await act(async () => { button(tree.root, 'Appeler').props.onClick() })
  assert.match(text(tree), /aucun nouveau contenu client n’est généré/)
  tree.unmount()
})
