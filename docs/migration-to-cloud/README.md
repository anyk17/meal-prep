# Household meal planner: NAS app and public read-only calendar

Code layout and dependency rules: see [docs/ARCHITECTURE.md](../ARCHITECTURE.md).

**Status:** Phases 1–5, 6a (container image), 6b (Publish button) and 6c (household login) are complete on `feature/cloud-migration`. Saved weeks live in SQLite with append-only revisions; home users create, regenerate, accept/discard, undo, and fill months in API mode. Month/year navigation works in both modes. Shopping trips cover sets of weeks (open → done) and show what to buy; the manual exporter publishes plans and trips to `publication/`, which the Pages build serves read-only. Weekly rules are domain specifications; the OR-Tools solver is an adapter behind a port (see [ARCHITECTURE.md](../ARCHITECTURE.md)). Home users can also swap any today-or-later meal to any catalog recipe (rules are advisory for swaps). The home app publishes selected months to https://jflopezo91.github.io/meal-plan/ (first live publish 2026-10-21). Viewing is open on the home network; changes and publishing need a household login (users are created with `db_cli user add`). Next: 6d go-live on the UGREEN DXP2800 NAS.

## Starting point and first milestone

Today, `planner/mealplanner/entrypoints/cli.py` loads YAML inputs and runs OR-Tools to generate JSON output. `planner/mealplanner/render.py` (since split into `application/plan_output.py` for construction and `adapters/files.py` for file writes) combined response construction with file writes. `site/src/App.jsx` fetches `plan.json` and `shopping_list.json` directly; there is no Flask API or database. Plans identify weekday/meal slots without calendar dates, and grocery checks live in the browser.

The first delivery milestone combines phases 1 and 2: preserve today's weekly viewer while introducing dates, a local Flask read API backed by the current files, and interchangeable API/static frontend readers. Browser generation and persistence arrive in phase 3; the month view arrives in phase 4. Dates and stable identities belong in the first contract so those later features can evolve without replacing it.

## Goal and operating model

Run the household's authoritative meal-planning app on a NAS inside the home network. The app uses Flask, SQLite, and one React frontend served from the same origin as its API. Household members use it on the home LAN with open viewing and a household login for every change; server-side authorization is the boundary. The NAS makes outbound requests to publish an explicitly selected, read-only calendar to a separate public GitHub Pages repository. There are no inbound public NAS ports, tunnels, or remote app sessions in this design. GitHub Pages never calls the NAS, runs the solver, receives app writes, or contains the authoritative database.

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
- A manual swap preserves unrelated assignments and is not blocked by weekly rules (they are reported as warnings; see phase 5). Regeneration replaces the whole week; there are no locks for now.

Append the revision and move the plan's current-revision pointer in one database transaction. Do expensive solving outside the transaction, then check the base revision at commit. Store complete plan snapshots so ordinary reads do not load all history. Grocery checkmarks, opening a plan, and theme changes are not plan revisions. Catalog edits do not rewrite previously saved assignments.

Derive saved grocery totals from the accepted assignment snapshots and their captured pantry context, never from the latest mutable catalog or pantry on each read or publication. Pantry changes apply to subsequent calculations; refreshing groceries for an already accepted plan requires a separately specified workflow. A seed alone cannot reconstruct an accepted plan: preserve its actual assignments and calculation context.

A month is a view over current weekly plans, not another aggregate or solver horizon. Fetch every plan whose Monday–Sunday week overlaps the selected month, including a week crossing a month boundary. Weekly rules stay scoped to their week; there are no cross-week rules (decided). If the manifest has no entry for a month, the public site says “No published plan.” It does not claim that the month is unplanned on the private NAS.

A **ShoppingTrip** (shown to users as a “shopping list”) is a separate aggregate that answers “what do we buy to cover these weeks?” It holds a set of weekly plans, a status (`open` or `done`), and a `version` for optimistic concurrency. Its name is the one the user typed, or, when left blank, derived from its current weeks (it follows weeks being added or removed). It never modifies a plan or creates a plan revision.

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

The OpenAPI contract declares a `cookieAuth` scheme (the `mp_session` cookie from `POST /api/v1/session`): reads carry `security: []` (open) and every change and the publishing area carry `cookieAuth` (phase 6c). The server never trusts a client-supplied household identity. The publication contract below references those same DTOs; static routes are not additional API operations.

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

