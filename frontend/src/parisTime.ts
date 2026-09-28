const ZONE = 'Europe/Paris'

const partsFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: ZONE, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

function parts(value: Date) {
  return Object.fromEntries(partsFormatter.formatToParts(value)
    .filter((item) => item.type !== 'literal')
    .map((item) => [item.type, Number(item.value)])) as Record<string, number>
}

function offsetMilliseconds(value: Date) {
  const local = parts(value)
  return Date.UTC(local.year, local.month - 1, local.day, local.hour, local.minute, local.second) - value.getTime()
}

/** datetime-local values are always interpreted as Europe/Paris business time. */
export function parisLocalToIso(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) throw new Error('Date et heure invalides.')
  const [, year, month, day, hour, minute] = match.map(Number)
  const wallClock = Date.UTC(year, month - 1, day, hour, minute)
  let instant = new Date(wallClock)
  for (let index = 0; index < 3; index += 1) {
    instant = new Date(wallClock - offsetMilliseconds(instant))
  }
  const check = parts(instant)
  if ([check.year, check.month, check.day, check.hour, check.minute].join('-') !== [year, month, day, hour, minute].join('-')) {
    throw new Error('Cette heure locale n’existe pas en Europe/Paris (changement d’heure).')
  }
  return instant.toISOString()
}

export function parisInputNow(value = new Date()): string {
  const item = parts(value)
  const two = (number: number) => String(number).padStart(2, '0')
  return `${item.year}-${two(item.month)}-${two(item.day)}T${two(item.hour)}:${two(item.minute)}`
}

export function isoToParisInput(value: string | null | undefined): string {
  if (!value) return ''
  return parisInputNow(new Date(value))
}

export function formatParisDateTime(value: string | null): string {
  return value ? new Intl.DateTimeFormat('fr-FR', {
    dateStyle: 'medium', timeStyle: 'short', timeZone: ZONE,
  }).format(new Date(value)) : '—'
}
