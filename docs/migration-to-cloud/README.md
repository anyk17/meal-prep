# Household meal planner: NAS app and public read-only calendar

Code layout and dependency rules: see [docs/ARCHITECTURE.md](../ARCHITECTURE.md).

**Status:** Phases 1–3 are complete on `feature/cloud-migration`. Saved weeks live in SQLite with append-only revisions; home users can create, preview/accept/discard regeneration, undo, and fill a month (a batch of independent weekly plans) in API mode, while static mode stays read-only. Weekly rules are domain specifications checked on every saved plan; the OR-Tools solver is an adapter behind a port (see [ARCHITECTURE.md](../ARCHITECTURE.md)). Backend, frontend, contract checks, and a browser walkthrough (2026-10-21) pass. Phase 4 (month/year navigation, shopping trips that cover weeks, and a minimal manual exporter so the public site shows what to buy) is next.

## Starting point and first milestone

Today, `planner/mealplanner/entrypoints/cli.py` loads YAML inputs and runs OR-Tools to generate JSON output. `planner/mealplanner/render.py` (since split into `application/plan_output.py` for construction and `adapters/files.py` for file writes) combined response construction with file writes. `site/src/App.jsx` fetches `plan.json` and `shopping_list.json` directly; there is no Flask API or database. Plans identify weekday/meal slots without calendar dates, and grocery checks live in the browser.

The first delivery milestone combines phases 1 and 2: preserve today's weekly viewer while introducing dates, a local Flask read API backed by the current files, and interchangeable API/static frontend readers. Browser generation and persistence arrive in phase 3; the month view arrives in phase 4. Dates and stable identities belong in the first contract so those later features can evolve without replacing it.

## Goal and operating model

Run the household's authoritative meal-planning app on a NAS inside the home network. The app uses Flask, SQLite, and one React frontend served from the same origin as its API. Household members use it on the home LAN with app authentication and server-side authorization. Only loopback development may bypass login. The NAS makes outbound requests to publish an explicitly selected, read-only calendar to a separate public GitHub Pages repository. There are no inbound public NAS ports, tunnels, or remote app sessions in this design. GitHub Pages never calls the NAS, runs the solver, receives app writes, or contains the authoritative database.

The public site is intentionally public. Anyone with its URL can read the published plans, weekly shopping totals, and published shopping trips (what to buy, and which weeks a trip covers). Reading a shopping trip away from home is a primary use of the public site. It contains only approved output. It does not contain the SQLite database, drafts or unaccepted proposals, revision history, planner inputs, pantry settings, household checkmarks, secrets, private app configuration, or credentials. Publication is a deliberate copy of selected current plans; saving a plan at home does not publish it.

```text
Home LAN: browser → React in API mode → Flask → application/domain services
                                                ├── SQLite (authoritative)
                                                └── OR-Tools services
                         NAS publisher ── outbound approved bundle ──┐
                                                                   ↓
Anywhere: browser → GitHub Pages: React in static mode + published JSON

One frontend codebase, two deployments; no public-site connection to the NAS.
```

The same React app, components, and API-shaped read models serve both modes. At build or startup configuration, the frontend selects exactly one trusted mode: `api` or `static`. There is no automatic fallback between them. API mode reads the NAS application from the same origin. Static mode reads the publication index and its same-origin files. GitHub Pages is always built in static mode and is never configured with the NAS API URL. Neither mode changes how cards, recipe details, statistics, or shopping totals are rendered. Mode and available actions come from trusted application/build configuration; hiding a button is only a UI choice, never authorization.

The NAS remains useful when the public site is unavailable. Publication failure must not block household planning or change saved plans. Public visitors see the last successfully published version and cannot tell whether newer unpublished changes exist on the NAS.

## Boundaries and shared read contract

Keep the two adapter boundaries distinct:

| Boundary | File implementation | Database / HTTP implementation | Consumer |
| --- | --- | --- | --- |
| Planner inputs: recipes, ingredients, rules, pantry | YAML reader | SQLite reader | generation and shopping services |
| Saved plans and shopping snapshots | JSON snapshot reader | SQLite plan reader | Flask read services and publisher |
| Frontend reader (both supported) | versioned publication-file adapter | HTTP adapter | shared React views |

The frontend reader exposes `listPlans(range)`, `getPlan(planId)`, and `getShoppingList(planId, revision, snapshotId)`. HTTP mode maps these to the versioned API. Static mode resolves the same reads through one publication index and its relative file paths. Both return the same `PlanSummary`, `Plan`, and `ShoppingList` data shapes. Adapters normalize transport, missing-file, stale-snapshot, and malformed-data failures into application errors the UI can display; static mode does not pretend those failures are HTTP status codes. Static mode is read-only regardless of how its files are served.

On the server, adapters own YAML/JSON/file/SQL details. Application services use validated domain data, not paths, YAML structures, SQL rows, Flask globals, or database sessions. The solver receives explicit recipe, ingredient, rule, and pantry data. Keep the CLI as a caller of the same generation and shopping services so moving the API to SQLite does not change the solver's calculations.

Apply focused DDD: agree vocabulary and invariants in phase 1, establish boundaries in phase 2, then implement the weekly-plan aggregate and change workflows in phase 3a before persistence. Keep business policies identifiable separately from their OR-Tools encoding so generation and replacements obey the same rules. Prefer small interfaces and ordinary Python models/functions to generic repository frameworks. The database switch should preserve the read contract, frontend, and calculations; migrations, imports, transactions, and concurrency checks remain real work even with those boundaries.

Preserve the policies currently expressed in `data/rules.yml`, ingredient constraints, and `planner/mealplanner/solver/solve.py`: exact weekly protein counts, per-meal protein adjacency, fish dinner limits, lunch carbs and no dinner carbs, carb frequency/adjacency, base-recipe usage limits with linked-pair counting, and lunch/dinner linkage. Cover these with representative cases during phase 2 extraction, phase 3 domain validation, and phase 5 replacements. Any deliberate rule change is separate from changing adapters.

Use Flask's application factory to wire routes, services, and adapters. Keep this as one application with clear domain and storage boundaries; microservices, event sourcing, a message bus, and separate read/write databases are outside scope. SQLite is a fit for this single-household workload, subject to verification on the chosen NAS. Store its file on a local persistent filesystem volume on the NAS, never on SMB/NFS. Keep state outside the container image and build directory.

