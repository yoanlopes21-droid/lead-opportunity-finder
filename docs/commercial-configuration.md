# Local commercial configuration

The commercial approach context describes a lead. The private commercial configuration describes the consultant's approved claims, available offers, and commercial decisions. A later composer can read both sources without changing either. This module does not create client-facing text.

The API is local under `/api/v1/commercial-configuration`:

| Resource | Read | Replace |
| --- | --- | --- |
| Consultant profile, specialties, territories, verified references | `GET /profile` | `PUT /profile` |
| Offer catalog | `GET /offers`, `GET /offers/default`, `GET /offers/{code}` | `PUT /offers/{code}` |
| Commercial policy | `GET /policy` | `PUT /policy` |

An absent profile or policy returns `null`; an empty catalog returns `[]`. The application seeds no identity, specialties, prices, or offer names. Values entered through the local API persist in the ignored SQLite database. Replacing an offer marked as default clears the previous default. A disabled offer cannot become the default. Another offer can be read by code for a manual choice.

Each specialty carries an expertise scope: `personal`, `network`, or `method_only`. A verified reference has its own evidence scope and must be explicitly approved before it can support a claim. Territory familiarity is stored by department code; no personal address is needed.

Each catalog offer can set `communication_scopes` per field, using paths such as `display_name` or `features.phone_screen`. Allowed values are `internal_only`, `client_communicable`, and `manual_approval_required`. Missing paths in existing local catalogs default to internal use: no catalog feature enters a client-safe claim without explicit approval. Internal consultant documents should be marked internal; fields from client documents may be marked communicable after checking their meaning and limitations. The angle service never turns policy prices, guarantees, or exclusivity into client claims.

Offer features distinguish a hunt campaign's targeted candidate count from responses or presented candidates. The application processing deadline is not a hiring deadline. Documentary guarantee inclusion can be `included`, `optional`, `conflicting`, or `not_documented`; the policy separately records whether a guarantee is included and whether it is complimentary. Replacement is modeled as renewed agreed recruitment efforts, without a promised result.

The policy stores optional private rate bands and a discretionary cap. `calculate_private_scenario` returns no amount until a rate is selected. Discounts and caps require explicit arguments, and client communication requires separate consultant approval. Success fee triggering and payment term are separate fields. New-client exclusivity defaults to false; existing-client exclusivity stays a manual decision. Contract types absent from the policy have no automatic accept or reject rule.

For a local database upgrade, back up the database with SQLite's backup API, test the new tables on a copy, and compare business table counts before and after. The new tables are additive. Never put a local configuration export or commercial document in Git.
