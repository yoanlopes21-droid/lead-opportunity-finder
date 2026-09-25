import test from 'node:test'
import assert from 'node:assert/strict'
import { availableOutcomes, channelLabels, outcomeLabels, outcomeStatuses, relationshipLabels } from '../src/interactionRules.ts'

test('every result has a visible label and proposed commercial status', () => {
  for (const outcome of Object.keys(outcomeLabels)) {
    assert.ok(relationshipLabels[outcomeStatuses[outcome]])
  }
  assert.equal(outcomeStatuses.callback_requested, 'follow_up')
  assert.equal(outcomeStatuses.position_filled, 'no_current_need')
  assert.equal(outcomeStatuses.do_not_contact, 'do_not_contact')
})

test('email request follows a phone exchange and sent email uses email channel', () => {
  assert.ok(availableOutcomes('phone').includes('email_requested'))
  assert.ok(!availableOutcomes('email').includes('email_requested'))
  assert.ok(availableOutcomes('email').includes('email_sent'))
  assert.ok(!availableOutcomes('phone').includes('email_sent'))
  assert.equal(channelLabels.phone, 'Téléphone')
})