## Domain and plan lifecycle

A **WeeklyPlan** is the consistency boundary for one household calendar week. It has one stable `plan_id` and one current accepted revision. A **PlanRevision** is an immutable full snapshot of that week's assignments and resolved recipe quantities. A **MealAssignment** is a dated lunch or dinner slot with stable `assignment_id`, selected recipe and variant, quantities, and optional reciprocal link to a same-day paired meal. A **Recipe** belongs to the independently managed catalog; saved assignments retain the recipe/quantity data needed to preserve their historical meaning. A **change proposal** is a preview based on a particular current revision; it does not modify the active plan until accepted.

Use Monday through Sunday calendar weeks, with actual `YYYY-MM-DD` meal dates interpreted in the household timezone (`America/Bogota` is the current example). There is at most one saved plan per household/week start, and each date/meal occurs once in a revision. An assignment date must be within the plan week and agree with its weekday. Assignment IDs stay stable across revisions. Weekends remain outside the cooking schedule unless the policy is deliberately changed. The current solver plans weekday lunches and dinners, uses exact weekly protein counts, and may link lunch and dinner to one base recipe.

Generation and edits follow these rules:

- An empty week can be created as revision 1. Opening or refreshing an existing week is read-only and never runs the solver.
- Generating for an existing week requires an explicit regeneration action. It creates a proposal and shows the full diff; it does not replace the current revision.
- Discard, solver failure, expiry, or an identical result creates no revision. Acceptance of a valid changed proposal appends one revision to the same plan.
- A proposal records its base revision and the recipe/rule input versions or snapshots used to build it. Acceptance verifies that basis and commits the previewed result; it does not rerun the solver and accept an unseen result.
- Each accepted replacement later creates one revision. If linked lunch/dinner assignments change, both changes validate and commit together. Undo appends a restoring revision rather than deleting history or decrementing the revision number.
- Mutation requests carry the expected revision. Stale proposals and concurrent writes are rejected for refresh/re-preview. Retry identifiers make a retried accepted request idempotent. Creating two plans for one week is prevented by a database uniqueness constraint.
- Full-week regeneration is initially limited to future weeks. Past meals stay fixed. Editing remaining meals in a started week needs an explicit policy for today's meals, locks, and weekly constraints.
- Manual choices become locks before later regeneration can alter them. A compatible replacement preserves unrelated assignments. If exact protein counts, repetition limits, adjacency rules, or a paired meal make a replacement infeasible, explain the conflict; broader rebalancing needs a separate preview and explicit acceptance.

Append the revision and move the plan's current-revision pointer in one database transaction. Do expensive solving outside the transaction, then check the base revision at commit. Store complete plan snapshots so ordinary reads do not load all history. Grocery checkmarks, opening a plan, and theme changes are not plan revisions. Catalog edits do not rewrite previously saved assignments.

Derive saved grocery totals from the accepted assignment snapshots and their captured pantry context, never from the latest mutable catalog or pantry on each read or publication. Pantry changes apply to subsequent calculations; refreshing groceries for an already accepted plan requires a separately specified workflow. A seed alone cannot reconstruct an accepted plan: preserve its actual assignments and calculation context.

A month is a view over current weekly plans, not another aggregate or solver horizon. Fetch every plan whose Monday–Sunday week overlaps the selected month, including a week crossing a month boundary. Weekly rules stay scoped to their week; there are no cross-week rules (decided). If the manifest has no entry for a month, the public site says “No published plan.” It does not claim that the month is unplanned on the private NAS.

A **ShoppingTrip** (shown to users as a “shopping list”) is a separate aggregate that answers “what do we buy to cover these weeks?” It holds a set of weekly plans, a status (`open` or `done`), and a `version` for optimistic concurrency. It never modifies a plan or creates a plan revision.

- **Items** are the sum, per ingredient and unit, of each covered week's saved grocery rows. They are derived, not stored or edited. There is no pantry tracking beyond what each weekly grocery list already captured, and no record of what was actually bought.
- **Open trip:** items are derived from each week's current revision. Weeks can be added and removed.
- **Done** (“shopping done”): the trip records the revision of each week it covered, and its items are frozen to those revisions. A done trip can be reopened (back to `open`) or deleted to undo a mistake.
- **Week coverage** is derived from trips, never stored on the plan: *not covered* (no trip), *on a list* (open trip), *covered* (done trip). A covered week whose current revision is newer than the covered revision shows a “changed since shopping” hint; there is no automatic top-up calculation.
- **Invariant:** a week belongs to at most one trip, whatever its status. Removing a week from a done trip requires reopening it first.
- Checkboxes on trip items are an in-store convenience only: device-local in both modes, never stored in SQLite, never exported, and never affect coverage.

## Identifiers and version meanings

Keep three tokens separate:

| Value | Changes when | Purpose |
| --- | --- | --- |
| `revision` | a changed plan is accepted | persistent plan-edit sequence and concurrency check |
| `snapshot_id` | normalized plan or its derived shopping payload changes, including its week anchor | pair a plan with exactly its matching groceries and detect changed fixtures |
| `publication_id` | a whole static publication/index is built | identify one coherent public export generation |

A read or publication does not increment a plan revision. A publication can contain several plan revisions. Re-publishing identical plan content may produce a new `publication_id` and timestamp while leaving each plan's revision and snapshot token unchanged. Keep opened static views pinned to one publication until refresh; never combine a plan file from one publication with groceries from another.

## Read API baseline (phase 1)

The reviewed API contract is [openapi.yaml](openapi.yaml), with [response examples](examples/) and [the validator](../../scripts/validate_api_contract.py). It defines exactly three read-only operations:

| Operation | Behavior |
| --- | --- |
| `GET /api/v1/plans?from=YYYY-MM-DD&to=YYYY-MM-DD` | Required inclusive ISO dates. Returns plans whose full Monday–Sunday week overlaps the range, ordered by `week_start`; `from` must not exceed `to`, and the inclusive range is at most 366 days. No matches return `{ "items": [] }`. |
| `GET /api/v1/plans/{plan_id}` | Returns the current plan snapshot; IDs are opaque to clients. |
| `GET /api/v1/plans/{plan_id}/shopping-list?revision=N&snapshot_id=TOKEN` | Requires both expected current tokens. Returns only matching current shopping data; historical reads are not available. |

