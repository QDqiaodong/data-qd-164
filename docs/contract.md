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


# Official-result rulings and tie-broken standings v1

`fixture-standings` and `python -m fixture_scheduler.standings` read one
self-contained batch of unordered official result revisions and team rulings,
then settle two knowledge cutoffs. They accept `--source` and `--output` and
reuse the same strict JSON reader and atomic report writer as the other entry
points. Business code uses only the Python standard library, keeps no state
between batches, and never reads dates, venues, previous schedules or locks.

## Wire format

The top-level object has exactly `teams`, `cutoffs`, `results` and `rulings`.

- `teams` holds 2–40 unique identifiers under the shared `common.ident` rule.
  Its order fixes every `circle_rounds` round number, home/away direction and
  slot, and is the final tiebreak for the standings.
- `cutoffs` holds exactly two strictly increasing non-boolean integers in
  `0..10^9`. A cutoff may fall between existing `seq` values or exceed the
  largest `seq`.
- `results` and `rulings` together hold at most 20 000 rows and each may be
  empty. Every `seq` across both arrays is a non-boolean integer in `1..10^9`,
  unique across the whole batch. Row order carries no meaning and sequences
  need not be contiguous.
- A `results` row has exactly `seq`, `round`, `home`, `away`, `status`,
  `home_goals`, `away_goals`. `round` is a non-boolean integer starting at 1,
  and `round`/`home`/`away` must name one directed `circle_rounds` fixture
  exactly. Reversed home/away, unknown teams, a wrong round and bye pseudo
  matches are rejected. A fixture may carry several revisions with different
  `seq`. For `status=played` both goals are non-boolean integers in `0..1000`;
  for `status=void` both goal fields must be `null`; any other status is
  rejected.
- A `rulings` row has exactly `seq`, `team`, `active`, `deduction`. `team` must
  be declared, `active` a real JSON boolean, and `deduction` a non-boolean
  integer in `0..10^6`. One ruling wholly replaces the team's previous `active`
  and `deduction`; it is not additive. Without a ruling a team defaults to
  `active=true` and `deduction=0`.
- Every record is validated even when its `seq` lies beyond both cutoffs. Each
  layer rejects missing or unknown fields, invalid types and duplicate `seq`
  values.

## Settlement

Each cutoff is settled independently from frozen, read-only snapshot views.

- Only records with `seq <= cutoff` are visible. The last visible result per
  fixture wins; a visible `void` never falls back to an earlier `played`. A
  fixture without a visible result is unreported (`pending`).
- The last visible ruling per team wins. A withdrawn team is excluded from the
  table and every match involving it is void for both sides.
- A fixture counts only when both sides are active and the selected record is
  `played`. It adds to both teams: 3/1/0 points, matches played, wins, draws,
  losses, goals for and goals against.
- A team's net points are its match points minus its current deduction and may
  be negative. Deductions never change goals, wins/draws/losses or
  head-to-head points.
- Every active team appears, even with no valid match. Both-inactive fixtures
  are voided once and never counted.

## Ranking

Teams are first grouped by net points. Inside one initial group `G`:

1. Rebuild each member's `(head_points, head_goal_difference, head_goals_for)`
   from exactly the counted matches whose two sides both lie in `G`, and sort
   the group by that triple descending (input index only for display before
   the next step).
2. Partition the sorted group into maximal blocks with equal triples, keeping
   block order.
3. If there is more than one block, recurse into every block of size > 1: take
   the matches *inside that block only*, rebuild the whole triple from its
   first component and repeat. Parent values are never continued and only the
   next triple component is never consulted.
4. If the group's triples are all identical and no split occurs, finish the
   group by global goal difference descending, global goals for descending and
   `teams` input index ascending. That group is not revisited.
5. A single-team group finishes immediately.

Final ranks are consecutive and unique `1..N` over the teams on the table.
Head-to-head statistics never cross groups or cutoffs.

## Result

The report publishes `schema_version: 1`, `source_sha256` over the raw input
bytes, and `result` with exactly `snapshots` and `movements`.

- `snapshots` lists the two cutoffs in order. Each has exactly `cutoff`,
  `standings`, `withdrawn` and `match_audit`.
