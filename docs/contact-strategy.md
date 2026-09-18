# Contact strategy

`contact_strategy` is a read-only business layer over the sourced
`person_contacts`, `contact_points`, and `contact_evidence` records. It returns
one deterministic `ContactStrategy` for a pre-existing `ContactTarget`; it does
not discover, write, or enrich contacts.

The layer deliberately has no persistence table. Its inputs are already
versioned by their contactability facts, and recomputing it avoids a stale copy
of a recommendation after a contact is corrected or made inactive.

## Selection rules

- For a company target, a sourced HR or recruitment person/service takes
  priority over a director. A director is useful when no HR/recruitment path is
  currently detected; employee range is explanatory context only, never a
  decisive threshold.
- For a local target, only a `manager` person and points with the exact same
  `local_key` are considered. Company-level facts are never propagated to a
  locality.
- An intermediary target always remains an intermediary target. The strategy
  never aims at a final client inferred from an offer.
- A direct email or phone must be explicitly linked to the selected
  `PersonContact`. No address is generated from a name and domain.
- A sourced recruitment/RH mailbox, service phone, standard, contact page, or
  professional URL can still be recommended without a named person.

The result includes the chosen person/point identifiers, ordered fallback
channels, confidence, rationale codes, user-facing short context, missing
information, inherited warnings, and provenance references for the future UI.

## Provider boundary

This layer never calls a provider. In particular, it neither invokes nor
requires Societe.com. A future UI may use an unresolved strategy to tell the
user that an optional structured source could be useful, but activating such a
source stays an explicit user action outside the strategy.