Responses use snake_case JSON. A summary requires `plan_id`, `week_start`, inclusive Sunday `week_end`, `timezone`, `revision`, `snapshot_id`, `generated_at`, and `seed`. Plan detail adds dated slots and derived protein/carb counts. A slot has stable assignment ID, date/day, lunch/dinner, recipe and variant IDs/names, protein and nullable carb, raw ingredient quantities and units, ingredient rows with roles, and optional reciprocal link IDs. Protein amount means the raw protein ingredient quantity, not nutritional protein weight. Carb and carb quantity are both null when absent. Slots are date ordered, lunch before dinner. The schema does not make ten slots a permanent maximum; the present source fixture contains weekday meals.

Shopping responses carry the same plan ID, revision, snapshot token, and week dates. `sections` maps section names to ingredient rows with IDs, display names, quantities, units, section, and nullable notes. Units remain explicit (`g`, `ml`, or `units`); do not assume eggs are grams. Shopping values retain existing rounding and exclude the pantry ingredients captured for that calculation. There is no purchased/checkmark field in the API.

`snapshot_id` is opaque to clients and separate from `revision`. For the checked-in examples it is `sha256:` plus SHA-256 over canonical UTF-8 JSON of `{ "plan": <plan without snapshot_id>, "shopping_list": <shopping list without snapshot_id> }`, with recursively sorted object keys, compact separators, and Unicode preserved. Implementations may choose another opaque content-token algorithm, but unchanged normalized content keeps its token and a change to either paired payload or week anchor changes it. `generated_at` records source generation time; it does not determine `week_start`.

The example week is explicitly anchored at Monday `2026-10-19` with `plan_2026_10_19`, revision 1, and dated `slot_...` IDs. Its original April source timestamp is retained to demonstrate that timestamps do not select a week. Clients treat those IDs as opaque. Reads never generate or mutate a plan. The current API has no history route, writes, authentication scheme, or publication manifest schema.

Errors use `{ "error": { "code", "message", "details" } }`, with a details array of `{ "field", "message" }` and an empty array for general errors. `400 invalid_request` covers missing/malformed input or invalid ranges/identifiers; `404 plan_not_found` covers an unknown plan; `409 snapshot_changed` asks the client to refetch plan then groceries; `503 source_unavailable` and `503 source_invalid` represent inaccessible or incoherent source data. A malformed fixture is a server/source error, not a user validation error. Do not return filesystem paths, tracebacks, or internal configuration. API responses use `Cache-Control: no-store`.

The OpenAPI server example is `http://127.0.0.1:5000` with `security: []` for loopback development. That exception is not a deployment policy. Household access must be enforced before serving the API to other LAN devices. Add authentication and authorization contract details once the LAN access mechanism is selected; never trust a client-supplied household identity. The publication contract below references those same DTOs; static routes are not additional API operations.

## Public publication contract

The publisher reads selected **current** plans and their captured grocery totals from a coherent SQLite read transaction. Capture the selected months, overlapping full weeks, timezone, and pantry inputs/versions used to derive those totals; pantry context stays private. Validate the entire output bundle before deployment. Do not hold a database transaction open while serializing large output or waiting on GitHub/network operations. A failure before deployment leaves the prior publication active and does not alter saved plans. If deployment confirmation is lost or times out, mark the attempt as pending/unknown and reconcile with GitHub before claiming success or failure.

The machine contract is [publication.schema.json](publication.schema.json), JSON Schema Draft 2020-12. Its `Index` and `Manifest` definitions use `schema_version: 1`; unsupported versions are errors. `Manifest` references `PlanSummary` directly from OpenAPI. Plan and shopping files validate against OpenAPI's existing `Plan` and `ShoppingList` schemas, with no separately maintained DTO copies. Resolve these references locally during validation/build, not through browser requests for YAML schemas.

The data directory has this fixed layout; on the static site it lives beneath the frontend's base URL at `publication/`:

```text
publication/
  index.json
  publications/<publication_id>/
    manifest.json
    plans/<plan_id>/plan.json
    plans/<plan_id>/shopping-list.json
    shopping-trips/<trip_id>.json        # phase 4
```

The index contains `schema_version`, `publication_id`, and `manifest_path`, exactly `publications/<publication_id>/manifest.json` relative to the index. The manifest contains `schema_version`, the same `publication_id`, `exported_at`, sorted unique `selected_months` in `YYYY-MM` form, one household IANA `timezone`, and `plans`. Each entry contains `summary`, `plan_path`, and `shopping_path`. The paths are exactly `plans/<plan_id>/plan.json` and `plans/<plan_id>/shopping-list.json` relative to the manifest. IDs use the safe ASCII identifier alphabet in the schemas; consumers still treat their meaning as opaque. Forbid absolute/external URLs, parent traversal, percent escapes, backslashes, query strings, fragments, and paths escaping the bundle. Resolve against each containing file's directory and preserve the Pages repository subpath.

The manifest also has a required `shopping_trips` array (empty allowed) of `{ "trip_id", "path" }`, sorted by unique `trip_id`, with `path` exactly `shopping-trips/<trip_id>.json` relative to the manifest. Each file validates against OpenAPI's `ShoppingTrip` schema (no copied DTO) and its `trip_id` equals the entry's. Coherence: every week of a published trip has an entry in `plans` with the same `plan_id` and dates; for an open trip each week's `revision` and `snapshot_id` equal the published plan's; for a done trip the week's `revision` is at most the published plan's (items are frozen to the covered revision); a plan is on at most one published trip; and when the trip's revisions equal the published ones its `items` must equal the sum of the published shopping lists. A trip whose weeks are not all in the bundle is not published. Unreferenced trip files fail validation like any other.