The public repository is [`jflopezo91/meal-plan`](https://github.com/jflopezo91/meal-plan), served at https://jflopezo91.github.io/meal-plan/. It holds publication data only. Its own Pages workflow checks out the public `meal-prep` code at a pinned commit, builds the static frontend with that data, and deploys it; no credential crosses between the two repositories. The publisher credential stays on the app server or another controlled outbound publishing process, with permission limited to the publication repository. Never ship credentials or tokens to the browser or public repository. Do not run arbitrary public-repository workflows on the NAS. A hosted runner can be considered only if it receives vetted publication artifacts and no inbound job can execute arbitrary work on the NAS.

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
- Expose capability flags from trusted mode/configuration for UI actions. Authorization is enforced on every server operation (phase 6c): changes and publishing need a session; capabilities are computed from the session.
- Document one local startup workflow for the API and frontend plus a static-mode preview. Anchor the legacy fixture to an explicitly configured Monday, never the request date or generation timestamp; an explicit reload/restart is sufficient for changing fixtures in this milestone.
- During frontend development, proxy same-origin `/api` requests from Vite to loopback Flask, or serve the built frontend through Flask. Avoid requiring permissive CORS. Static-mode paths must also work under a GitHub Pages repository subpath.

**Exit:** the same frontend views work against both API mode and a prepared static bundle. API/JSON examples and static DTOs render the same plan and grocery information. No automatic fallback occurs. CLI results, quantities, statistics, and solver behavior match before refactoring for representative inputs and the same solver configuration. Read services can be exercised without Flask context; solver services need no YAML/filesystem access. Contract, mode, and integration checks pass.

### Phase 3 — Saved weeks and home editing (complete)

**3a: domain and application workflows.** Implement plan creation, regeneration proposal, discard, acceptance, no-op, stale-base rejection, idempotent retries, and started-week policy independently of Flask/SQLite. Keep linked changes atomic and preserve complete saved assignment data. Test lifecycle transitions through application services.

**3b: SQLite.** Add migrations for catalog/planner inputs, plans, dated assignments, recipe/version snapshots, and revisions. Import YAML and the configured fixture with validation and an explicit repeatable conflict policy; repeated startup/import must not overwrite household edits. SQLite becomes authoritative; YAML is import/export. Use a local persistent volume, including when later deployed to the NAS. Add planner-input and plan-read SQLite adapters behind the phase 2 interfaces. Enforce unique household/week and per-revision date/meal keys, immutable revision snapshots, foreign keys, transactions, and atomic current-revision updates. Persist retry identifiers atomically with mutations. Compare file and database adapters on shared inputs and equivalent plan/grocery outputs. Verify races, stale commits, retries, rollback, restart, and backup restore.

**3c: API and home UI.** Extend OpenAPI before adding create/generate/preview/accept/history actions. Routes call tested workflows and map domain errors. Add week selection, explicit generation, proposal diff, accept/discard, and conflict refresh. Bound solver runtime and concurrent generation on the server. Preserve history now; a history browser may follow later. Mutations are API-mode/home-only. Static mode remains read-only.

**Exit:** home users create and revisit saved weeks after restart, preview and discard without changing current state, and accept revisions without losing their predecessors. Stale/duplicate requests and failed writes cannot silently overwrite or partially commit a plan. SQLite imports preserve the phase 2 read shapes and calculations, and a tested independent backup restores locally.

### Phase 4 — Monthly views, shopping trips, and a minimal exporter (complete)

**4a: shopping trips (domain, storage, API).** Add the `ShoppingTrip` aggregate and its invariants (one trip per week; frozen covered revisions on done; derived items and coverage) in the domain, a `ShoppingTripRepository` port with SQLite and in-memory adapters, and a migration. Extend OpenAPI before adding routes: list/get trips, create, add/remove week, mark done, reopen, delete, each carrying the expected trip `version` (stale → `409`), plus week coverage on plan summaries or a coverage read. Writes stay loopback/home-only like phase 3.

**4b: minimal manual exporter.** Pulled forward from phase 6a so the public site can show what to buy. `python -m mealplanner.entrypoints.db_cli export --months 2026-10,2026-11 --out <dir>` reads selected current plans, their groceries, and shopping trips in one read transaction, writes a new immutable `publications/<publication_id>/` directory, validates the whole bundle against `publication.schema.json`, and only then replaces `index.json`. A trip is published only when every week it covers is in the bundle; others are reported and skipped. Publishing is manual: the household copies the bundle to the Pages repository and pushes. Extend the publication contract with `shopping-trips/<trip_id>.json` entries in the manifest (status, covered weeks and revisions, frozen or current items). No credentials, automation, or retention cleanup yet.

**4c: frontend.** Add month/year navigation to both modes. API mode queries overlapping current weeks; static mode shows only months in the active publication and “No published plan” for absent months (absence from the public site does not assert absence on the NAS). A month intersecting up to six weeks displays the same underlying weekly plans and revisions; a week crossing a month boundary appears in both months. Show week coverage badges. API mode adds create/edit/done/reopen/delete for trips. Static mode adds a read-only, phone-friendly shopping trip view with device-local checkboxes.

Keep weekly shopping totals per week. A trip's items are a plain sum of its weeks' saved groceries; there are no monthly pantry or purchased-state rules. Keep weekends visibly outside the cooking schedule. Catalog editing is deferred.

**Exit:** month and year boundaries work in API and static mode, including weeks crossing month boundaries. Home users create a trip from several weeks, see what to buy, mark it done, and see those weeks as covered after reload; a week cannot be on two trips; stale trip edits are rejected. An exported bundle validates, shows the published trips on the static site, and a failed export leaves the previous `index.json` active.

### Phase 5 — Flexible meal swaps (complete)

“Swap” replaces the recipe/variant in one dated slot with any catalog recipe/variant. Exchanging two scheduled meals is a separate future action. The household decides; weekly rules are **advisory for swaps** (decided).

- **Rules:** a swap is never blocked by `domain/weekly_rules.py`. The preview and the result list the violations the swapped plan would have, as information. Generation (create, regenerate, month fill) still must satisfy every rule. Undo is allowed even when the restored revision violates rules (it may restore a swapped week); the server logs them, and the undo response shape is unchanged.
- **Linked lunch/dinner:** swapping one meal of a linked pair swaps both by default (they stay linked to the new recipe); the caller may choose `scope: "single"`, which swaps only that slot and removes the link from both. A swapped-in recipe's lunch→dinner extension is not added automatically.
- **Which days:** any slot dated today or later in the household timezone, including in a started week; past slots are fixed.
- **Flow:** a stateless preview (no stored proposal) returns the slot changes, the week's grocery delta (per ingredient and unit: before, after, difference), rule warnings, and the shopping trip impact (the trip holding the week, its status, and for a done trip the extra quantities now needed). Applying the swap carries `expected_revision` and an `Idempotency-Key`, appends exactly one revision (recomputing statistics and groceries from the saved assignments and the base revision's captured pantry), and returns the new plan plus warnings. A stale `expected_revision` is rejected. A replay with the same key and body returns the original result with 200; a swap to the variant already in the slot is a no-op (200, no revision). Undo appends a restoring revision as today, and is offered in the started week too. Variants with two raw protein ingredients (currently `bistec_a_caballo`) are not swappable until the data marks the second as `protein_extra`.
- **Shopping trips:** unchanged rules. An open trip's items follow the new revision automatically; a done trip keeps its frozen items and the week shows “changed since shopping”.
- **Regenerate after swaps:** regenerate still replaces the whole week (no locks). Its proposal reports how many slots were changed by swaps since the last generated revision, and the UI warns before accepting (“this will replace N meals you swapped”).