- `standings` is ordered by rank; each row has exactly `team`, `rank`,
  `played`, `won`, `drawn`, `lost`, `goals_for`, `goals_against`,
  `goal_difference`, `raw_points`, `deduction` and `points`. `raw_points` is
  before deduction and `points` is net.
- `withdrawn` lists the snapshot's inactive teams in `teams` order.
- `match_audit` covers every circle fixture, ordered by round and original slot,
  with exactly `round`, `home`, `away`, `selected_seq`, `reason`, `home_goals`
  and `away_goals`. With no visible result `selected_seq` and both goals are
  `null` and `reason` is `pending`; otherwise the selected `seq` and original
  goals are kept and `reason` is `withdrawn` when either side is inactive,
  `void` when the selected record is void, and `counted` otherwise. A played
  record excluded by a withdrawal keeps its goals purely for audit and never
  enters a scoring table.
- `movements` covers every team in `teams` order with exactly `team`,
  `before_rank`, `after_rank` and `delta`. A team absent from a table has a
  `null` rank there, and `delta` is `before_rank - after_rank` only when both
  ranks exist, otherwise `null`.

Malformed input or IO failure exits 2, explains on stderr and preserves any
existing report; legal input including empty data or a fully withdrawn field
exits 0. Successful stdout JSON equals the published file, reruns of the same
bytes are byte-identical, input and output must differ and the output may not
be a symlink.

## Implementation note: frozen snapshot views

Both cutoffs are settled from frozen read-only views rather than by streaming
`seq` forward and undoing old contributions. Each snapshot first selects the
maximum visible `seq` per fixture and per team, then derives every statistic
from scratch using only its own selected records. Nothing is mutated in place,
so a correction replaces both sides of the old result instead of adding to them,
a withdrawal removes the whole match from both sides, and restoring a team
replays exactly the results visible at that cutoff without double counting.
Because the two snapshots never share a working table, a later correction or
ruling cannot leak into the earlier table.

Ranking likewise rebuilds the head-to-head table from the current block's own
matches at every recursion level; it never reads a parent block's numbers or
only the next triple component. Correction is safe because the fixture and
group memberships are recomputed from the selected records of that same
snapshot.

## Testing

`tests/test_standings.py` checks the mandated linked example (teams
`[A, C, B, D]`, cutoffs `[7, 8]`, final tables `[A, B, C, D]` and
`[A, C, D, B]`), correction/withdrawal/restoration, the pending-versus-void
distinction, non-boolean integers, invalid future records, input shuffling,
two-level recursion and fallback metrics. An independent truth implementation
in the test module re-derives every snapshot with linear scans and a separate
recursive partition, and is cross-checked on 150 random batches plus 40
full-slate six-team batches. Boundary tests cover empty data, all-withdrawn,
two teams, odd team counts with byes and 40 teams.


# Ticket inventory and integer refund ledger v1

`fixture-tickets` and `python -m fixture_scheduler.tickets` manage an
independent, persistent SQLite ledger file. The three subcommands are
`init --db DB --source CATALOG`, `apply --db DB --source BATCH` and
`report --db DB --output REPORT`; the same initialisation, batch execution and
reporting functions are directly importable from
`fixture_scheduler.tickets`. The database is a standalone SQLite file that
survives process restarts; no other entry point's data or fields are touched,
and only the Python standard library (including `sqlite3`) is used. Tickets are
business quantities: there are no physical seat numbers, match dates or
official results.

## Catalogue wire format

The catalogue top level has exactly `teams` and `fixtures`.

- `teams` follows the repository identifier rule: 2-40 unique identifiers whose
  input order fixes every `circle_rounds` round, directed home/away match and
  slot.
- `fixtures` must cover every directed `circle_rounds` match exactly once. Each
  row has exactly `round`, `home`, `away`, `capacity`, `unit_price_cents`.
  Round numbers, home and away must match the circle rounds precisely:
  duplicates, missing matches, reversed home/away, wrong rounds and bye pseudo
  matches are rejected. `capacity` is a non-boolean integer in `0..10^6` and
  `unit_price_cents` a non-boolean integer in `1..10^6`; both are immutable
  after initialisation.