Entries are sorted by `week_start`, have unique plan IDs and week starts, and include only full weeks overlapping at least one selected month. Each summary must exactly match its plan metadata; plan and grocery identities, week dates, revision, and snapshot token must agree. Every referenced file must exist. Unknown fields and unreferenced data files fail bundle validation. A selected month may have no saved plans, so `plans: []` is valid; no placeholder plan is synthesized. The exporter must check completeness against the selected database weeks: a public bundle alone cannot prove whether private plans were omitted. The [populated example](static-examples/populated/index.json) includes October 19–25 and October 26–November 1; the [empty example](static-examples/empty/index.json) selects December with no plans. These are prepared fixtures, not exporter output.

For phase 2 static preview, mount or copy the contents of one fixture root (`static-examples/populated/` or `static-examples/empty/`) beneath the frontend's `publication/` directory. The fixture's `index.json` then resolves at the configured `publication/index.json` path; the two examples are alternative bundles, not sibling publications in one deployment.

`exported_at` is the time artifact generation was finalized, before upload. The manifest cannot know whether a later upload and Pages deployment will succeed. After GitHub Pages confirms success, store `deployed_at` with the home publication job/status; do not write a success timestamp back into the artifact that was deployed. On the public site label the time as “Snapshot as of” `exported_at`. At home show unpublished revisions, failed attempts, and the last successfully confirmed publication time. Pantry context and publication-job records are private and absent from this schema.

Publish to a separate repository used only for approved public artifacts. Use immutable, publication-scoped directories for the manifest's referenced files, with a small current index pointing at the active publication. One completed publication is one validated bundle/deployment: upload the index and all referenced files together so a partial build never makes a mixed plan/grocery set active. Retain the latest three successfully deployed publication directories by default, including the active one; removal occurs only as part of a subsequent successful deployment. This count is a retention policy, not a guaranteed lifetime for an open tab. After expiry, show an expired-publication/reload message rather than silently pairing versions. Phase 6 validates retained bundles separately before assembling a deployment; phase 1's bundle validator accepts one candidate publication, not arbitrary extra archive or frontend files.

Versioned paths allow stable caching for payload files. On startup or explicit refresh, request the index with browser cache bypass/revalidation and a fresh query nonce; resolve only the paths from that fetched index. Pin the manifest and all subsequent reads to its publication ID. Load and validate the visible plan and matching groceries before replacing the displayed state together. If refresh fails, keep any already displayed coherent data with a clear refresh error; do not label it fresh. Navigation within the pinned publication never silently switches to a newer one. GitHub Pages cannot set the NAS API's `no-store` headers, so a stale but coherent publication remains a possible result. The displayed export time makes its age visible; live cache behavior is a phase 6 deployment check.

Initial publication is a manual action with explicit month selection and a preview of every included week. Publishing a selected month includes complete overlapping plan weeks, even where meal dates fall outside the selected month; show that scope before approval. Saving, generating, or editing at home never auto-publishes. A plan revision does not change until accepted at home. Starting, retrying, or completing publication never creates a plan revision. A changed revision becomes public only in a later successful publication.

At home, show the current local state separately from publication status: which selected revisions are unpublished, the latest failed attempt, and the last successful deployment confirmed by GitHub Pages. A read/build/push failure is not “published.” Do not claim success until GitHub Pages confirms deployment. On the public site show “Snapshot as of” the manifest's `exported_at` and its selected months. It reports only what it can know from the published artifact; it cannot know whether the NAS has newer revisions or whether a later publish is queued.

Serialize publication jobs so an older export cannot finish later and replace a newer one. Record the exact exported plan/revision/snapshot tokens; edits made during upload remain unpublished even after that upload succeeds. Define retention and removal before launch: removing a month from the active index does not erase retained files, repository history, browser caches, or copies already downloaded. The preview must make that public-sharing consequence clear.

The publisher credential stays on the app server or another controlled outbound publishing process, with permission limited to the publication repository. Never ship credentials or tokens to the browser or public repository. Do not run arbitrary public-repository workflows on the NAS. A hosted runner can be considered only if it receives vetted publication artifacts and no inbound job can execute arbitrary work on the NAS.

## Frontend reader contract

[frontend-config.schema.json](frontend-config.schema.json) permits exactly one of the following build/startup configurations; it rejects mixed modes, extra keys, and arbitrary API URLs:

```json
{ "mode": "api", "api_base_path": "/api/v1" }
```

```json
{ "mode": "static", "publication_index_path": "publication/index.json" }
```

Resolve the static index against Vite's configured application base URL, not a client-side navigation route. Mode selection is controlled by the deployment build, never URL parameters or local storage. A Pages build must reject API mode. Both modes initially expose `canEditPlans: false`; phase 3 adds API-only write services and UI capabilities without adding mutation methods to the reader. Server authorization remains mandatory regardless of those flags.

| Read | Shared result and semantics |
| --- | --- |
| `listPlans({from, to})` | A `PlanList` (`{items: PlanSummary[]}`), sorted by week start. Both adapters validate real inclusive dates, `from <= to`, and at most 366 days. Filter by full-week overlap. Static results contain only entries from the pinned publication. |
| `getPlan(planId)` | A `Plan` for the API's current revision or the static publication's pinned revision. Missing identities return `plan_not_found`. |
| `getShoppingList(planId, revision, snapshotId)` | A `ShoppingList` matching all three tokens. A mismatch returns `snapshot_changed` and never displays the wrong grocery totals. |

Phase 4 adds `listShoppingTrips()` and `getShoppingTrip(tripId)` to both readers, returning the same trip DTO (status, covered weeks with revisions, items). In static mode they read only trips in the pinned publication. Trip writes are API-only write services, like plan edits, never reader methods.

These are asynchronous reads. Common application errors carry `code`, `message`, and `details`, reusing API error codes where applicable; static transport errors are not fabricated HTTP responses. `invalid_request` covers invalid arguments, `source_unavailable` covers transport failures, and `source_invalid` covers malformed schemas, unsupported versions, bad paths, or incoherent metadata. A missing referenced static file is an invalid/incomplete publication, not an unknown plan. If a previously loaded pinned publication has been removed, use `publication_expired` and offer explicit refresh. API `409` maps to `snapshot_changed`; refetch the plan and its groceries together with bounded retries. Static mismatches fail the read and offer refresh rather than fetching from another publication or from the NAS.