**Exit:** a swap updates the saved revision and that week's groceries in one revision; stale or duplicate requests cannot apply twice; linked swaps are atomic; rule violations are shown but never block a swap; undo cannot erase a later edit; open shopping lists follow the swap and done lists flag the week.

### Phase 5b — Long-lasting and fresh shopping

Some groceries are bought monthly (frozen proteins, rice, oils) and others every two weeks (vegetables, dairy). One shopping list can cover both, with each week covered separately per group.

- **Labels.** Every ingredient in `data/ingredients.yml` has `shelf_life: long_lasting | fresh` (required; validated with the other input policies). Initial labels: long-lasting = all proteins (eggs included), arroz, pasta, lentejas, arepa de maíz, maíz tierno, every condiment, fat and spice, queso parmesano, crema de coco; fresh = every vegetable and herb, papa, plátano verde, plátano maduro, yuca, pan, yogurt griego, crema de leche, queso fresco, manzana verde, limón. The UI calls the groups **Long-lasting** and **Fresh**. Labels are read from the current planner inputs, not stored in plan snapshots, so relabeling an ingredient regroups it everywhere (including done lists) without new plan revisions.
- **Coverage is per (week, group).** A shopping list holds entries `(plan_id, group)`; a week can be on a list for one group or both. A `(week, group)` pair is on at most one list (the old one-list-per-week rule, per group). Typical month: list A = weeks 1–4 long-lasting + weeks 1–2 fresh; list B (mid-month) = weeks 3–4 fresh. Lists created before this change cover both groups for their weeks.
- **Items.** A list's items are the weekly grocery rows of each covered week whose ingredient is in a covered group, summed as before, each item tagged with its `shelf_life`. Done lists still freeze the revision of each covered week.
- **Contract changes** (OpenAPI and the publication schema, which reuses `ShoppingTrip`):
  - `ShelfLife` enum `long_lasting | fresh`.
  - `ShoppingTripWeek.groups`: non-empty array of `ShelfLife`, order `long_lasting`, `fresh`. One week entry per week with all its groups on that list.
  - `ShoppingTripItem.shelf_life`: `ShelfLife`.
  - `CreateShoppingTripRequest.groups` and `AddTripWeekRequest.groups`: optional, default both. Adding a week already on the list with other groups extends its groups. A pair already on another list → the existing conflict error, naming the group.
  - `DELETE /api/v1/shopping-trips/{trip_id}/weeks/{plan_id}?group=<ShelfLife>` removes one group (the week entry disappears when no group is left); without `group` it removes the week.
  - `WeekCoverage` becomes `{ plan_id, week_start, week_end, groups: { long_lasting: GroupCoverage, fresh: GroupCoverage } }` with `GroupCoverage = { coverage: CoverageState, trip_id: string|null, changed_since_shopping: bool }`.
  - Swap trip impact reports every list the swapped week is on, per group.
- **UI.** There is no "what to buy" choice. Adding weeks (month view "Create shopping list" / "Add to list", or "Add a week" on an open list) takes every group of the week that is on no list, and says which group was skipped because another list has it. An open list's "Edit weeks" shows each week with two toggle chips (Long-lasting, Fresh): tapping an on chip removes that group (the whole week when it is the only group), tapping an off chip adds it, and a group on another list is disabled with that list's name; a separate ✕ removes the week. The last week of a list cannot be removed (delete the list instead). Week coverage shows both groups (e.g. "Long-lasting: covered · Fresh: not covered") in the week and month views. A list shows its items in two groups, Long-lasting then Fresh, each with the existing sections, and per week which groups it covers. The static site shows the same.

