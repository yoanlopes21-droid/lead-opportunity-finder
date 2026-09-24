# Deterministic commercial approach pack

`GET /api/v1/commercial-leads/{company_key}/approach-pack?department=94` composes a read-only pack from the existing approach context, private local configuration, and commercial angle. It makes no network call and sends no message.

The response separates phone drafts by audience and situation, three email drafts, the complete objection library, three priority objection codes, evidence, and internal advice. The email requested after a call has no body in this endpoint: a future workflow must provide a recorded call request before that text becomes available. Attachments are recommendations only; no file is attached or sent.

`communication_status` distinguishes communicable drafts, prepared drafts without a channel, contact verification, and blocked outreach. Employer or offer verification and suspension remove client copy while retaining the angle and internal diagnostic. A general company address uses the routing email; it is never treated as a confirmed HR contact.

Catalog wording can enter copy only when its exact field has `client_communicable` scope in the local SQLite catalog. Missing, internal, and approval required scopes are excluded. Prices, guarantees, exclusivity, candidate availability, and past assignments require separate validated evidence or human decisions. The runtime does not derive these claims from commercial documents. Multiple adverts or locations alone never suggest the enhanced offer.

The local catalog can be reviewed against approved client material without committing the actual configuration. The internal comparison, private profile, real client references, pricing, and local recipe report stay outside Git.
