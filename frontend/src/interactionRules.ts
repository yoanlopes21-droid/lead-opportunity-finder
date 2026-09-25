import type { InteractionChannel, InteractionOutcome, RelationshipStatus } from './types'

export const relationshipLabels: Record<RelationshipStatus, string> = {
  contacted: 'Contacté', awaiting_reply: 'En attente de retour', follow_up: 'À relancer',
  interested: 'Intéressé', meeting_scheduled: 'Rendez-vous pris', proposal_sent: 'Proposition envoyée',
  client: 'Client', no_current_need: 'Pas de besoin actuellement', refused: 'Refus',
  wrong_contact: 'Mauvais interlocuteur', do_not_contact: 'Ne plus contacter',
}
export const channelLabels: Record<InteractionChannel, string> = {
  phone: 'Téléphone', email: 'Email', other: 'Autre', professional_network: 'Réseau professionnel',
}
export const outcomeLabels: Record<InteractionOutcome, string> = {
  no_answer: 'Pas de réponse', switchboard: 'Standard / orientation', wrong_contact: 'Mauvais interlocuteur',
  conversation: 'Échange avec interlocuteur', email_requested: 'Email demandé', email_sent: 'Email envoyé',
  callback_requested: 'Rappel demandé', interested: 'Intéressé', meeting_scheduled: 'Rendez-vous pris',
  no_current_need: 'Pas de besoin actuellement', position_filled: 'Poste presque ou déjà pourvu',
  refused: 'Refus', proposal_sent: 'Proposition envoyée', client: 'Client', do_not_contact: 'Ne plus contacter',
}
export const outcomeStatuses: Record<InteractionOutcome, RelationshipStatus> = {
  no_answer: 'contacted', switchboard: 'contacted', wrong_contact: 'wrong_contact',
  conversation: 'contacted', email_requested: 'awaiting_reply', email_sent: 'awaiting_reply',
  callback_requested: 'follow_up', interested: 'interested', meeting_scheduled: 'meeting_scheduled',
  no_current_need: 'no_current_need', position_filled: 'no_current_need', refused: 'refused',
  proposal_sent: 'proposal_sent', client: 'client', do_not_contact: 'do_not_contact',
}
export function availableOutcomes(channel: InteractionChannel): InteractionOutcome[] {
  return (Object.keys(outcomeLabels) as InteractionOutcome[]).filter((outcome) =>
    (outcome !== 'email_requested' || channel === 'phone') &&
    (outcome !== 'email_sent' || channel === 'email'))
}
