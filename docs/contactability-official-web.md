# Official web contactability provider

## Officiality is separate from company identity

An exact SIREN, legal name, address, or officer list proves only that a page
describes a company. It does not prove that the company operates the domain.
`high_confidence` therefore requires an independent ownership signal, such as a
brand-coherent domain or legal notice identifying the target/brand as operator,
in addition to identity evidence. A third-party profile or directory is
rejected when its structure, site-wide legal data, or legal operator identifies
another service, even if its company facts are exact.

Brave discovery uses the company/brand, a geographic or France context, and
`site officiel` first. The SIREN remains a verification signal; it is not put
in the primary discovery query because it disproportionately returns company
data directories. The bounded fallback remains a single `contact` query.

`official_web` is the primary website-discovery path. It is independent from the
optional Societe.com provider and never starts a Societe.com request or batch.
An already persisted Societe.com website may be used as a structured candidate
only when that opt-in provider was previously run by an explicit user action.

## Phases

1. **Discovery** considers a fresh verified-site cache, persisted structured
   website candidates, URLs sourced from offers, then optional Brave Search.
2. **Verification** fetches at most the candidate page plus a small number of
   same-domain legal/contact pages and records weighted identity signals.
3. **Extraction** is a bounded third resource on an already `high_confidence`
   or `review_needed` domain. It may retain public email, phone, official site,
   contact-page URL and clearly attributed professional people.

No Brave response, fetched HTML, binary, or page archive is persisted. The
local Brave ledger records each attempted search separately from estimated
provider consumption. Connection, DNS, proxy, pool and connect-timeout failures
that occur before an HTTP request can reach Brave remain visible as attempts but
do not consume the local budget. Once dispatch may have happened (including a
read timeout or a broken connection after writing), the estimate remains counted
conservatively. HTTP responses, including 429 and 5xx, are counted.
database contains only canonical candidate URLs, short search metadata,
verification outcomes, and minimal explainable signals.

## Cache-policy versions and scores

Discovery, verification and extraction include explicit policy versions in their
resource fingerprints. A policy change naturally makes only the corresponding
resource state stale; candidates and verification records remain historical
audit artifacts until a later run replaces them. Extraction also depends on the
verification policy version and the currently verified domains.

`VerifiedOfficialSite.score` is usable official-site confidence, not a raw sum
of identity clues. A `rejected` result always has score `0`; its persisted
signals retain the explainable evidence that led to rejection. UI/API consumers
must use status together with this score, never treat a rejected record as a
high-confidence website.

## Optional Brave configuration

Set `LEAD_FINDER_BRAVE_SEARCH_API_KEY` only when Brave discovery is explicitly
enabled. Without it, cached, structured and offer-derived candidates remain
usable. Missing Brave or Societe.com credentials do not prevent the application
or local website verification from operating.

Discovery uses no more than two searches and retains at most five results per
search. A satisfactory first result avoids the fallback query. Local targets do
not trigger Brave searches by default.

## Verification and confidence

Search rank is never evidence of official status. Excluded jobboards,
directories, social networks, search/video platforms and obvious tracking
domains are rejected before verification. The verifier then weighs exact legal
identifiers, legal/display names, geography and same-domain legal pages.

`high_confidence` requires a score of at least 80, no strong conflict, and either
an exact SIREN/SIRET signal or independent identity plus legal/geographic signal
families. Other outcomes are `review_needed`, `ambiguous`, or `rejected`.
There is no automatic `confirmed` status. A SIREN observed on a website is only
verification evidence and never mutates DINUM enrichment.

National brand domains (for example retail networks) remain company/brand
candidates. They do not establish a local legal employer and are never copied
to every LocalOpportunity. A future official local page may be attached to a
`local_key` only when its location is explicit.

## HTTP safety policy

The fetcher allows GET requests to public HTTP(S) destinations on standard web
ports, with a 10-second default timeout, three redirects, a 1 MiB response cap,
HTML/text content only, and approximately one request per second per domain.
Every initial destination and redirect is DNS-resolved and rejected when any
address is non-public, loopback, private or link-local.

The initial search-result/candidate page may be fetched directly because it is
the user/provider-selected public resource being verified. Every additional
page requires an affirmative robots-policy check; without an available robots
decision, additional exploration is refused. The same page is reused from an
in-memory cache during one verification.

## Cache

- discovery: 30 days;
- discovery with no result: 30 days;
- verified `high_confidence`: 90 days;
- `review_needed`, `ambiguous`, or rejected hypotheses: 30 days.

Discovery and verification have independent provider states. Verification's
input fingerprint includes the ordered candidate set, so a changed candidate
set invalidates verification without making the generic batch understand web
artifact internals.

## Robots and contact extraction

Additional extraction pages are selected only from the verified domain: at most
six useful pages (home, contact, legal, team, careers, or an explicit local
page). `robots.txt` is fetched through the same protected GET-only fetcher and
parsed per origin. It is injectable for tests; if it cannot be obtained or
parsed for a follow-up URL, exploration fails closed. No POST, forms, login,
browser automation, PDF, binary, or broad crawl is used.

All extracted facts have `official_web` evidence with an exact page URL,
observation time, reason, and a short text excerpt; fetched HTML is never
stored. Website evidence is `source_verified` with at most `high_confidence`,
never automatically `confirmed`. People are created only where an official,
non-editorial page explicitly pairs a name with a relevant role: HR, recruitment,
director, or manager. Local facts additionally require the page to explicitly
match the local commune or location; they are never propagated to other local
opportunities. Intermediary facts remain scoped to the intermediary.

Extraction is cached separately: contacts for 30 days, people for 45 days, and
a no-result outcome for 14 days. Its fingerprint includes verified-domain
records and extraction-policy version, so a verified-domain change invalidates
it independently.