- Canonical fixture order is round number followed by the original circle slot;
  team-name string ordering is never used.
- `init` validates the whole catalogue before creating anything, then creates
  the ledger with application and schema-version markers. Any existing file or
  symlink at the database path is refused; an invalid catalogue leaves no
  half-initialised database, `-wal` or `-shm` file behind.

## Batch wire format and canonical request identity

A batch has exactly `batch_id` and `operations`. `batch_id`, `order_id` and
`refund_id` follow the identifier rule. `operations` is an ordered array of
1-1000 items.

- A buy operation has exactly `kind="buy"`, `order_id`, `discount_cents` and
  `lines`. Each line has exactly `round`, `home`, `away`, `quantity`; the
  fixture must be declared, a fixture appears at most once per order, and
  `quantity` is a non-boolean integer in `1..2000`. An order totals at most
  2000 tickets, `lines` is non-empty and bounded by the fixture count, and a
  batch issues at most 20000 new tickets. `discount_cents` is a non-boolean
  integer from zero up to the order's gross total. Every ticket's gross price
  is the fixture's initialised unit price; all tickets of an order are
  determined together and partial fills never occur.
- A refund operation has exactly `kind="refund"`, `refund_id`, `order_id` and
  `ticket_nos`. `ticket_nos` is a non-empty, duplicate-free array of the
  order's original ticket numbers, never longer than the original order.
  Only successfully bought, not-yet-refunded original tickets can be refunded.
  Negative or non-existent tickets and second refunds are rejected. A batch
  may refund tickets bought earlier in the same batch and buy again after a
  refund.
- `order_id` is permanently unique across the whole database and is not
  released even after a full refund. `refund_id` is permanently unique in its
  own namespace, independent of order identifiers. Reusing a business
  identifier fails unless it is a retry of the identical original batch.