*5b status (backend implemented; the frontend is tracked separately).* `ShelfLife` lives in `domain/catalog.py` (`Ingredient.shelf_life`, required) and `PlannerInputs.shelf_lives()`. `domain/shopping_trip.py` models `TripWeek.groups`, `sum_items(weeks, labels)` (items carry `shelf_life`) and per-group `week_coverage`. `ShoppingTripService` takes a `labels` callable (the SQLite adapter's `SqlitePlannerInputReader.shelf_lives`), so labels are read from the current stored inputs on every view and never from snapshots. Migration 6 labels stored ingredients from `data/ingredients.yml` (it fails, leaving the database at version 5, if a stored ingredient has no shipped label) and rebuilds `shopping_trip_weeks` as `(trip_id, plan_id, shelf_group)` with `UNIQUE (plan_id, shelf_group)`, expanding every existing membership to both groups. Swap preview now returns `trips` (a list: every list holding the week, with its `groups`; `extra_needed` limited to those groups) instead of the single `trip`; this is the contract's "per group" swap impact. The publication validator checks group order and (plan, group) uniqueness across published trips, and that trip items equal the covered groups' rows. The example JSON under `examples/` and `static-examples/` carry the new fields.

### Phase 6 — Container, publishing, login and NAS operation

Order chosen so every step is testable on the development machine first, and nothing listens on the home LAN before login exists. Target hardware: UGREEN NASync DXP2800 (Intel N100, x86-64, 8 GB RAM, UGOS Pro). Its Docker app deploys Compose files through **Project → Create**; app data lives under `/volume1/docker/meal-prep/`.

**6a: container image (complete).** One image: the API-mode frontend build, Flask behind a production WSGI server (gunicorn), OR-Tools, and the app code; SQLite and other state in a `/data` volume, never in the image. Run as a non-root user with a read-only root filesystem, no added capabilities, no Docker socket, a health check, bounded logs and `restart: unless-stopped`. Migrations run at startup (`db_cli migrate`). A single Compose file works unchanged in WSL (loopback port only) and in the UGOS Docker app: it references a prebuilt image (no `build:` on the NAS) and absolute host paths. GitHub Actions builds `linux/amd64`, pushes to GHCR, and attaches build provenance (`actions/attest-build-provenance`). Deployments reference the image by digest (`ghcr.io/jflopezo91/meal-prep@sha256:…`), never a mutable tag; an update is `gh attestation verify`, then change the digest and redeploy, and a rollback is the previous digest. Supply chain: actions pinned to commit SHAs, base image pinned by digest, locked Python dependencies and `npm ci`, `packages: write` only in the publish job and only on pushes to the project branch, never on pull requests.

**6b: Publish button (complete).** Harden the phase 4 exporter and publish from the home app. The home page gets a publish view: explicit month selection, a preview of every included week and trip, and the public-sharing warning. `POST` to a publication endpoint runs the export, validates the bundle, and pushes it with a deploy key to `jflopezo91/meal-plan`, whose workflow builds and deploys Pages. One publication job at a time (a lock; an identical bundle is a no-op). Record the exported plan/revision/snapshot tokens and job status privately; show unpublished revisions, the last failure, and “live” only after the Pages deployment is confirmed through the GitHub API. The deploy key is an SSH key with write access to `meal-plan` only, mounted as a file (`/run/secrets/deploy_key`), never an environment variable, image layer, repository file or browser value. GitHub's SSH host keys are pinned in the image. A failed export, push or deploy leaves the previous public version live. Until 6c the endpoint exists only in loopback development.

*6b status (implemented).* `application/publishing.py` (`PublishingService`) runs validate months, sync the working copy to the remote, build and check the bundle with `PublicationService`, compare, write, commit, push, and records a job. The `PublicationRepository` port is implemented by `adapters/git_publication.py` (subprocess git over SSH with the deploy key; working copy `<data dir>/publish-repo`; the remote `main` is authoritative and is never force-pushed), the `DeploymentStatusReader` port by `adapters/github_deployments.py` (unauthenticated GitHub REST: the workflow runs for the pushed commit, cached 10 s). Jobs live in the `publication_jobs` table (migration 4) with the exported plan/revision/snapshot tokens, trips, a content hash and the application revision. The public repository layout is `publication/` (as `scripts/export_pages_publication.sh` produces, newest three publications kept) plus `app-ref`, one line holding the 40-character meal-prep commit whose frontend code must render this data; the `meal-plan` workflow checks out public `meal-prep` at that commit. Rules worth knowing: an identical bundle (same content, ignoring `publication_id` and `exported_at`, built from the same application revision) is `unchanged` and makes no commit; a changed `app-ref` alone is a change; one job at a time (process lock plus a database check, and a `running` row older than 10 minutes counts as crashed and is marked failed); a push that the remote rejects (it moved) fails the job and the next attempt re-syncs; `live` is set only after the GitHub run for the pushed commit completed successfully, and an unreachable GitHub API leaves the job `pushed`. HTTP routes are in `entrypoints/http/publish_routes.py` and `openapi.yaml` (`/api/v1/publishing`, `/preview`, `/jobs`, `/jobs/{job_id}`; `can_publish` in capabilities). Settings: `MEALPLANNER_PUBLISH_REPO` (git URL; unset disables publishing), `MEALPLANNER_PUBLISH_KEY` (default `/run/secrets/deploy_key`), `MEALPLANNER_PUBLISH_KNOWN_HOSTS` (default `/etc/ssh/ssh_known_hosts`), `MEALPLANNER_PUBLISH_WORKDIR` (default `<db dir>/publish-repo`), `MEALPLANNER_PUBLIC_SITE_URL` (default `https://jflopezo91.github.io/meal-plan/`), `MEALPLANNER_APP_REVISION` (the image sets it from the `APP_REVISION` build arg; in a checkout it falls back to `git rev-parse HEAD`; unknown means publishing answers `503 publishing_disabled`) and, optionally, `MEALPLANNER_PUBLISH_GITHUB_REPO` (default `jflopezo91/meal-plan`, the repository whose runs are read).

**6c: household login (complete).** Viewing is open on the home network: every `GET` (plans, groceries, revisions, shopping lists, coverage, the variant catalog, `capabilities`) works without a session. Every state-changing request (`POST`/`PUT`/`PATCH`/`DELETE`, including swap preview, which is a `POST`) and the whole `/api/v1/publishing` area need a valid session, else `401 unauthenticated`. Contract: `GET /api/v1/session` (200 `{username}` or 401), `POST /api/v1/session` (login: 200 + cookie, 401 `invalid_credentials`, 429 `too_many_attempts` with `Retry-After`, 422 malformed body), `DELETE /api/v1/session` (204, idempotent); `GET /api/v1/capabilities` adds `authenticated` and `username` (all `can_*` false without a session). Implementation:
- Passwords: scrypt (n=2^15, r=8, p=1, 16-byte salt, 64-byte key) in a self-describing string `scrypt$n$r$p$salt$hash`, constant-time compare, a dummy hash check for unknown usernames, minimum length 10. Usernames are NFKC-normalized and lowercased. No third-party dependency.
- Sessions: random 256-bit token in the `mp_session` cookie (`HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` unless `MEALPLANNER_COOKIE_SECURE=0`, `Max-Age` = remaining lifetime); only its SHA-256 is stored in SQLite (migration 5: `users`, `sessions`). 30 days idle, 90 days absolute; `last_seen` refreshed at most hourly; logout, a password change, disabling or removing a user delete the sessions.
- Login rate limit (in process, one gunicorn worker): 5 failures in 15 minutes per normalized username and per client address; success clears the username counter; bounded memory.
- Cross-origin protection on every state-changing request, login included: `403 cross_origin_rejected` when `Sec-Fetch-Site` is present and not `same-origin`/`none`, or, without it, when `Origin` differs from the request's own scheme+host. Requests with neither header (curl, scripts) pass. Bodies must be `application/json`. No CORS headers are sent.
- Behind a reverse proxy (6d) set `MEALPLANNER_TRUSTED_PROXIES=<n>` (default 0) so the client address, scheme and host come from `X-Forwarded-*` (Werkzeug `ProxyFix`); never set it when the app is reachable without that proxy.
- Every logged-in user may do everything (one household role); `can_publish` still needs `MEALPLANNER_PUBLISH_REPO`.
- The loopback rule and `MEALPLANNER_TRUST_NETWORK_PEERS` are gone.
Users: `db_cli user add|passwd|disable|enable|logout-all|remove <username>` and `user list` (passwords come from a hidden prompt or, with `--password-stdin`, one line of stdin; never from an argument). No public registration. `Secure` cookies are accepted by Chromium and Firefox on `http://localhost` and `http://127.0.0.1` but not over plain HTTP to a LAN address; the LAN setup is decided in 6d.

**6d: NAS go-live.** Home-network access starts as plain HTTP, like most NAS apps (Jellyfin serves `http://<nas>:8096` the same way): Compose publishes `8080:8080` on the LAN and sets `MEALPLANNER_COOKIE_SECURE=0`. Viewing needs no login; the login password crosses the home Wi-Fi unencrypted, an accepted trade-off on a WPA2/WPA3 network. UGOS Pro has no built-in reverse proxy or Let's Encrypt for apps, so HTTPS, if wanted later, is a Caddy container in the same Compose project with a DNS-challenge certificate (e.g. a DuckDNS name pointing at the NAS's LAN address; no inbound ports), then `MEALPLANNER_TRUSTED_PROXIES=1`, the app port no longer published and `Secure` cookies back on. No app change is needed for that. First a throwaway Compose project in the UGOS Docker app that only prints a mounted `secrets:` file, to confirm the secret mechanism (fallback: a read-only bind mount of the same file). Generate the deploy key on the NAS over SSH (`chmod 600`), add only the public key to GitHub, then turn SSH off. Deploy by digest, measure solver time on the N100, and set container memory/CPU limits from that. SQLite-consistent backups (`db_cli backup`) copied somewhere independent of the NAS, with a restore test into a clean container. Document update, rollback, migration and restore. Confirm no port forward, tunnel, CORS trust or NAS admin endpoint exposes the app, and check public cache behavior from outside the LAN.

