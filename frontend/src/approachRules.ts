import type { CommercialApproachPack, EmailDraft, ObjectionResponse, PhoneDraft } from './types'

export type EmailEdit = { subject: string; body: string }

export function initialEmailEdits(pack: CommercialApproachPack): Record<string, EmailEdit> {
  return Object.fromEntries(pack.email.map((draft) => [draft.type, { subject: draft.subject ?? '', body: draft.body ?? '' }]))
}

export function hasEditedEmail(pack: CommercialApproachPack, edits: Record<string, EmailEdit>): boolean {
  return pack.email.some((draft) => {
    const value = edits[draft.type]
    return Boolean(value && (value.subject !== (draft.subject ?? '') || value.body !== (draft.body ?? '')))
  })
}

export function canCopyPhone(pack: CommercialApproachPack, draft: PhoneDraft): boolean {
  return pack.communication_status === 'communicable' && draft.ready_to_copy && Boolean(draft.opening || draft.first_30_seconds)
}

export function canCopyEmail(pack: CommercialApproachPack, draft: EmailDraft, edit: EmailEdit): boolean {
  return pack.communication_status === 'communicable' && draft.ready_to_copy && Boolean(edit.subject.trim() && edit.body.trim())
}

export function canCopyObjection(pack: CommercialApproachPack): boolean {
  return pack.communication_status === 'communicable'
}

export function copyReason(pack: CommercialApproachPack, ready: boolean, requiredEvent?: string | null): string {
  if (ready) return ''
  if (pack.communication_status === 'blocked') return 'Communication bloquée : vérifiez les points signalés avant tout contact.'
  if (requiredEvent) return 'Une demande réelle pendant un appel est nécessaire pour cette variante.'
  if (pack.communication_status === 'prepared_no_channel') return 'Brouillon préparé ; aucun canal utilisable n’est identifié.'
  if (pack.communication_status === 'verify_contact') return 'Le contact doit être vérifié avant utilisation.'
  return 'Cette variante n’est pas utilisable pour le canal actuel.'
}

export function scriptText(draft: PhoneDraft, priority: ObjectionResponse[]): string {
  return [
    draft.opening, draft.first_30_seconds, draft.continuation,
    draft.qualification_questions.length ? `Questions :\n${draft.qualification_questions.map((question) => `• ${question}`).join('\n')}` : null,
    draft.meeting_transition ? `Transition RDV :\n${draft.meeting_transition}` : null,
    priority.length ? `Objections prioritaires :\n${priority.map((item) => `• ${item.objection}\n${item.response}`).join('\n\n')}` : null,
  ].filter(Boolean).join('\n\n')
}