Keep source-specific context alongside the reader: API mode, or static `publication_id`, `exported_at`, and `selected_months`. It drives freshness labels and “No published plan” states, without adding publication fields to shared plan DTOs. Selected-month navigation uses that context; an overlapping week does not automatically publish navigation for every adjacent month. Browser behavior and error mapping are phase 2 implementation checks; schema validation alone does not prove them.

## Grocery checkmarks by mode

In phase 2, preserve current browser-local checkmarks in API mode. Scope every local check to source mode, `plan_id`, revision, `snapshot_id`, and ingredient ID so a changed fixture, plan, or publication cannot inherit an unrelated check. In static mode, use optional browser/device-local checks as the default to preserve today's interaction; label that they do not sync between people/devices and are never exported or written back to the NAS. Static totals remain read-only.

In phase 4, shared household state is the shopping trip (see “Domain and plan lifecycle”), not per-ingredient purchased state. Grocery and trip-item checkmarks stay device-local in both modes, are never written to the database or exported, and do not create plan revisions or force republishing. There is no purchased-quantity tracking or reopen/adjustment rule; a plan edited after shopping only shows a “changed since shopping” hint on the covered week.

## Delivery roadmap and exit criteria

### Phase 1 — Domain, API, and static publication contracts (complete)

**Completed baseline:** domain vocabulary and weekly invariants; read-only OpenAPI 3.1 contract; dated examples transformed from current plan/grocery files; local schema, example, and semantic validator.

**Completed static addendum:** publication index/manifest and exclusive frontend mode schemas; shared API DTO references; populated, cross-month, and empty examples; path confinement, matching plan/grocery identity, and complete-reference validation. This document defines publication status, retention, refresh/pinning, read methods, error mapping, and the public output allowlist. Static files are not added as OpenAPI operations.

**Exit met:** API and publication contracts are reviewed; local validators pass and reject incoherent/mixed artifacts. Browser fetching, build-mode enforcement, and atomic visible-state refresh remain phase 2 implementation checks. Authentication, exporting from SQLite, and live publication remain later-phase work.

### Phase 2 — Local app, both frontend read modes, and storage boundaries (complete)

- Implement the same-origin Flask `create_app` read API with the JSON snapshot reader, using the phase 1 API contract and explicit dated fixture anchor. Validate plan, shopping, links, derived counts, and captured pantry-adjusted quantities before exposing one coherent snapshot. A reload error must not mix files from different generations.
- Replace the legacy direct-file-only frontend data access with the React read adapter interface and shared UI model. Select `api` or `static` explicitly through trusted build/configuration. Implement HTTP and static-file readers; no fallback, mutation, browser generation, or historical revision support. The NAS app uses same-origin API mode; the separately built Pages site uses static mode only. Static mode reads the prepared phase 1 fixture bundle; automated NAS publication is phase 6.
- Keep both server storage boundaries: YAML planner-input reader and JSON plan-snapshot reader. Extract generation/shopping services so the CLI passes explicit validated data and retains its current calculations and output. The API reads; it does not run the solver.
- Preserve weekly cards, recipe details, statistics, theme, and local checkmarks. Add loading, missing-plan, stale-snapshot, source-error, and empty states. Confirm shopping metadata before displaying totals with a plan.
- Expose capability flags from trusted mode/configuration for UI actions. Enforce authorization on every server operation once reachable to LAN; the loopback-only unauthenticated development exception remains confined to localhost.
- Document one local startup workflow for the API and frontend plus a static-mode preview. Anchor the legacy fixture to an explicitly configured Monday, never the request date or generation timestamp; an explicit reload/restart is sufficient for changing fixtures in this milestone.
- During frontend development, proxy same-origin `/api` requests from Vite to loopback Flask, or serve the built frontend through Flask. Avoid requiring permissive CORS. Static-mode paths must also work under a GitHub Pages repository subpath.

**Exit:** the same frontend views work against both API mode and a prepared static bundle. API/JSON examples and static DTOs render the same plan and grocery information. No automatic fallback occurs. CLI results, quantities, statistics, and solver behavior match before refactoring for representative inputs and the same solver configuration. Read services can be exercised without Flask context; solver services need no YAML/filesystem access. Contract, mode, and integration checks pass.

### Phase 3 — Saved weeks and home editing (complete)

**3a: domain and application workflows.** Implement plan creation, regeneration proposal, discard, acceptance, no-op, stale-base rejection, idempotent retries, and started-week policy independently of Flask/SQLite. Keep linked changes atomic and preserve complete saved assignment data. Test lifecycle transitions through application services.

**3b: SQLite.** Add migrations for catalog/planner inputs, plans, dated assignments, recipe/version snapshots, and revisions. Import YAML and the configured fixture with validation and an explicit repeatable conflict policy; repeated startup/import must not overwrite household edits. SQLite becomes authoritative; YAML is import/export. Use a local persistent volume, including when later deployed to the NAS. Add planner-input and plan-read SQLite adapters behind the phase 2 interfaces. Enforce unique household/week and per-revision date/meal keys, immutable revision snapshots, foreign keys, transactions, and atomic current-revision updates. Persist retry identifiers atomically with mutations. Compare file and database adapters on shared inputs and equivalent plan/grocery outputs. Verify races, stale commits, retries, rollback, restart, and backup restore.

**3c: API and home UI.** Extend OpenAPI before adding create/generate/preview/accept/history actions. Routes call tested workflows and map domain errors. Add week selection, explicit generation, proposal diff, accept/discard, and conflict refresh. Bound solver runtime and concurrent generation on the server. Preserve history now; a history browser may follow later. Mutations are API-mode/home-only. Static mode remains read-only.

**Exit:** home users create and revisit saved weeks after restart, preview and discard without changing current state, and accept revisions without losing their predecessors. Stale/duplicate requests and failed writes cannot silently overwrite or partially commit a plan. SQLite imports preserve the phase 2 read shapes and calculations, and a tested independent backup restores locally.

### Phase 4 — Monthly views, shopping trips, and a minimal exporter

**4a: shopping trips (domain, storage, API).** Add the `ShoppingTrip` aggregate and its invariants (one trip per week; frozen covered revisions on done; derived items and coverage) in the domain, a `ShoppingTripRepository` port with SQLite and in-memory adapters, and a migration. Extend OpenAPI before adding routes: list/get trips, create, add/remove week, mark done, reopen, delete, each carrying the expected trip `version` (stale → `409`), plus week coverage on plan summaries or a coverage read. Writes stay loopback/home-only like phase 3.