**Exit:** household devices reach the app only through the intended LAN access policy, and unauthorized requests cannot read household data or mutate plans. The NAS publishes explicitly selected months outbound; a failed publish leaves the previous public version intact. A public visitor browses only approved read-only artifacts and can refresh to one coherent publication. Backup/restore, restart, migration, update/rollback by digest, image provenance, credential scope, cost assumptions and old-publication retention are documented and exercised.

## Access and operating constraints

- Viewing is open on the home network; changes and publishing need a household login (phase 6c). The server-side session check is the boundary; hiding controls in React is not. The container port stays bound to `127.0.0.1` until the LAN HTTPS decision in 6d, because the session cookie is `Secure`.
- The session cookie is `SameSite=Lax` and state-changing routes reject cross-site requests (`Sec-Fetch-Site` / `Origin`). Do not use permissive CORS as authentication. No public registration or multiple-household support is planned.
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

Then start the frontend as above (`npm run dev:api` in `site/`). `GET /api/v1/capabilities` reports `authenticated`, `username` (when logged in) and `can_edit_plans`, which is true only for a logged-in user of a SQLite-backed API. It also reports `can_edit_shopping_trips` (same condition). Create a user first: `python -m mealplanner.entrypoints.db_cli user add <name>` (then log in through `POST /api/v1/session`). Shopping trip routes live under `/api/v1/shopping-trips` (changes need a login like plan writes; the expected trip `version` goes in the JSON body, or in the `expected_version` query parameter on DELETE; a mismatch is `409 stale_version`), and `GET /api/v1/coverage?from=&to=` reports per-week coverage. Without `MEALPLANNER_DB` the API serves the JSON snapshot and rejects writes with `403 read_only`; changes without a session get `401 unauthenticated`. Optional settings: `MEALPLANNER_TIMEZONE` (default `America/Bogota`, also the clock for "future week" checks), `MEALPLANNER_SOLVER_TIMEOUT_SECONDS` (default 30) and `MEALPLANNER_MAX_CONCURRENT_GENERATIONS` (default 1; extra requests get `503 generation_busy` with `Retry-After`).