The canonical request text is produced from the parsed batch with
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)` and encoded as UTF-8. Object key order and indentation do not
affect identity, while every array order is preserved and semantic. The
canonical text and its SHA-256 are stored; retries compare the actual
canonical text rather than trusting the digest.

## Integer discount allocation

For one order, let ticket `i` have gross `g_i`, total gross
`G = sum(g_i)` and order discount `D`. Each ticket first receives
`floor(D * g_i / G)`. The remaining cents are allocated one at a time to the
tickets with the largest `(D * g_i) mod G` remainder; ties follow canonical
ticket order. The ticket's `paid_cents = g_i - discount_share`. All arithmetic
is integer-only: no floating point, banker's rounding or average-price
substitution exists. `D = 0` and `D = G` are both legal.

The order-level discount is therefore frozen per ticket at purchase time with
the largest-remainder method, ties broken by canonical ticket order. A partial
refund returns only the frozen paid amount of the named tickets; the discount
is never redistributed over the remaining tickets and current average prices
are never used.

## Ticket expansion and inventory

Tickets of an order are expanded in canonical fixture order first, then by
quantity sequence inside a fixture, receiving `ticket_no` values 1, 2, 3, ...
that never change for that order.

Each buy checks the remaining inventory of every named fixture before any
ticket exists. A refund only releases the named, not-yet-refunded tickets of
its order. Operations inside one batch execute strictly in order, so a later
order can use inventory released by an earlier refund; if any operation fails,
the whole batch rolls back its inventory, money and all business identities.
A refund simultaneously releases fixture inventory and records the original
paid refund; a zero-paid ticket still releases its seat. A cross-fixture order
whose final fixture is sold out leaves no tickets, money or used order number
on the other fixtures.

Every write performs its capacity checks, facts and batch receipt in one
database transaction (`BEGIN IMMEDIATE`). Two connections competing for the
last ticket can have at most one successful issuer; the loser reports
sold-out or a lock timeout without partial effects. A failure never consumes a
batch, order or refund identifier.

## Idempotent batches

After a batch commits, its `batch_id` is matched against the stored canonical
request. The identical request returns the original frozen receipt without
re-validating current inventory or refund state, even when the tickets have
since been refunded or the fixture sold out; no extra tickets or charges are
created. A different request under the same `batch_id` is rejected. Because
the receipt is committed in the same transaction as the facts, a CLI output
failure after commit can be recovered by retrying the identical request, which
returns the stored receipt exactly once; the implementation never claims such
a failure was rolled back.

## Receipts and report

A successful `apply` prints an envelope whose top level is exactly
`schema_version: 1` and `result`. The result has exactly `batch_id`,
`request_sha256` and `receipts`, with receipts in operation order.

- A buy receipt has exactly `kind`, `order_id`, `gross_cents`,
  `discount_cents`, `paid_cents`, `tickets`. Tickets are ordered by
  `ticket_no`; each has exactly `ticket_no`, `round`, `home`, `away`,
  `gross_cents`, `discount_cents`, `paid_cents`.
- A refund receipt has exactly `kind`, `refund_id`, `order_id`, `ticket_nos`
  (ascending) and `refunded_cents`, the sum of the named tickets' frozen paid
  amounts.

The report envelope again has `schema_version: 1` and `result`, with exactly
`catalog_sha256` (the digest of the original catalogue input bytes),
`batch_count`, `fixtures`, `orders` and `totals`.

- `fixtures` covers every fixture in canonical order. Each row has exactly
  `round`, `home`, `away`, `capacity`, `unit_price_cents`, `issued_tickets`,
  `refunded_tickets`, `active_tickets`, `available_tickets`, where quantities
  are full-history cumulative or current values,
  `active = issued - refunded` and `available = capacity - active`.
- `orders` is sorted by the Python string order of `order_id`. Each row has
  exactly `order_id`, `gross_cents`, `discount_cents`, `paid_cents`,
  `refunded_cents`, `retained_cents`, `tickets`. Tickets use the buy receipt
  fields plus `refund_id`, which is `null` when the ticket is still active.
- `totals` has exactly `gross_cents`, `discount_cents`, `paid_cents`,
  `refunded_cents`, `retained_cents`, `active_tickets`. Money totals are sums
  over orders with `paid = gross - discount` and
  `retained = paid - refunded`; `active_tickets` equals the sum of per-fixture
  active values. All money is integer cents. Zero-money refunds still count in
  `refunded_tickets`. An empty ledger reports complete zero values and an empty
  `orders` array.

The report's multi-table reads belong to one read snapshot and never mutate
business state; when state is unchanged, repeated reports are byte-identical.
`report` reuses `common.save`, so stdout JSON equals the published file, the
output cannot be a symlink and a failed publication preserves the previous
report. No clocks or random values appear in any output.

## Files, errors and concurrency

The shared reader keeps the 8 MB limit, duplicate-key rejection and
non-finite-number rejection; every layer rejects missing or unknown fields and
wrong types, including future operations that would only fail at execution
time. `apply` and `report` reject a missing path, a symlink, or a database that
is not a compatible `fixture-tickets` ledger; they never create or migrate one.
The source or report target cannot point at the database file.

Business, input, SQLite and file errors exit `2` with a stderr message. A
failed database transaction preserves the prior business state; a failed report
publication preserves the old report. No cross-file/SQLite distributed
transaction and no power-loss file recovery are promised.

## Implementation note: facts versus maintained projections

Two designs satisfy the ledger rules.

- **Maintained per-operation inventory and balances.** Every operation updates
  per-fixture active counts and per-order money directly. All copies of the
  state and the batch identity must then be written in one transaction so that
  a failure cannot leave divergent projections.
- **Projections from immutable sale/refund facts.** Buy and refund facts are
  inserted once, sale-to-refund links stay unique, and inventory and money are
  computed from those facts inside one read snapshot. Later operations in a
  batch must still observe refunds made by earlier operations.

The chosen implementation combines them deliberately: immutable `orders`,
`tickets`, `refunds` and `batches` facts are the durable source of truth, while
per-fixture `active_tickets` and per-order totals are maintained inside the
same write transaction and are independently re-derived by the report queries.
Because both representations commit or roll back together and the report uses a
single snapshot, either design alone would produce the same business result.

# Official-result driven refund synchronisation v1

`sync-refunds` is the fourth `fixture-tickets` subcommand
(`fixture-tickets sync-refunds`, importable as
`fixture_scheduler.tickets.sync_refunds`). It reconciles the ticket ledger
against one selected snapshot of a `fixture-standings` report and issues
refunds for fixtures the official result settles as `void` or `withdrawn`.
Like the rest of the ledger it uses only the Python standard library, never
contacts a payment network or external service, and never writes outside the
named SQLite database and receipt file.

## Invocation and inputs

```
fixture-tickets sync-refunds --db DB --source STANDINGS_REPORT \
    --cutoff N --batch-id ID --output RECEIPT