**4b: minimal manual exporter.** Pulled forward from phase 6a so the public site can show what to buy. `python -m mealplanner.entrypoints.db_cli export --months 2026-10,2026-11 --out <dir>` reads selected current plans, their groceries, and shopping trips in one read transaction, writes a new immutable `publications/<publication_id>/` directory, validates the whole bundle against `publication.schema.json`, and only then replaces `index.json`. A trip is published only when every week it covers is in the bundle; others are reported and skipped. Publishing is manual: the household copies the bundle to the Pages repository and pushes. Extend the publication contract with `shopping-trips/<trip_id>.json` entries in the manifest (status, covered weeks and revisions, frozen or current items). No credentials, automation, or retention cleanup yet.

**4c: frontend.** Add month/year navigation to both modes. API mode queries overlapping current weeks; static mode shows only months in the active publication and “No published plan” for absent months (absence from the public site does not assert absence on the NAS). A month intersecting up to six weeks displays the same underlying weekly plans and revisions; a week crossing a month boundary appears in both months. Show week coverage badges. API mode adds create/edit/done/reopen/delete for trips. Static mode adds a read-only, phone-friendly shopping trip view with device-local checkboxes.

Keep weekly shopping totals per week. A trip's items are a plain sum of its weeks' saved groceries; there are no monthly pantry or purchased-state rules. Keep weekends visibly outside the cooking schedule. Catalog editing is deferred.

**Exit:** month and year boundaries work in API and static mode, including weeks crossing month boundaries. Home users create a trip from several weeks, see what to buy, mark it done, and see those weeks as covered after reload; a week cannot be on two trips; stale trip edits are rejected. An exported bundle validates, shows the published trips on the static site, and a failed export leaves the previous `index.json` active.

### Phase 5 — Safe recipe replacements

Initially, “swap” means replacing a recipe/variant in a dated slot. Exchanging two scheduled meals is a separate future action.

Define alternatives and replacement preview/accept routes before implementation. Validate recipe/variant eligibility, exact protein distribution, repetition limits, and applicable adjacency rules. Preserve unrelated assignments for the first replacement flow. For linked meals, preview all affected assignments and accept or reject them together. Recompute statistics and groceries as part of the same plan revision. Reject stale previews. Add explicit locks for manually chosen meals before offering regeneration that can affect them. Undo appends a new restoring revision.

**Exit:** compatible replacements update the saved revision and groceries; invalid or stale replacements leave it intact; paired changes are atomic; undo cannot erase a later edit. Broader “rebalance this week” is a separately previewed full diff requiring explicit acceptance.

### Phase 6 — NAS operation and public static publishing

**6a: exporter and validation.** Harden the phase 4 manual exporter: add an explicit month-selection preview and an export service over the current read interfaces. Read a coherent set of current plans/groceries, including the pantry context used for totals, in a bounded read transaction. Produce immutable publication-scoped plan/grocery files and the reviewed manifest/index; validate every schema, relative path, ID, revision, token, date range, and referenced file before upload. Separate `publication_id` from plan revisions and keep exported files allowlisted. Test failure at each stage leaves the old publication active. Do not put DB files, planner inputs, history, drafts, local grocery checks, environment files, or credentials in the artifact.

**6b: NAS deployment and recovery.** Confirm NAS model, CPU, memory, supported container/runtime setup, local volume behavior, and measured solver runtime before choosing deployment resources. Bind the Flask app to the home LAN only after household authentication/authorization is implemented. Package reproducibly with a production application server, app-only data mounts, restart/health checks, and bounded logs. Choose LAN HTTPS/certificate and session handling before deploying login. Keep app state in a persistent local filesystem volume, independent of the container image. Test SQLite-consistent backups copied somewhere independent of the NAS and restore them to a clean environment. A public export is a convenience copy, not a backup. Review actual hosting, power/storage, domain, transfer, and any service costs before launch; use free-only tooling with paid runners/overages disabled unless separately accepted. Do not make provider pricing or resource guarantees without checking the current plan and hardware.

**6c: outbound Pages publishing.** From the NAS or another controlled outbound process, push only a validated bundle to the separate public publication repository. Require successful GitHub Pages deployment before showing a publication as live. Keep old immutable files for the chosen retention window. Test public cache behavior and refresh strategy from a browser outside the LAN. Confirm that no port forward, tunnel, CORS trust, NAS admin endpoint, or inbound hosted-runner job exposes the app. The public site has no API connection back to the NAS.

**Exit:** household devices can reach the app only through the intended LAN access policy, and unauthorized requests cannot read household data or mutate plans. The NAS can publish explicitly selected months outbound; a failed publish leaves the previous public version intact. A public visitor can browse only approved read-only artifacts and can refresh to one coherent publication. Backup/restore, app restart, schema migration, update/rollback, actual cost assumptions, credential scope, and removal/expiry of old publications are documented and exercised.

Deployment may move earlier, after phase 3, if household testing on the NAS is useful. The same access, resource, backup/restore, and cost checks still apply. Pages publication remains after the exporter and publication contract; moving the home app does not grant remote app access or auto-publish home edits.

## Access and operating constraints

- Local development binds to loopback and may use the contract's temporary `security: []` setting. Household app authentication and server-side authorization are required before any LAN deployment, for reads and writes. Server-side authorization is the boundary; hiding controls in React is not.
- If authentication uses cookies, protect state-changing routes against CSRF. Do not use permissive CORS as authentication. No public registration or multiple-household support is planned.
- Do not expose NAS management interfaces. Keep the app/API private to the home LAN; do not add inbound internet routes or tunnel access under this plan.
- Keep publication credentials server-side and narrowly scoped to the public output repository. Treat all static output as public and inspect the completed bundle before publishing.
- Keep independent SQLite backup/restore procedures. GitHub Pages history and exported plan JSON are not a recovery strategy.
- At deployment time, measure on the actual NAS and verify current software/provider plans and costs. Avoid paid hosted runners, automatic upgrades that break restore paths, autoscaling, and optional metered services unless their cost and purpose are reviewed.

## Local development environment (WSL/Linux)

