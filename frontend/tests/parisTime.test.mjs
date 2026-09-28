import test from 'node:test'
import assert from 'node:assert/strict'
import { formatParisDateTime, isoToParisInput, parisLocalToIso } from '../src/parisTime.ts'

test('14:00 Paris is serialized and displayed as 14:00 in summer', () => {
  const iso = parisLocalToIso('2026-07-15T14:00')
  assert.equal(iso, '2026-07-15T12:00:00.000Z')
  assert.equal(isoToParisInput(iso), '2026-07-15T14:00')
  assert.match(formatParisDateTime(iso), /14:00/)
})

test('14:00 Paris is serialized and displayed as 14:00 in winter', () => {
  const iso = parisLocalToIso('2026-01-15T14:00')
  assert.equal(iso, '2026-01-15T13:00:00.000Z')
  assert.equal(isoToParisInput(iso), '2026-01-15T14:00')
})

test('nonexistent DST wall-clock time is rejected', () => {
  assert.throws(() => parisLocalToIso('2026-03-29T02:30'), /n’existe pas/)
})