Open `http://127.0.0.1:5174/meal-prep/`. This uses the checked-in populated publication bundle, not newly generated CLI files. `VITE_STATIC_BUNDLE=empty npm run dev:static -- --port 5174` exercises the empty publication. Neither mode offers generation, persisted edits, or swaps yet; grocery checkmarks remain browser-local.

The default view is explicitly October 19–25, 2026. To anchor API data to another Monday, set `MEALPLANNER_WEEK_START=YYYY-MM-DD` before starting Flask and set matching `VITE_DEFAULT_FROM=YYYY-MM-DD VITE_DEFAULT_TO=YYYY-MM-DD` before starting Vite. The API timezone defaults to `America/Bogota` and accepts `MEALPLANNER_TIMEZONE`. Static sample dates remain those in their manifests. Date navigation is a later milestone.

Check both modes for weekly cards, recipe details, statistics, theme, grocery checkmarks, and refresh. Stop Flask to check the API error state; static mode should continue working independently. A failed refresh should retain the last complete plan and grocery view. Checkmarks are scoped to mode and snapshot identity.

### Run the container

This is the day-to-day way to run the home app (and the only one configured to publish): open http://127.0.0.1:8080/. Its data lives in `var/docker-data/` (git-ignored). The Vite + Flask flow above is for developing the code.

One image serves the API-mode frontend (at `/meal-prep/`, `/` redirects there) and the API from one origin, behind gunicorn. Static serving is enabled only when `MEALPLANNER_STATIC_DIR` is set (the image sets it); the Vite dev flow above is unchanged. `GET /healthz` is the liveness check (no database access). Viewing is open; changes and publishing need a household login (phase 6c), so the container needs at least one user. Create the first one after the container is running (the password is read from stdin, not an argument):

```sh
docker compose -f deploy/docker-compose.dev.yml exec meal-prep python -m mealplanner.entrypoints.db_cli user add juan   # prompts twice
printf '%s\n' "$PASSWORD" | docker compose -f deploy/docker-compose.dev.yml exec -T meal-prep python -m mealplanner.entrypoints.db_cli user add juan --password-stdin
```

Other commands: `user passwd|disable|enable|logout-all|remove <name>`, `user list`. Compose binds the port to `127.0.0.1` in WSL; on the NAS it is opened to the LAN over plain HTTP (6d). The session cookie is `Secure`; browsers accept it on `http://127.0.0.1:8080`. `MEALPLANNER_COOKIE_SECURE=0` and `MEALPLANNER_TRUSTED_PROXIES=<n>` are documented under 6c.

WSL (needs Docker; the user must be in the `docker` group):