Use the Linux project directory (`/home/julopez/dev/meal-prep`), Python 3.12 with its `venv` support, and Linux Node.js 22. The repository's [.nvmrc](../../.nvmrc) selects Node 22; the setup script requires at least 22.12. Use Linux Node/npm inside WSL so Windows executables do not enter the build path. Python dependencies live in the repository's ignored `.venv/`, frontend dependencies in `site/node_modules/`, and download caches in `.cache/`.

On this Ubuntu/WSL machine, install the missing system prerequisites from the user's terminal:

```sh
sudo apt-get update
sudo apt-get install -y python3-venv curl git
```

Install nvm if it is not already available, using its [official installation instructions](https://github.com/nvm-sh/nvm#installing-and-updating). The verified installer version for this setup is `v0.40.8`:

```sh
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh -o /tmp/meal-prep-install-nvm.sh
bash /tmp/meal-prep-install-nvm.sh
```

Open a new WSL terminal so nvm is loaded, then run:

```sh
cd /home/julopez/dev/meal-prep
nvm install
nvm use
bash scripts/setup-local.sh
```

[setup-local.sh](../../scripts/setup-local.sh) checks the Linux runtime, creates or repairs `.venv`, installs the planner with API/development extras and contract tools, installs frontend dependencies with `npm ci`, then verifies the dependency installation. It does not install system packages or deploy anything. Run `bash scripts/setup-local.sh --check` later to check the installed environment without installing packages. Setup completion is distinct from application validation; run the application and contract checks below.

The project dependencies are now installed and usable by the agent. The agent sandbox cannot resolve package-registry hosts or open listening sockets, so package installation and the live browser preview run in the user’s WSL terminal. Node installed through nvm is loaded by the setup script even in a non-interactive shell.

### Run Phase 2 locally

From the repository root, start Flask in one terminal:

```sh
.venv/bin/python -m mealplanner.entrypoints.http.app
```

In another WSL terminal:

```sh
cd /home/julopez/dev/meal-prep
nvm use
cd site
npm run dev:api
```

Open `http://127.0.0.1:5173/meal-prep/`. Flask listens on `127.0.0.1:5000`; Vite proxies same-origin `/api` requests to it. Both are local development servers. The API captures and validates `site/public/plan.json`, `site/public/shopping_list.json`, and `data/pantry.yml` once at startup. After CLI generation or pantry changes, restart Flask and refresh the browser.

For the separate static sample, Flask is unnecessary:

```sh
cd /home/julopez/dev/meal-prep/site
npm run dev:static -- --port 5174
```

### Run Phase 3 locally

Plan changes (create, preview/accept regeneration, undo) need the SQLite store. Import once, then start Flask against it:

```sh
cd /home/julopez/dev/meal-prep
.venv/bin/python -m mealplanner.entrypoints.db_cli import --fixture   # inputs + the sample week into var/mealplanner.db
MEALPLANNER_DB=var/mealplanner.db .venv/bin/python -m mealplanner.entrypoints.http.app
```

`POST /api/v1/months/YYYY-MM/fill` (body `{}`) creates the missing weekly plans for every week overlapping that month, one independent plan per week; it skips past weeks, never overwrites existing ones, and reports per-week `created | exists | skipped_past | failed`.

Then start the frontend as above (`npm run dev:api` in `site/`). `GET /api/v1/capabilities` reports `can_edit_plans`, which is true only for loopback clients of a SQLite-backed API. It also reports `can_edit_shopping_trips` (same condition). Shopping trip routes live under `/api/v1/shopping-trips` (writes gated like plan writes; the expected trip `version` goes in the JSON body, or in the `expected_version` query parameter on DELETE; a mismatch is `409 stale_version`), and `GET /api/v1/coverage?from=&to=` reports per-week coverage. Without `MEALPLANNER_DB` the API serves the JSON snapshot and rejects writes with `403 read_only`; non-loopback writes get `403 forbidden`. Optional settings: `MEALPLANNER_TIMEZONE` (default `America/Bogota`, also the clock for "future week" checks), `MEALPLANNER_SOLVER_TIMEOUT_SECONDS` (default 30) and `MEALPLANNER_MAX_CONCURRENT_GENERATIONS` (default 1; extra requests get `503 generation_busy` with `Retry-After`).

Open `http://127.0.0.1:5174/meal-prep/`. This uses the checked-in populated publication bundle, not newly generated CLI files. `VITE_STATIC_BUNDLE=empty npm run dev:static -- --port 5174` exercises the empty publication. Neither mode offers generation, persisted edits, or swaps yet; grocery checkmarks remain browser-local.

The default view is explicitly October 19–25, 2026. To anchor API data to another Monday, set `MEALPLANNER_WEEK_START=YYYY-MM-DD` before starting Flask and set matching `VITE_DEFAULT_FROM=YYYY-MM-DD VITE_DEFAULT_TO=YYYY-MM-DD` before starting Vite. The API timezone defaults to `America/Bogota` and accepts `MEALPLANNER_TIMEZONE`. Static sample dates remain those in their manifests. Date navigation is a later milestone.

Check both modes for weekly cards, recipe details, statistics, theme, grocery checkmarks, and refresh. Stop Flask to check the API error state; static mode should continue working independently. A failed refresh should retain the last complete plan and grocery view. Checkmarks are scoped to mode and snapshot identity.

### Run Phase 4 (manual export) locally

If the HTTP server answers `503 source_unavailable` for a SQLite database, the file is probably unmigrated (the API never migrates a database it only reads). Apply pending migrations to an existing database, then export:

```sh
.venv/bin/python -m mealplanner.entrypoints.db_cli migrate [--db var/mealplanner.db]
.venv/bin/python -m mealplanner.entrypoints.db_cli export --months 2026-10,2026-11 --out /tmp/publication [--db var/mealplanner.db] [--timezone America/Bogota]
```

`migrate` refuses a missing file (use `import` to create a database) and is idempotent. `export` never writes to the database. It publishes the current revision of every plan whose Monday-Sunday week overlaps a selected month, plus every shopping trip whose weeks are all in that set (others are listed as skipped with the reason), into `<out>/publications/<publication_id>/`, validates the written bundle (including `scripts/validate_publication_contract.py`; `--skip-schema-validation` skips only that JSON-schema pass), and only then replaces `<out>/index.json`. A failure before that last step leaves the previous `index.json` untouched and removes the new directory. Earlier publication directories in `--out` are kept (no retention cleanup yet).

Reads are not one SQLite transaction (plans, groceries and trips come through separate repositories); the exporter checks that they agree and re-reads up to three times if the data changed mid-export, then fails. Publishing stays manual: copy the contents of `<out>` into the Pages site's `publication/` directory (index last) and push. Old publication directories you no longer want must be removed by hand for now, and the validator only accepts one active publication, so validate the Pages directory only after pruning to the active one.

### Phase 2 checks and build scope

After setup, run from the repository root:

```sh
.venv/bin/python -m pytest planner/tests -q
.venv/bin/python scripts/validate_api_contract.py --require-openapi-validator --self-test
.venv/bin/python scripts/validate_publication_contract.py --self-test
cd site
npm test
npm run lint
npm run build:api
npm run build:pages
../.venv/bin/python ../scripts/validate_publication_contract.py --bundle dist/publication
```

Both builds write `site/dist/`, replacing the previous build. API builds contain no publication or legacy raw plan JSON. Pages builds include only the prepared public sample plus frontend assets. Bare `npm run dev`/`npm run build` deliberately require an explicit mode. Existing GitHub Generate Plan/Build Frontend workflows still use the old generic build and will stop on this branch; do not change them to publish the static sample as though it were newly generated household data. The real exporter and publication workflow belong to phase 6. Existing deployed content is unaffected by local work.

The dependency installation reported 22 npm vulnerabilities. The dependency versions and lockfile were not changed by Phase 2; remediation needs the audit details and is separate from validating the migration. Avoid automatic breaking upgrades while that report is being reviewed.

## Validation and working references

From the repository root, install the contract validation dependencies in an isolated virtual environment and run the strict local checks:

```sh
python3 -m venv .venv-contract
.venv-contract/bin/python -m pip install -r scripts/requirements-contract.txt
.venv-contract/bin/python scripts/validate_api_contract.py --require-openapi-validator --self-test
.venv-contract/bin/python scripts/validate_publication_contract.py --self-test
```

The API validator checks duplicate YAML keys, the three read-only operations, parameters/headers, local references, Draft 2020-12 component schemas, response examples, source-fixture normalization, snapshot hashing, dates/links/derived counts, and pantry-adjusted shopping totals. Self-checks mutate examples to ensure targeted failures are detected. If `openapi-spec-validator` is unavailable, the default run warns; the strict flag fails unless a complete document-schema alternative is supplied with `--openapi-schema PATH`. A separately supplied OpenAPI 3.1 JSON Schema meta-schema checks the document structure; this is distinct from validating the contract's Schema Objects and examples. No temporary downloaded meta-schema is required for the normal package-based command.

[The publication validator](../../scripts/validate_publication_contract.py) checks both schemas, both mode examples, and the populated/empty bundles offline. It reuses OpenAPI's DTOs and shared plan semantics, then checks paths, complete references, dates/month overlap, matching summaries and plan/grocery tokens, and the public-file allowlist. Use `--bundle PATH` to check another single candidate data bundle whose root contains `index.json`. Its self-tests deliberately corrupt copied bundles/configurations and require the expected diagnostic. Public validation can verify totals of included grocery ingredients, but cannot prove excluded pantry items are correct without private captured inputs; those checks belong to the private exporter and the API fixture validation. Neither validator implements or tests browser fetching, authentication, deployment, or the future exporter.

Verification passes for both static bundles, 17 targeted bundle mutations, invalid configuration/JSON checks, and API fixture/self-tests. Full OpenAPI document validation now uses the installed `openapi-spec-validator` package. All 24 backend tests pass. Backend HTTP checks use Flask’s test client; frontend adapter tests use Node. An integration check also fed actual Flask test-client responses through the JavaScript API reader for list, plan, groceries, empty-range, and stale-snapshot requests. CLI output matches the original implementation for seeds 5 and 42 with the same installed solver, excluding only the generation timestamp.

Useful references: [Flask application factories](https://flask.palletsprojects.com/en/stable/patterns/appfactories/) and [Flask design decisions](https://flask.palletsprojects.com/en/stable/design/). GitHub Pages is a public static host in this design; review its [usage limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits) and [site visibility behavior](https://docs.github.com/en/enterprise-cloud@latest/pages/getting-started-with-github-pages/changing-the-visibility-of-your-github-pages-site) before publication. Confirm current GitHub plan/visibility options at deployment time.

## Decisions and defaults

| Topic | Current decision | Revisit by |
| --- | --- | --- |
| Authoritative app/data | Flask + SQLite on home NAS local persistent storage | verify hardware before phase 6 |
| Household access | Home LAN only; no inbound public route or tunnel | phase 2/6 |
| Public sharing | Separate public GitHub Pages repository with approved read-only artifacts | implement and verify in phase 6 |
| Frontend | One React app, explicit API/static reader mode, shared DTOs/components | implement in phase 2 |
| Week/timezone | Monday–Sunday; default `America/Bogota`; weekday cooking | configure before importing saved weeks in phase 3 |
| Revisions | One stable plan per week; append-only accepted full-plan revisions | phase 3 |
| Static publication | Manual month selection; immutable bundle; retain latest three successful publications. Minimal manual exporter (bundle + manual push) in phase 4; automated outbound publishing in phase 6 | phase 4 / 6 |
| Static checkmarks | Device-local and never exported/written back | phase 2 |
| Home checkmarks | Device-local only; shared household state is the shopping trip, not purchased quantities (decided) | — |
| Shopping trips | Aggregate covering a set of weeks; open/done; items = plain sum of weekly groceries; no pantry or purchase tracking; one trip per week; published to the static site | phase 4 |
| Cross-week rules | None (decided) | — |
| Month generation | Batch of independent weekly plans; no monthly or cross-week rules; existing weeks never overwritten | — |
| Replacement | Preserve unrelated meals; explain infeasibility; candidates must satisfy `domain/weekly_rules.py` (same specifications as generation, solver-independent) | phase 5 |
| Costs | Verify NAS, power, storage, GitHub plan, domain, transfer, and tooling at launch | before phase 6 |