```

- `--db` must be an existing, compatible `fixture-tickets` ledger (the same
  application marker and `schema_version=1` checks as `apply`/`report`); the
  command never creates or migrates the application version.
- `--source` is read with `common.read`, so the 8 MB limit, duplicate-key and
  non-finite-number rejections apply. It must be a `fixture-standings`
  envelope with top-level fields exactly `schema_version`, `source_sha256` and
  `result`, where `schema_version` is the integer `1` and `result` has exactly
  `snapshots` and `movements`.
- `--cutoff N` must be a non-boolean integer and must match the `cutoff` of
  exactly one snapshot; zero matches or multiple matches are rejected. Each
  snapshot must have exactly `cutoff`, `standings`, `withdrawn` and
  `match_audit`.
- `--batch-id` passes `common.ident`, so it is a non-blank identifier of at
  most 128 characters with no control characters.
- The receipt path may not be a symlink and may not equal either the database
  or the standings source.

## Snapshot fixture identity and reasons

The selected snapshot's `match_audit` rows each have exactly `round`, `home`,
`away`, `selected_seq`, `reason`, `home_goals` and `away_goals`. Rows must
carry unique directed `(round, home, away)` keys, and that key set must equal
the fixture set of `tickets.build_report(DB)` exactly: a missing, extra or
duplicate directed fixture (including reversed home/away or a wrong round) is
rejected before any write. The only accepted `reason` values are `pending`,
`void`, `withdrawn` and `counted`; any other string fails the command.

## Refund eligibility and canonical operations

A ticket qualifies only when all of the following hold:

1. its fixture's audit reason is `void` or `withdrawn`; and
2. its own `refund_id` is empty (it has never been refunded).

`pending` and `counted` fixtures never trigger a refund. Already-refunded
tickets are skipped, and a later snapshot that changes a fixture back to
`counted` never reverses a historical refund or re-charges a ticket. Every
refund returns the ticket's original frozen `paid_cents`; discount shares are
never recomputed.

Qualifying tickets are grouped by `order_id` (one refund operation per order)
with the `ticket_no` values ascending, and the operations are ordered by
`order_id`, giving a stable canonical request independent of source row order.
The complete request is rejected as a whole when it contains more than 1000
refund operations or covers more than 20000 tickets.

## Refund identity

For each order the `refund_id` is

```
frs- + sha256_hex(
    batch_id + NUL + standings_report_sha256 + NUL +
    decimal_cutoff + NUL + order_id + NUL +
    comma_joined_ascending_ticket_nos
)
```

encoded as UTF-8, where `standings_report_sha256` is the SHA-256 of the raw
standings report bytes. The identifier is `frs-` followed by 64 lowercase
hexadecimal characters (68 characters total, within the 128-character limit)
and is bound to the official input, the cutoff and the exact refunded ticket
identity. These refund ids live in the same permanent namespace as the
`refund_id` values of manual batches.

## Atomic execution, replay and conflicts

The canonical refund request is executed through the same atomic
`apply_batch` machinery (`BEGIN IMMEDIATE`, whole-batch simulation then
commit), so inventory release, money movement, the refund facts and the batch
identity commit or roll back together. A later competing refund that takes a
ticket first therefore rejects the entire synchronisation batch without
partial effects and without consuming the `batch_id`.

- The first synchronisation with a given `batch_id` stores its canonical
  request. Repeating the same `batch_id`, standings hash, cutoff and canonical
  request returns the original frozen receipt and never refunds twice.
- The same `batch_id` with a different standings hash or a different cutoff is
  rejected as a different canonical request even when the refundable ticket
  set is identical, because the generated refund ids differ. A different
  canonical request under a reused `batch_id` is likewise rejected. Regular
  buy/refund batches and synchronisation batches share the one batch id
  namespace.
- When no ticket qualifies, the command succeeds with `status=noop`, zero
  refunded cents and ticket count and an empty `refunds` array. It does not
  call `apply_batch`, does not modify the database, does not add the additive
  sync-receipt column to an older file and does not occupy the `batch_id`; the
  id remains usable for a later real synchronisation.

The full synchronisation receipt is frozen in the same database transaction
as the refund facts in an additive nullable `batches.sync_receipt_json`
column. Fresh ledgers create the column directly; older `schema_version=1`
ledgers receive it via `ALTER TABLE` only when a write is actually required,
and older code paths never read it.

## Receipt

The published envelope has exactly `schema_version: 1` and `result`. The
result has exactly these fields:

- `status`: `synced` for a first successful synchronisation that issued
  refunds, `noop` when nothing qualified, and the original first-run status on
  an identical replay;
- `standings_report_sha256`: the SHA-256 of the raw source bytes;
- `cutoff`: the selected cutoff;
- `batch_id`: the caller-supplied identifier;
- `refunded_ticket_count` and `refunded_cents`: totals across this run's
  refunds (both zero for a noop);
- `refunds`: the refund receipts in canonical order, each with exactly `kind`,
  `refund_id`, `order_id`, ascending `ticket_nos` and `refunded_cents`;
- `report`: the complete post-execution `fixture-tickets report` envelope.

The receipt is published atomically with `common.save`, so stdout JSON equals
the file, the output cannot be a symlink and a publication failure preserves
any previous receipt. Every validation step finishes before the first
persistence; any validation or ledger failure exits `2` with a stderr message
and leaves both the database and any existing receipt unchanged.

## Testing

`tests/test_tickets_sync.py` and `tests/test_tickets_sync_cli.py` cover the
mandated linked example (a `void` fixture with two unrefunded tickets and one
already-refunded ticket, a `withdrawn` fixture and a `counted` fixture),
per-ticket paid amounts and report conservation, the exact NUL-joined refund
id derivation, identical-batch replay, a later cutoff that reclassifies the
void fixture as `counted` without reversing or duplicating refunds, the noop
path, hash/cutoff/canonical-request conflicts, shared batch-id namespaces,
a competing late refund that rejects without partial modification, invalid
cutoffs/sources/snapshots/reasons/identifiers, fixture-set mismatches,
incompatible databases, path and symlink collisions, the 1000-operation and
20000-ticket limits, legacy-ledger column migration, and unchanged
`init`/`apply`/`report` behaviour.

# Ticket ledger testing

`tests/test_tickets.py` and `tests/test_tickets_cli.py` cover the
hand-calculated linked example, a cross-fixture sell-out that rolls the whole
batch back, refund-then-resell inside one batch, zero-paid refunds,
largest-remainder ties with equal and unequal prices, split refunds whose
cumulative total equals the original paid amount, identifier reuse after
failure, replaying an original buy batch after its tickets were refunded,
same-identifier conflicts, duplicate refund identifiers, invalid match
identities, boolean money, invalid future operations, real SQLite write errors,
post-commit output recovery, JSON key order/whitespace invariance,
array-permutation conflicts, and last-ticket contention through both real
SQLite connections and separate operating-system processes. Reports are
independently checked for ticket and money conservation, and the three
original entry points keep their full regression suites.

# 下游日历差量同步 fixture-calendar v1

`fixture-calendar` 和 `python -m fixture_scheduler.calendar` 把已获准发布的完整赛程，
按订阅端的球队或场馆范围，以新增、改期、换馆与撤下差量的形式同步给各自独立的下游日历
（例如球队网站与场馆公告）。状态保存在一季一库、独立于票务账本的本地 SQLite 文件中，
每条命令都是独立进程；业务运行仅用 Python 标准库（含 `sqlite3`），不连接任何外部服务，
也不读写其他入口的文件。库内带 `application=fixture-calendar` 与
`schema_version=1` 标记，其他应用或不兼容版本的库一律拒绝，且除 `init` 外不会创建缺失的库。

## 命令与输入

```
fixture-calendar init      --db DB --source SEASON
fixture-calendar publish   --db DB --revision N --source REPORT
fixture-calendar subscribe --db DB --subscriber ID --source SCOPE
fixture-calendar prepare   --db DB --subscriber ID
fixture-calendar ack       --db DB --subscriber ID --package ID
```

所有 `--source` 都经 `common.read` 读取，因此 8 MB 上限、重复 JSON 键拒绝和非有限数值拒绝
全部适用；标识遵循 `common.ident`，整数按非布尔整数校验。输入文件不得与数据库指向同一文件，
数据库路径不接受符号链接。业务、输入或 SQLite 错误以退出码 2 与 stderr 说明结束，失败不留下
部分业务状态：写入命令在 `BEGIN IMMEDIATE` 单事务内先完整校验再落库，任何错误都回滚。

### init：赛季声明

`SEASON` 顶层恰含 `teams` 与 `venues`：`teams` 为 2–40 个、`venues` 为 1–40 个无重复标识。
球队顺序直接决定 `model.circle_rounds` 生成的全部有向 `(round, home, away)` 比赛键，也是之后
发布校验的唯一身份来源。日期在本应用中始终是不透明标识，不做任何历法换算。已有文件、目录或
符号链接一律拒绝覆盖；非法输入会删除半成品库及 `-wal`/`-shm` 残留。

### publish：官方修订

`REPORT` 必须是项目既有的 `schema_version=1` 信封（顶层恰含 `schema_version`、
`source_sha256`、`result`），接受两种 `result` 形状：

- 生成器：恰含 `round_count`、`fixture_count`、`fixtures`；两个计数必须分别等于该赛季
  `circle_rounds` 的轮数与有向比赛总数，且与实际行数一致；
- 修复器：恰含 `feasible`、`objective`、`assignment_vector`、`fixtures`；只有
  `feasible=true` 可发布，`feasible=false` 整份拒绝。

其他结果形状、非整数 1 的 `schema_version`、空 `source_sha256` 均拒绝。每个比赛行恰含
`round`、`home`、`away`、`date`、`venue` 五字段，且必须满足：

- 行集合与该赛季全部有向 circle 比赛键完全相等：缺场、重复、额外场次、颠倒主客、错误轮号、
  未知球队、bye 伪比赛一律拒绝；场馆必须是赛季已声明场馆；
- 同一轮所有行共用一个日期、同轮场馆不重复，轮与轮之间日期互异；报告行序无语义，校验后按
  `(round, home, away)` 规范序存储。

这里只核可发布结构，不会从源排程约束重求可行性，也不计算修复代价。修订号是 1..10^9 的非布尔
整数，首次必须为 1，此后必须恰好等于当前号加 1；任何拒绝都不推进修订号，随后正确的下一修订
仍可发布。发布是整体替换：新修订写入一份全新的完整 `official_fixtures` 快照，旧修订原样保留。

### subscribe：订阅范围

`SCOPE` 顶层恰含 `teams` 与 `venues`，两者都是无重复的赛季已声明标识数组，均允许为空。
比赛只要任一参赛队在 `teams` 中、或当前场馆在 `venues` 中即进入该端视图，两个条件同时命中
仍只保留一行；两组皆空表示该端视图为空。命令用于新增订阅端或替换其当前范围；范围变更不触及
该端的确认视图、最近确认包标识或待确认包。

## 端点、差量与包生命周期

- 旧端点是该端**最近一次已确认包的完整目标视图**（首次确认前为空），随确认事实持久化，
  永远不用当前订阅范围重算。
- 新端点是当前官方修订按当前订阅范围筛出的视图。
- 比较按有向比赛键 `(round, home, away)`：新有旧无、或日期/场馆变化的比赛发送完整五行
  `upserts`；旧有新无的比赛只发送三字段 `{round, home, away}` 撤下标记；未变化的比赛不发送。
  两个数组都按轮号、主队标识、客队标识升序排列。

`prepare` 供已登记端拉取包；尚无官方赛程或未知订阅端报错。其行为：

- 若该端已有待确认包，直接返回库内冻结的原包（含原始差量与目标修订），即使期间发生了新发布
  或改范围——同一订阅端至多一个待确认包；
- 否则以当前官方修订和当前范围计算新端点，与确认视图比较：无变化时返回
  `status="unchanged"`、`package_id=null`、两个空数组与当前修订号，不建任何待确认行，也不改
  已确认视图；有变化时生成库内唯一的不透明 `pkg-` 标识，并把包、渲染好的两类数组、目标修订
  以及完整目标视图（`package_fixtures`）在同一事务内全部持久化后才输出
  `status="pending"`。

`prepare` 的 stdout 是恰含 `status`、`package_id`、`revision`、`upserts`、`removals` 的
JSON 对象；`revision` 是该包目标视图所用的官方修订号。

`ack --package ID`：

- 标识匹配该端待确认包：确认视图整体替换为该包冻结的目标视图，删除待确认包及其目标快照，
  记录最近确认标识，返回 `status="acked"`；
- 标识等于该端最近一次已确认标识：即使当前另有待确认包，也返回 `status="already_acked"`，
  待确认包原样保留（重试上一个已确认包永远不会确认新包）；
- 其他标识（包括另一端的包标识、从未存在的标识）一律报错。未知订阅端同样报错。

## 持久化与可恢复性

包的可恢复性不依赖调用方内存，而依赖落库事实：每个待确认包在 `packages` 中冻结自己的
`revision`、`upserts`、`removals`，并在 `package_fixtures` 中冻结完整目标视图；旧端点则由
`confirmed_fixtures` 中最近确认视图承担。因此赛程发布新版与订阅范围改变可以先后或同时发生，
重取仍返回原包，确认只推进到该包实际交付的视图，新进程从同一 SQLite 文件得出完全相同的
重取与确认行为。包标识由库内单调序列、订阅端、目标修订与两端视图的规范 JSON 共同散列生成，
再以主键和每端部分唯一索引兜底唯一性。所有写操作先提交后打印；即便提交后 stdout 投递失败，
重试同一命令也只会取回已冻结的包或重放 `already_acked`，不会产生重复效果。

## 旧视图与旧输入何时可以清理

- 待确认包未确认前，它的目标快照（`package_fixtures`）和该端当前确认视图都不可删除，否则迟到
  的确认或重取无法复原；包只在成功 `ack`（或订阅端整体退役）时移除，没有自动过期。
- 旧修订的 `official_fixtures` 行在仍被任何待确认包引用时不可清理；所有订阅端都不再持有引用
  该修订的待确认包后，旧修订才成为可归档/删除对象。已确认视图只存当前一份，确认新包即原子
  替换旧视图，无需手工清理。
- 订阅端永久退役时，才可连同其范围、确认视图、待确认包与包快照一起删除；本赛季库本身按季
  独立，赛季结束并完成全部确认后整库归档即可。
- 输入 JSON（赛季声明、发布报告、范围文件）只在对应命令成功读取后需要保留多久由调用方决定：
  发布成功后报告内容已完整入库，源文件可立即删除；库内另存每次发布的 `source_sha256` 供
  溯源，若需要按原始字节对账则应自行归档报告文件。

## 测试

`tests/test_calendar.py`（进程内）与 `tests/test_calendar_cli.py`（独立进程，经
`python -I` 包装器）覆盖：题目指定的两端同订 V1、待确认期间发布改馆并让一端改订 V2、迟到
确认旧包后改订方收到同键 upsert、留守方收到撤下、应用差量后本地视图与目标视图逐键相等；
待确认包跨进程重取稳定；重试旧确认标识不确认新包；跨端标识与未知标识拒绝；无新发布只改范围
的纯撤下；球队与场馆同时命中不重复、缩范围只撤该撤的行；日期/场馆变化为 upsert 且未变化行
不发送；被拒绝的发布（不可行修复、未知形状、错误计数、缺场/重复/额外/颠倒/错轮/未知场馆、
轮日期与场馆冲突、修订号跳号）和被拒绝的确认不影响后续差量；40 队边界、空范围、严格读取器
限制、路径/符号链接/异构库拒绝以及 `unchanged` 不留下待确认包。`scripts/calendar_demo.sh`
提供可复现的端到端命令演示。既有四个入口与其全部回归测试保持不变。