```sh
cd /home/julopez/dev/meal-prep
mkdir -p var/docker-data
docker compose -f deploy/docker-compose.dev.yml up --build   # http://127.0.0.1:8080/
docker compose -f deploy/docker-compose.dev.yml down
```

A fresh `/data` is created on first start (`db_cli import` loads the planner inputs shipped in the image, no plan data); later starts only run `db_cli migrate`. Changed recipe inputs in a newer image are not applied automatically; run `db_cli import --replace-inputs` deliberately. The container runs as uid 10001 with a read-only root filesystem, a tmpfs `/tmp`, no capabilities and one gunicorn worker (4 threads, 300 s timeout; override with `WEB_WORKERS`, `WEB_THREADS`, `WEB_TIMEOUT`). The dev Compose file runs as your uid (`DEV_UID`/`DEV_GID`) so the bind-mounted `var/docker-data` and the deploy key stay yours; the dev build passes that uid/gid as `APP_UID`/`APP_GID` so the `app` user in the image has it (ssh, used for the publish push, refuses to run for a uid with no passwd entry).

NAS (UGOS Docker app):

1. Over SSH, once: `mkdir -p /volume1/docker/meal-prep/data && chown 10001:10001 /volume1/docker/meal-prep/data`.
2. After a push to `feature/cloud-migration`, the **Container image** workflow publishes `ghcr.io/jflopezo91/meal-prep` and prints the digest in its job summary. Verify it: `gh attestation verify --owner jflopezo91 oci://ghcr.io/jflopezo91/meal-prep@sha256:<digest>`.
3. Put the digest in `deploy/docker-compose.yml` (`image:` line), then **Docker > Project > Create**, paste the file and start it. The port stays bound to `127.0.0.1` until the LAN HTTPS decision (6d); then create the first user with `docker exec -it <container> python -m mealplanner.entrypoints.db_cli user add <name>`.
4. Update: verify the new digest, replace it in the project, redeploy. Rollback: put the previous digest back and redeploy; the data volume is untouched, and `migrate` refuses a database newer than the image supports, so restore a backup (`db_cli backup`/`restore`) if a rollback crosses a schema change.

The image also contains git, openssh-client and GitHub's SSH host keys (`deploy/github_known_hosts`, from `api.github.com/meta`) for the phase 6b push; refresh that file if GitHub rotates keys. Python dependencies are hash-locked in `planner/requirements.lock` (regenerate with `pip-compile --generate-hashes --strip-extras --extra api --extra server -o requirements.lock pyproject.toml` in `planner/`).

### Run Phase 4 (manual export) locally

If the HTTP server answers `503 source_unavailable` for a SQLite database, the file is probably unmigrated (the API never migrates a database it only reads). Apply pending migrations to an existing database, then export:

```sh
.venv/bin/python -m mealplanner.entrypoints.db_cli migrate [--db var/mealplanner.db]
.venv/bin/python -m mealplanner.entrypoints.db_cli export --months 2026-10,2026-11 --out /tmp/publication [--db var/mealplanner.db] [--timezone America/Bogota]
```

`migrate` refuses a missing file (use `import` to create a database) and is idempotent. `export` never writes to the database. It publishes the current revision of every plan whose Monday-Sunday week overlaps a selected month, plus every shopping trip whose weeks are all in that set (others are listed as skipped with the reason), into `<out>/publications/<publication_id>/`, validates the written bundle (including `scripts/validate_publication_contract.py`; `--skip-schema-validation` skips only that JSON-schema pass), and only then replaces `<out>/index.json`. A failure before that last step leaves the previous `index.json` untouched and removes the new directory. Earlier publication directories in `--out` are kept (no retention cleanup yet).

Reads are not one SQLite transaction (plans, groceries and trips come through separate repositories); the exporter checks that they agree and re-reads up to three times if the data changed mid-export, then fails. Publishing stays manual: copy the contents of `<out>` into the Pages site's `publication/` directory (index last) and push. Old publication directories you no longer want must be removed by hand for now, and the validator only accepts one active publication, so validate the Pages directory only after pruning to the active one.

### Publish from the home app (phase 6b)

The public site is built by the `meal-plan` repository's workflow from whatever the home app pushes there; the app never serves the public site itself. Publishing needs a logged-in user (and the port stays loopback-bound until 6d).

One-time setup:

1. In `meal-plan` (Settings, Deploy keys) add the **public** half of a key as a deploy key with **write access**, and in Pages settings choose "GitHub Actions" as the source. The private half is a file only the container reads: dev `var/secrets/deploy_key` (git-ignored, `chmod 600`, owned by your user), NAS `/volume1/docker/meal-prep/secrets/deploy_key` (`chown 10001:10001`, `chmod 600`). A Compose `secrets: file:` mount keeps the host file's owner and mode, and ssh rejects a key the container user does not own or others can read, so these must match the uid the container runs as (dev: your uid; NAS: 10001).
2. Dev: run Compose with the commit the frontend code should come from, and make sure that commit exists on the public `meal-prep` repository (the `meal-plan` workflow checks it out):
   `APP_REVISION=$(git rev-parse HEAD) docker compose -f deploy/docker-compose.dev.yml up --build`. The dev file builds the image with your uid/gid (`DEV_UID`/`DEV_GID`), because ssh refuses to run for a uid without a passwd entry. The NAS image gets its revision from the container workflow (`github.sha`). Without Docker (`python -m mealplanner.entrypoints.http.app`), export `MEALPLANNER_PUBLISH_REPO=git@github.com:jflopezo91/meal-plan.git`, `MEALPLANNER_PUBLISH_KEY=var/secrets/deploy_key` and `MEALPLANNER_PUBLISH_KNOWN_HOSTS=deploy/github_known_hosts`. An existing database needs `db_cli migrate` once (migration 4) before the API will read it; the container does that at start.

