import test from 'node:test'
import assert from 'node:assert/strict'
import { canCopyEmail, canCopyObjection, canCopyPhone, copyReason, hasEditedEmail, initialEmailEdits, scriptText } from '../src/approachRules.ts'

const phone = { opening: 'Bonjour.', first_30_seconds: 'Pourquoi cet appel ?', continuation: 'Je vous écoute.', qualification_questions: ['Quel besoin ?'], meeting_transition: 'Prenons rendez-vous.', ready_to_copy: true }
const email = { type: 'routing_email', subject: 'Objet', body: 'Bonjour,', ready_to_copy: true }
const pack = (communication_status) => ({ communication_status, email: [email] })

test('ready and routing drafts can be copied', () => {
  assert.equal(canCopyPhone(pack('communicable'), phone), true)
  assert.equal(canCopyEmail(pack('communicable'), email, { subject: 'Objet', body: 'Bonjour,' }), true)
  assert.equal(canCopyObjection(pack('communicable')), true)
})
test('channel missing keeps drafts but disables client copy', () => {
  assert.equal(canCopyPhone(pack('prepared_no_channel'), phone), false)
  assert.equal(canCopyEmail(pack('prepared_no_channel'), email, { subject: 'Objet', body: 'Bonjour,' }), false)
  assert.match(copyReason(pack('prepared_no_channel'), false), /aucun canal/)
})
test('verify contact and blocked readiness disable all copy', () => {
  for (const status of ['verify_contact', 'blocked']) {
    assert.equal(canCopyPhone(pack(status), phone), false)
    assert.equal(canCopyEmail(pack(status), email, { subject: 'Objet', body: 'Bonjour,' }), false)
    assert.equal(canCopyObjection(pack(status)), false)
  }
  assert.match(copyReason(pack('blocked'), false), /bloquée/)
  assert.match(copyReason(pack('blocked'), false, 'confirmed_email_request_in_call'), /bloquée/)
})
test('required call event and wrong channel remain unavailable', () => {
  assert.match(copyReason(pack('communicable'), false, 'confirmed_email_request_in_call'), /demande réelle/)
  assert.equal(canCopyEmail(pack('communicable'), { ...email, ready_to_copy: false }, { subject: 'Objet', body: 'Bonjour,' }), false)
  assert.equal(canCopyPhone(pack('communicable'), { ...phone, ready_to_copy: false }), false)
})
test('empty edited fields cannot be copied', () => {
  assert.equal(canCopyEmail(pack('communicable'), email, { subject: ' ', body: 'Bonjour,' }), false)
  assert.equal(canCopyEmail(pack('communicable'), email, { subject: 'Objet', body: '' }), false)
})
test('email edits remain distinguishable from the composed draft', () => {
  const initial = initialEmailEdits(pack('communicable'))
  assert.deepEqual(initial.routing_email, { subject: 'Objet', body: 'Bonjour,' })
  assert.equal(hasEditedEmail(pack('communicable'), initial), false)
  assert.equal(hasEditedEmail(pack('communicable'), { routing_email: { subject: 'Objet édité', body: 'Bonjour,' } }), true)
})
test('copied script contains usable speech and no internal metadata', () => {
  const text = scriptText(phone, [{ objection: 'Pas maintenant', response: 'Je comprends.' }])
  assert.match(text, /Bonjour\./)
  assert.match(text, /Quel besoin \?/)
  assert.match(text, /Prenons rendez-vous\./)
  assert.match(text, /Je comprends\./)
  assert.doesNotMatch(text, /ready_to_copy|communication_status|claims_used/)
})
