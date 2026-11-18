# Availability-constrained round robin v1

Generate one round-robin match per unordered team pair using the circle method. Odd team counts receive one bye per round. Each whole round is assigned to a distinct supplied date; every participant must be available on that date and each match needs a venue not blacked out that date. A deterministic augmenting-path bipartite matcher places rounds in order of fewest eligible dates, then round index. It visits dates in input order and can reassign previously placed rounds to find a complete matching; venues follow input order. Search work is polynomial in the bounded round/date graph.

Teams, dates, venues are unique identifiers; date identifiers are opaque labels in this baseline. Team and venue blackout maps may only name declared identities, and each value must be an array of unique supplied dates. The input must provide enough feasible dates and venues; otherwise the schedule fails rather than dropping matches. This baseline does not optimize travel, home/away streaks, broadcast preferences, or multi-leg tournaments.

The synthetic four-team fixture produces three rounds and six fixtures across three dates.

# Minimal-disruption repair v1

`fixture-repair` and `python -m fixture_scheduler.repair` read one self-contained
batch and publish a complete repaired schedule or an explicit infeasible result.
They accept `--source` and `--output` and reuse the same strict JSON reader and
atomic report writer as the baseline generator. Business code uses only the
Python standard library.

## Wire format

The top-level object has exactly `teams`, `dates`, `venues`, `team_blackouts`,
`venue_blackouts`, `previous`, `fixture_venues`, `locked_rounds` and
`locked_fixtures`.

- `teams` (2–40), `dates` (1–100) and `venues` (1–40) are unique identifiers.
  `teams` order fixes every `circle_rounds` round, home/away direction and slot.
  Input positions of `dates` and `venues` define the zero-based canonical
  indices. Date identifiers remain opaque labels: no interval or travel
  inference is performed.
- `team_blackouts` and `venue_blackouts` map declared identities to arrays of
  unique declared dates. Blackouts express retired dates or venues; they never
  invalidate the structural identity of the previous schedule.
- `previous` has exactly one row per `circle_rounds` fixture with fields
  `round`, `home`, `away`, `date`, `venue`. Each row must match one directed
  circle fixture exactly: no missing, duplicate, re-rounded or reversed match.
  Rounds share one old date, distinct between rounds; old venues are distinct
  within a round. Old dates and venues must be declared. Row order is not
  meaningful.
- `fixture_venues` is a sparse permission array with fields `round`, `home`,
  `away`, `venues`. Each row names an existing directed fixture and a
  duplicate-free array of declared venues. Omitted fixtures allow every
  declared venue; an explicit empty array means no permitted venue and is a
  business infeasibility, not malformed input. Duplicate rows for the same
  fixture, unknown fixtures and unknown venues are invalid.
- `locked_rounds` is a duplicate-free array of valid round numbers and locks
  that round's old date only. `locked_fixtures` is a duplicate-free array of
  `{round, home, away}` objects and locks that fixture's old venue only; its
  date may still move. Both arrays may be empty. Arrays are bounded by the
  round count and total fixture count.

Every layer rejects unknown fields, wrong types and unknown references.

## Feasibility and objective

A round is executable on a date when no participating team is blacked out,
the date is free for that round's service and a duplicate-free venue can be
assigned to every fixture on that date, respecting venue blackouts,
per-fixture permissions and locked venues. Sufficient global venue capacity
does not imply a round is executable; only the per-date matching decides.
All circle rounds occupy distinct dates.

The objective, in priority order, minimizes:

1. the number of rounds whose date changes (`moved_rounds`);
2. the number of fixtures whose venue changes, compared by the same fixture
   identity even when its date also moves (`changed_venues`);
3. the canonical vector, ordered by round number: each round contributes its
   date index followed by every fixture's venue index in original
   `circle_rounds` slot order.

String ordering is never used as a substitute for input indices. Fixture
grouping, home/away direction and round numbers are immutable.

## Result

A feasible run publishes `schema_version: 1`, `source_sha256` over the raw input
bytes, and a `result` with exactly `feasible: true`,
`objective: {moved_rounds, changed_venues}`, `assignment_vector` and `fixtures`.
Each fixture row reuses the five `previous` fields and rows are ordered by
round and original slot. When no complete schedule exists the command still
exits 0 and publishes `{feasible: false, objective: null,
assignment_vector: null, fixtures: []}`; no partial schedule is delivered.
Malformed input or IO failure exits 2, explains on stderr and preserves any
existing report. Source and output must differ and the output may not be a
symlink. Successful stdout JSON equals the published file and reruns of the
same bytes are byte-identical.

## Implementation note: objective encoding

Two designs were considered:

- **Layered sequential optimisation.** Solve dates for minimum moved rounds,
  freeze that count, re-solve venues, freeze it, then solve the vector. This is
  simple but needs three search passes and careful re-validation, and an
  accepted venue change can silently invalidate an earlier date optimum.
- **Single integer scalar per cell.** The chosen design assigns every
  (round, date) cell a cost
  `moved * BIG + changed * UNIT + vector_contribution`, with integer radix
  `base = max(|dates|, |venues|, 2)` and `UNIT = base^positions` (`positions` is
  the canonical-vector length). Because `UNIT` exceeds any possible sum of
  canonical digit weights, `changed` can never leak into the moved count; and
  because `BIG = (|fixtures| + 1) * UNIT` exceeds the total of all `changed`
  and vector terms, high-priority objectives can never be broken by later
  optimisation. The outer minimum-cost bipartite assignment then minimises the
  sum exactly, so the priority order is a property of the encoding rather than
  of a post-hoc filter. No floating point is involved.

The venue cost of a cell is not a cheap pre-computed summary: each cell runs
a minimum-cost distinct-venue assignment over the real per-date availability,
permission, blackout and lock masks. That binding means an unavailable or
permission-incompatible date is removed from the date graph, and a date whose
venue solution changes forces the global date assignment to re-optimise.

## Testing

Small instances are cross-checked against an independent exhaustive oracle that
visits every feasible date permutation and venue assignment. Production-sized
tests use 40 teams, 100 dates and 40 venues and never enumerate date or venue
permutations. The integration fixture demonstrates a round whose date has two
free venues but whose per-fixture permissions leave only one matchable venue,
forcing a global date move; locking the other two rounds makes it infeasible.