Publishing: open the publish view, select months, check the preview (every week with its revision and the revision last confirmed live, every trip, the warning that everything is public), and press Publish. The request runs the export, validates the bundle, commits `publication/` and `app-ref` and pushes, then answers `pushed` (or `unchanged` when nothing differs). The page then follows the job until GitHub reports the Pages deployment finished (`live`) or failed. Over HTTP:

```sh
curl -s 'http://127.0.0.1:8080/api/v1/publishing/preview?months=2026-10,2026-11'
curl -s -X POST -H 'Content-Type: application/json' -d '{"months":["2026-10","2026-11"]}' http://127.0.0.1:8080/api/v1/publishing/jobs
curl -s http://127.0.0.1:8080/api/v1/publishing/jobs/<job_id>
```

A failed publish (export, validation, push rejected, GitHub unreachable) records a failed job with a short message and leaves the previously pushed version in place; just publish again. To try it without GitHub, point `MEALPLANNER_PUBLISH_REPO` at a local bare repository (`git init --bare --initial-branch=main x.git`, seeded with one commit on `main`).

To preview a bundle locally, export it (`db_cli export --months … --out <dir>`) and run `VITE_PUBLICATION_DIR=<dir> npm run dev:static` (paths resolve from the repository root). The old Pages route from this repository (committed `publication/`, **Build Frontend** and **Deploy to Pages** workflows, `scripts/export_pages_publication.sh`) was retired on 2026-10-21; Pages never runs the solver.

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
| Household login | Viewing open on the home network; every change and all publishing need a login. Server-side sessions in SQLite (cookie `mp_session`: HttpOnly, SameSite=Lax, Secure), scrypt passwords, 30-day idle / 90-day absolute expiry, 5 failures per 15 minutes per username and per address, `Sec-Fetch-Site`/`Origin` check on changes, users created by CLI only, one household role (decided; LAN HTTPS decided in 6d) | phase 6c / 6d |
| Public sharing | Separate public repository `jflopezo91/meal-plan` (data only; its workflow builds the frontend from `meal-prep` at a pinned commit) | phase 6b |
| Frontend | One React app, explicit API/static reader mode, shared DTOs/components | implement in phase 2 |
| Week/timezone | Monday–Sunday; default `America/Bogota`; weekday cooking | configure before importing saved weeks in phase 3 |
| Revisions | One stable plan per week; append-only accepted full-plan revisions | phase 3 |
| Static publication | Manual month selection; immutable bundle; retain latest three successful publications. Minimal manual exporter (bundle + manual push) in phase 4; automated outbound publishing in phase 6 | phase 4 / 6 |
| Static checkmarks | Device-local and never exported/written back | phase 2 |
| Home checkmarks | Device-local only; shared household state is the shopping trip, not purchased quantities (decided) | — |
| Shopping trips | Aggregate covering a set of weeks; open/done; items = plain sum of weekly groceries; no pantry or purchase tracking; one trip per week; published to the static site | phase 4 |
| Cross-week rules | None (decided) | — |
| Month generation | Batch of independent weekly plans; no monthly or cross-week rules; existing weeks never overwritten | — |
| Swaps | Any catalog recipe/variant in a today-or-later slot; preserve unrelated meals; weekly rules are advisory warnings for swaps and undo, enforced for generation; linked pairs swap together unless `scope: single`; no locks, regenerate warns about swapped meals | phase 5 |
| Shelf-life groups | Ingredients are long-lasting or fresh; shopping lists cover (week, group) pairs so long-lasting can be bought monthly and fresh every two weeks on the same or different lists | phase 5b |
| Container image | Built by GitHub Actions, pushed to GHCR with build provenance; deployed by digest, verified with `gh attestation verify`; non-root, read-only root filesystem | phase 6a |
| Publish transport | Home app commits `publication/` + `app-ref` to `meal-plan` `main` over SSH (deploy key); the remote is authoritative and never force-pushed; identical content is a no-op; `live` only after the GitHub Actions run for the pushed commit succeeds; job records private in SQLite | phase 6b |
| Publish credential | Deploy key with write access to `meal-plan` only; generated on the NAS, mounted as a file, never an env var | phase 6b / 6d |
| NAS | UGREEN DXP2800 (N100, x86-64, 8 GB, UGOS Pro); Compose project in the UGOS Docker app; data under `/volume1/docker/meal-prep/` | phase 6d |
| Costs | Verify NAS, power, storage, GitHub plan, domain, transfer, and tooling at launch | before phase 6 |
