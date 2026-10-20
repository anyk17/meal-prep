# Migration to cloud: household meal planner

Status: agreed migration direction; implementation details remain subject to refinement. Features described here are not implemented yet.

Progress: phase 1 domain decisions, read API contract, response examples, and validation tooling are complete. Phase 2 application implementation is next. The existing frontend and CLI still run as before.

Review outcome: the first delivery milestone combines phases 1 and 2 into the existing weekly viewer running through an API. Browser generation, saved-week management, and revision history move to phase 3. The week lifecycle section below defines regeneration, replacements, and revision history.

## Goal and boundaries

Turn the current static weekly viewer into a household web app with recipes and saved plans in a database, assignments on actual dates, a monthly calendar, and eventually recipe replacements. Develop and validate locally before deploying to one AWS Lightsail instance.

Priorities: predictable infrastructure costs, household-only access, preservation of the existing planning rules, and small milestones that can be exercised through the frontend. The NAS is excluded. Public registration, multiple households, autoscaling, and managed database services are outside the initial scope.

## Current implementation

- `planner/mealplanner/cli.py` loads YAML, runs the Python OR-Tools solver, and generates JSON files.
- `planner/mealplanner/render.py` combines response construction with filesystem writes.
- `site/src/App.jsx` reads `site/public/plan.json` and `shopping_list.json`; there is no application API.
- Plans identify meals by weekday and meal type, without dates or saved plan identities.
- Grocery checkmarks are browser-local and keyed by ingredient, so they are neither shared across devices nor scoped to a shopping period.
- The active rules plan weekday lunches and dinners. Weekly protein counts are exact; some recipes link lunch and dinner. These constraints affect calendar planning and replacements.

## Proposed architecture

```text
React frontend -> versioned HTTP API -> application services
                                            |-- planner-input interface -> YAML / SQLite
                                            |-- plan-read interface -> JSON / SQLite
                                            |-- existing solver (explicit input data)

Storage: YAML/JSON first -> SQLite database later
Deployment: one Lightsail instance with persistent application data
```

Use Python/Flask for the API, following the household's framework preference. Keep an explicit OpenAPI contract and SQLite with schema migrations as the proposed storage choice. Keep the solver independent of HTTP and database code. Extract reusable generation/shopping services in phase 2 and keep the CLI as their caller; the API invokes them when generation workflows are introduced in phase 3.

SQLite is a starting recommendation for this household workload, not a requirement imposed on the contract. The frontend must not depend on storage layout. Use explicit adapters around data loading and saved-plan access rather than building a generic storage framework.

Establish both storage boundaries in phase 2:

| Concern | Initial adapter | Database adapter | Consumers |
| --- | --- | --- | --- |
| Recipes, ingredients, rules, and pantry | YAML planner-input reader | SQLite planner-input reader | Generation and shopping services |
| Generated plan and groceries | JSON snapshot reader | SQLite plan reader | API application read services |

Interfaces return application/domain data, without file paths, YAML-specific structures, or database session/row objects leaking into consumers. The solver receives explicit validated inputs rather than a repository or loader. File parsing and filename conventions belong to adapters; shared business validation applies regardless of storage. Database-backed revision writes extend these read boundaries in phase 3 rather than requiring a temporary file-based write implementation.

Evaluate storage migration separately from new functionality. Switching adapters should preserve existing read contracts, frontend behavior, calculations, and domain rules. Database work still includes schema migrations, import, transaction handling, uniqueness constraints, and concurrency verification. Regeneration, revision history, and acceptance workflows are feature work, not overhead caused by changing storage.

### Flask and a focused DDD approach

Agreed approach: apply domain-driven design to the planning rules and vocabulary within one application. Start domain design in phase 1, establish implementation boundaries in phase 2, and implement the richer plan behavior in phase 3 before connecting database writes. The useful complexity is in weekly constraints, linked meals, dated assignments, and accepted revisions, even though the number of users is small. Layering supports those decisions but is not by itself DDD.

| Responsibility | Owns |
| --- | --- |
| HTTP API | Flask routes, request/response validation, authentication, and mapping application errors to HTTP |
| Application services | Reading plans now; orchestrating generation, preview, acceptance, and replacement later |
| Domain | Week identity, assignments, revision behavior, locks, and policies governing valid changes |
| Adapters | YAML/JSON access first, SQLite access later, and the OR-Tools generation implementation |

Treat a weekly plan as the consistency boundary (a DDD aggregate): validate affected assignments together and commit one accepted revision atomically. Store historical snapshots separately; do not load the entire revision history for every operation. Recipe catalog changes have a separate lifecycle, and shopping totals are derived from accepted assignments.

Use a small repository interface where it helps the planned file-to-database migration. The initial read adapter needs only read operations. Add mutation methods and transaction handling in phase 3; avoid speculative base repositories or a class for every action. Ordinary Python functions and existing Pydantic models are appropriate where they express the rules clearly.

The current `solve_plan` and shopping functions receive a filesystem-oriented `DataLoader`. In phase 2, move that dependency behind the YAML adapter and pass explicit recipe/ingredient/rule/pantry data into the reusable services instead. Preserve the solver algorithm and CLI behavior during this refactor. Keep meaningful business policies identifiable independently of their OR-Tools encoding so generation and manual replacement obey the same rules. Check both implementations with representative rule cases if separate evaluators are necessary.

Use Flask's `create_app` factory to wire services and adapters, keeping Flask request/application globals out of domain code. The OpenAPI document remains the reviewed contract; implementation checks must detect drift. API schemas can evolve separately from storage schemas and solver internals.

The first milestone delivers the existing viewer through the API and establishes both file-storage boundaries, including a limited refactor of CLI generation inputs. Introduce richer plan behavior when writes arrive. Microservices, event sourcing, a message bus, and separate read/write databases are outside the planned architecture; immutable revision snapshots do not require them.

Framework references: [Flask application factories](https://flask.palletsprojects.com/en/stable/patterns/appfactories/) and [Flask design decisions](https://flask.palletsprojects.com/en/stable/design/).

## Adjustments to the suggested sequence

1. **Design dates and identities before implementing persistence.** Include a required Monday `week_start`, stable plan and assignment IDs, and actual meal dates in the first contract. Implement the richer calendar later. Otherwise the database phase will immediately require another API redesign.
2. **Separate frontend integration from state management.** Use the existing files behind a read-only API first. Isolate both YAML planner inputs and JSON plan reads in phase 2, while keeping generation available through the CLI. Implement saved-week mutations and history with the database, avoiding a temporary file-based database implementation.
3. **Make a month a calendar view before making it a solver horizon.** Keep weekly rules and weekly generation initially. A calendar month intersects four to six weeks; treating it as exactly four independent weeks creates boundary problems.
4. **Separate replacing a recipe from rebalancing a plan.** Start by preserving other assignments. If a replacement cannot satisfy the rules, explain why. Broader solver-driven changes need a separate preview and confirmation flow.
5. **Treat deployment as its own milestone.** Household access, backup restoration, and a review of possible extra charges are part of readiness, even if feature development is already complete.

## Week identity, regeneration, and revisions

**One calendar week has one saved plan, and that plan has one current revision.** Each accepted change to planned meals creates an immutable revision of the same plan. Users normally see their calendar and current meals; history is a secondary feature.

| Concept | Example | Meaning |
| --- | --- | --- |
| Calendar week | `week_start: 2026-10-19` | Monday date; uniquely identifies the household's planning period |
| Plan | `plan_id: plan_123` | Stable identity throughout regeneration and replacements |
| Revision | `revision: 3` | Third accepted state of this plan |
| Meal assignment | `2026-10-21`, `lunch` | Stable slot within the plan; its selected recipe can change |
| Proposal | Candidate based on revision 3 | Preview that has not changed the saved plan |

Use dates rather than week number alone, avoiding ambiguity around years and week-numbering conventions. Enforce uniqueness for household/week start and for date/meal within each revision. A meal's date must belong to its plan's week. Assignment IDs stay stable across revisions of that plan.

### What happens when someone generates again?

| Action | Result |
| --- | --- |
| Generate for an empty week | Save a valid plan as revision 1; first generation can be a single explicit action |
| Open or refresh a planned week | Read the current revision; never rerun the solver |
| Generate through the creation action for an already planned week | Return an existing-week conflict and offer “Regenerate” explicitly |
| Regenerate an existing week | Produce a proposal and show changed meals; current plan stays active |
| Discard the proposal or solver fails | No change and no new revision |
| Accept a valid changed proposal | Append revision 2, make it current, retain revision 1 |
| Accept an identical result | No change; do not create a meaningless revision |
| Retry the same accepted request after a lost response | Return its existing outcome without appending another revision |

Changing the solver seed may still produce the same assignments. Store the actual result; do not use the seed as plan identity or assume it can reconstruct history after recipes/rules change. If “give me something different” is a requirement, enforce a difference in the generation algorithm later.

A proposal records its base revision and the relevant recipe/rule input versions or snapshots. Acceptance checks that it is still valid and commits the previewed assignments; it must not rerun the solver and silently accept a different result. A stale proposal requires a new preview. Short-lived proposals may expire; an expiration leaves the current plan untouched.

### Does a recipe replacement create another revision?

Yes: one accepted replacement creates one revision of the same weekly plan. If linked lunch/dinner assignments both change, they belong to the same revision and commit together.

Example:

```text
Week beginning October 19 — plan_123
  revision 1: initial generation
  revision 2: accepted regeneration
  revision 3: Wednesday lunch recipe replaced
  revision 4: undo replacement, restoring revision 2's meals
```

History stays append-only. Restoring old meals creates a new current revision with a restore reason. It does not roll the revision counter backward. Future undo must require the expected current revision so it cannot erase a later change from another device.

Version the complete plan, not each meal independently. Ten assignments per week make full snapshots straightforward, and weekly constraints mean changes are validated together. Preserve selected recipe/variant data, resolved quantities, and rule context needed to understand and validate a saved state. A normal table of revision snapshots is enough; a general event-sourcing system is unnecessary here.

Grocery checkmarks, opening a week, and theme changes do not create meal-plan revisions. Editing a catalog recipe does not retroactively change a saved revision.

### Concurrency and shopping consistency

Every plan mutation carries an expected revision. If two devices read revision 3 and one commits revision 4, the other cannot overwrite it: return a conflict and refresh/re-preview. First creation uses a database uniqueness constraint so simultaneous requests cannot create two plans for the same week. Request retry identifiers handle accidental resubmission separately from revision checks.

Append the revision and move the current pointer in one transaction. Compute and return shopping totals and statistics for that revision; expose the revision in responses so the frontend never combines a plan from revision 4 with groceries from revision 3. Expensive solving happens outside the transaction, followed by a revision check at commit.

Keep purchased/checkmark state separate from required quantities. A future swap may increase an already checked ingredient: the shared-shopping feature must show that adjustment or reopen the item under a documented policy. It must not silently treat the larger quantity as already purchased.

### Boundaries to preserve

- **Started weeks:** proposed initial rule is whole-week regeneration only for future weeks. Keep past meals fixed. Enable changes to remaining meals in a started week when locking and constraint handling exist; define today's editable meals explicitly because the app does not yet track whether a meal was eaten.
- **Manual choices:** when swaps arrive, preserve them through explicit locks before offering subsequent regeneration. A general regenerate action must not silently erase chosen meals.
- **Replacement feasibility:** exact protein counts mean chicken-to-fish may require changing another meal. Start with compatible replacements that preserve other meals and explain conflicts. Rebalancing is a later previewable action.
- **Monthly view:** read the current revisions of overlapping weeks and display their dated assignments. A week crossing October and November remains the same plan in both views. A month does not need its own competing plan versions.

### How much belongs in the first milestone?

The first milestone delivers the existing weekly viewer through a real HTTP API. Use current files, bind the sample plan to an explicitly selected week, and include stable IDs, dates, and a baseline revision in responses. Read recipe details and groceries through that API; preserve current browser-local checkmarks with appropriate plan/revision scoping.

Also decouple CLI generation from YAML access: reusable generation/shopping services receive validated data from the YAML adapter. This prepares the second storage boundary for SQLite without adding browser generation or changing the planning algorithm.

During this temporary read-only stage, a fixture reload is a development operation, not a saved user revision. Changed fixture content must have a distinguishable snapshot token/revision so cached UI state is not incorrectly reused. Validate coherent plan/grocery inputs before publishing the snapshot. An import into the database starts its persistent history at revision 1.

Contract-first work defines this read interface and documents future write semantics. It does not require implementing regeneration, drafts, locks, full revision history, or swaps before the frontend/backend architecture can be validated. Implement those mutations with the database in the next milestone.

## Phases

Delivery milestones: phases 1–2 deliver frontend/backend parity locally; phase 3 delivers saved dated weeks; phase 4 delivers the monthly view; phase 5 delivers replacements; phase 6 delivers cloud access. The contract is the first task within the first milestone, not its entire user-visible result.

DDD work progresses with those milestones:

| Phase | DDD outcome |
| --- | --- |
| 1 | Shared vocabulary, invariants, and consistency boundaries shape the API contract |
| 2 | API reads and CLI generation use explicit boundaries for JSON plans and YAML inputs; core logic is independent of both file formats |
| 3a | Plan lifecycle behavior is implemented and tested independently of Flask and SQLite |
| 3b | Persistence enforces uniqueness, atomic revision commits, and concurrency rules |
| 3c | API and frontend expose the tested application workflows |
| 4–5 | Calendar and replacement features extend the existing model and policies |

### Phase 1 — Define the domain model and API contract

First describe the domain behavior, then deliver a reviewed `openapi.yaml` and representative request/response examples before implementing routes. OpenAPI is the reviewed source of the public interface; verify the running API against it to prevent drift.

Define:

- Resources: recipes, ingredients, planning rules, weekly plans, dated meal assignments, and shopping lists.
- Stable identifiers, recipe variants, quantities and units, linked lunch/dinner assignments, plan revisions, and timestamps.
- Calendar conventions: Monday week start; `YYYY-MM-DD` meal dates interpreted in the household timezone; UTC timestamps for events. Weekends remain explicitly outside the cooking schedule initially.
- Specify the initial read contract and examples needed by today's viewer. Record later creation/regeneration semantics separately so unimplemented write routes are not advertised as available.
- Use one plan per household/calendar week and one current revision, following [the lifecycle proposal](#week-identity-regeneration-and-revisions). Carry identity, dates, and a revision field in initial responses without implementing history yet.
- Define validation, missing-data, and malformed-source errors now. Define stale revisions, infeasible plans, solver timeouts, and busy generation responses when adding writes in phase 3.
- Household authorization requirements for every application route, even though the first local development server binds only to loopback.

Record domain decisions in this document: the meaning of `WeeklyPlan`, `PlanRevision`, `MealAssignment`, `Recipe`, and a change proposal; the weekly plan aggregate boundary; and ownership of recipe catalog and shopping state. Describe initial generation, regeneration preview, acceptance, and conflicts with concrete examples. Establish invariants before choosing database tables: one saved plan per week, one current revision, dates within that week, and linked changes accepted together. Identify which rules belong to the domain and which guarantees also require persistence, such as concurrent creation of the same week.

Define the minimum internal interfaces for reading planner inputs and plan snapshots, separately from the HTTP contract. Specify the validated data they return so YAML, JSON, and future SQLite adapters can satisfy the same expectations. These internal interfaces do not require catalog API endpoints in milestone one.

Initial read API surface to refine in the contract:

| Capability | Candidate endpoint |
| --- | --- |
| Find available plans overlapping a range | `GET /api/v1/plans?from=...&to=...` (initially one configured fixture) |
| Read a saved week | `GET /api/v1/plans/{plan_id}` |
| Read its shopping list | `GET /api/v1/plans/{plan_id}/shopping-list?revision=...&snapshot_id=...` |

The plan response includes the recipe/ingredient details used by today's recipe modal. Separate catalog endpoints are added when a frontend feature needs them. An empty range response means no plan is available; a read never generates one.

Reserve the concepts needed for revisions, locks, recipe editing, and swaps, but do not implement or advertise unsupported routes. Decide whether initial generation can be synchronous after measuring runtime; do not add a job queue by default.

**Exit:** vocabulary, aggregate boundaries, and lifecycle examples are documented consistently with the week lifecycle above. The validated contract covers displaying the existing weekly plan, recipe details, distribution statistics, and groceries, including failure states. Future mutation behavior is defined without adding unimplemented write routes to the initial contract.

#### Phase 1 design decisions and deliverables

The machine-readable contract is [openapi.yaml](openapi.yaml), using OpenAPI 3.1.0. Complete response fixtures live in [examples/](examples/); this README remains the single narrative design document. The contract describes the API to implement in phase 2; it does not mean a server or routes exist yet. The example week beginning **2026-10-19** is an explicit demonstration anchor, not an inferred date or an application default.

Domain vocabulary and ownership:

| Concept | Definition and boundary |
| --- | --- |
| `PlanningWeek` | Monday start date plus household timezone; ends on the inclusive Sunday six days later. Identity uses the date, not a week number. |
| `WeeklyPlan` | Aggregate with stable identity, one planning week, and a current accepted revision. The database additionally guarantees uniqueness per household/week. |
| `PlanRevision` | Immutable accepted assignment snapshot plus generation/input provenance. Later accepted changes advance its integer revision. History is loaded separately when needed. |
| `MealAssignment` | Stable plan/date/meal slot with a selected base recipe, variant, resolved ingredient quantities, and any linked assignment. The selection changes across revisions; the slot identity does not. |
| `Recipe` | Catalog definition with stable ID, eligibility, ingredients, and carb strategy. A variant selects a carb option. Catalog edits do not rewrite accepted assignment snapshots. |
| `ChangeProposal` | Unaccepted candidate based on a specific revision and input snapshot. Acceptance validates it and saves the previewed assignments. This is a future write workflow, not an initial API resource. |
| `PlannerInputs` | Validated recipes, ingredients, ordered planning rules, and pantry exclusions supplied to generation/shopping services. It contains data and provenance, not filesystem or database handles. |
| `PlanSnapshot` | Coherent read value containing plan metadata, assignments, derived statistics, groceries, revision, and content token. This lets both read endpoints refer to the same data. |
| Shopping state | Required quantities derive from assignments and captured pantry exclusions. Purchased/checkmark state is separate; phase 2 keeps it in the browser. |

Invariants owned by the domain include unique date/meal assignments within a week, dates matching weekday labels, consistent quantity units, reciprocal links between same-day lunch/dinner assignments using the same base recipe, and the configured planning constraints. Linked meals can use different variants and portions. Weekly protein counts count both assignments; the current repetition rule treats an extendable recipe's linked pair as one day's use. Read APIs do not reinterpret a historical snapshot against today's mutable catalog or rules.

HTTP owns input parsing and error translation. Application services coordinate snapshot reads and future lifecycle operations. Domain logic decides whether a change is valid. Storage guarantees atomic publication/commits, uniqueness across plans, and expected-revision checks under concurrency. A domain check alone cannot prevent two requests from creating the same week.

**Minimum internal read interfaces for phase 2:**

```text
PlannerInputReader.load() -> PlannerInputs
PlanSnapshotReader.list_overlapping(from_date, to_date) -> sequence[PlanSummary]
PlanSnapshotReader.get_current(plan_id) -> PlanSnapshot

generate(inputs: PlannerInputs, seed) -> solution
calculate_shopping(solution, inputs: PlannerInputs) -> shopping
```

These are design signatures, not runtime interfaces implemented in phase 1. `PlannerInputs` holds ID-indexed validated definitions, rules preserving configured day/meal order, a set of pantry ingredient IDs, and source provenance/fingerprint. Canonicalize recipe and ingredient maps by ascending ID before solving; preserve configured day/meal order, recipe ingredient order, and allowed-carb list order. This matches sorted recipe filenames under the existing filename/ID validation rule and prevents database row order from changing solver inputs. Parsing YAML and checking filename conventions stay in the YAML adapter; referential and business validation is shared with the future database adapter. Existing Pydantic models can be reused where they express these data structures accurately.

`get_current` returns one coherent value; the shopping application service compares both expected `revision` and `snapshot_id` against that value before returning its groceries. A missing plan is distinct from an unavailable or invalid source. A range with no matching plans returns an empty list. Adapter errors are typed application errors, translated by Flask; they do not expose paths or SQL errors. The CLI calls generation with loaded inputs and handles file export outside core logic. Neither the solver nor shopping logic accepts `DataLoader`, file paths, Flask globals, or database sessions after phase 2.

**Read contract decisions:**

- JSON field names use `snake_case`. The frontend API client maps these responses into its components during phase 2. Domain/storage models need not match the wire representation.
- `from` and `to` are required real calendar dates in `YYYY-MM-DD`, both inclusive, with `from <= to` and at most 366 included days. A week matches when `week_start <= to` and `week_end >= from`. Results include entire weeks and are sorted by week start; a week crossing two months appears in both queries with the same plan ID. No pagination is needed for this bounded household query.
- Meal dates use the household timezone, initially `America/Bogota`. `generated_at` remains the source generation timestamp with a UTC offset; it never selects the week. Slots are ordered by date, then lunch before dinner. Current cooking days remain Monday–Friday, with informational weekend cards in the UI. Missing plans are not generated by reading them.
- IDs are opaque to clients. Plan IDs contain 1–128 ASCII letters, digits, underscores, or hyphens, starting with a letter or digit; malformed IDs return 400. Phase 2 assigns a stable ID to the configured week and stable assignment IDs for its date/meal slots. The examples use readable IDs for inspection, but clients must not parse them. Revisions start at 1.
- `snapshot_id` identifies the complete normalized plan/grocery content, including the week anchor. A changed file reload changes this token even though the development fixture still reports revision 1. It is not a revision number or a security credential. The sample digest recipe is documented in OpenAPI. If both the fixture content and anchor are unchanged, its token remains stable across restart.
- Shopping reads require both `revision` and `snapshot_id` from the detail response. A mismatch produces `409 snapshot_changed`; refetch the plan, then groceries. The initial API reads only current snapshots. Each endpoint returns internally coherent data even if another reload happens after the response; the frontend never combines different tokens and replaces plan/groceries together after a successful pair of reads.
- Quantity objects carry `value` and `unit` (`g`, `ml`, or `units`). Protein quantity means the raw protein ingredient amount, not nutritional protein grams. Resolve its unit from the captured ingredient definition; current `proteinQty` naming is misleading for eggs. Phase 2 must render units rather than append `g` unconditionally. Carb absence is `null`, with `carb_quantity: null`.
- Each linked pair has reciprocal `linked_assignment_id` values. `extended_from_lunch` is true only on its dinner assignment. The recipe modal receives the full resolved ingredient list in the plan; separate recipe/catalog endpoints are unnecessary for current UI parity.
- Responses use `Cache-Control: no-store`. Client grocery checkmarks are keyed by plan ID, revision, snapshot ID, and ingredient ID during phase 2. Theme preference remains independent.

Error envelope: `{ "error": { "code": "...", "message": "...", "details": [] } }`. Field errors use `{ "field": "from", "message": "..." }` entries. Messages must not expose internal paths, tracebacks, or credentials.

| HTTP status | Code | Meaning and frontend behavior |
| --- | --- | --- |
| 400 | `invalid_request` | Missing/malformed parameters or invalid date range; show input errors |
| 404 | `plan_not_found` | No plan with that ID; return to available plans |
| 409 | `snapshot_changed` | Shopping request references a different revision/token; reload the pair |
| 503 | `source_unavailable` | Required source cannot be read; show retry state |
| 503 | `source_invalid` | Source cannot form a coherent validated snapshot; show unavailable state, do not display partial data |

**Source consistency:** the current JSON files have no shared generation token. Schema validation alone cannot establish that they belong together. In phase 2, capture both files and the pantry exclusions for a load, verify shopping totals against the resolved assignment ingredient quantities (with the existing rounding and units), validate derived counts and links, then publish one immutable `PlanSnapshot`. Missing optional legacy link metadata can be normalized only when the paired recipe data confirms it; ambiguity fails validation. A changed load is published only after verification; if reload fails, return a source error until corrected rather than mixing old/new payloads. Introduce a shared manifest or an atomic export bundle when revising the exporter; atomic per-file writes alone do not make a two-file update atomic. Persist the validated snapshot's necessary input context on database import, never invent provenance absent from a legacy fixture.

**Household access:** the initial OpenAPI document explicitly models local development with no authentication (`security: []`) and a loopback server. This exception must be opt-in and confined to loopback in phase 2. Before any remote/shared-network deployment, every application route must enforce household access through the chosen private network policy or authenticated household membership. Membership comes from trusted access context, never a client-supplied household ID. Authentication transport, login endpoints, and related error responses will be added to the contract once the deployment access method is selected. No catalog or plan data is public by default.

**Frontend walkthrough and examples:**

1. Request `GET /api/v1/plans?from=2026-10-01&to=2026-10-31`; [plans.json](examples/plans.json) contains the example week. [plans-empty.json](examples/plans-empty.json) represents a range without a plan and displays an empty state.
2. Request `GET /api/v1/plans/plan_2026_10_19`; [plan.json](examples/plan.json) supplies dated cards, recipe details, distribution statistics, revision, and snapshot token. The sample's original April `generated_at` is deliberately retained while assigning an October week.
3. Request `GET /api/v1/plans/plan_2026_10_19/shopping-list?revision=1&snapshot_id=<URL-encoded token from step 2>`; [shopping-list.json](examples/shopping-list.json) supplies groceries. Confirm metadata matches before displaying it with the plan.
4. On `snapshot_changed`, clear the mismatched groceries and repeat steps 2–3. On unavailable/invalid source, show a retry state. On missing plan, refresh the list. Opening a recipe uses embedded assignment data and performs no catalog lookup.

The first contract intentionally contains no create, regenerate, accept, swap, history, or authentication routes. The lifecycle section above defines their future semantics; the first milestone remains focused on reading today's functionality through the API.

**Repeatable validation:** from the repository root, install the contract-only tooling in a virtual environment and run:

```sh
python3 -m venv .venv-contract
.venv-contract/bin/python -m pip install -r scripts/requirements-contract.txt
.venv-contract/bin/python scripts/validate_api_contract.py --require-openapi-validator --self-test
```

The validator checks the OpenAPI document, local references, JSON Schema definitions and response examples, then verifies dates, paired meals, revisions/tokens, normalized source parity, distribution counts, and pantry-adjusted grocery totals. Negative self-checks verify that date/day disagreement, a broken linked pair, a mismatched revision, an incorrect grocery quantity, and an invalid date format are rejected. Examples currently track `site/public/plan.json`, `site/public/shopping_list.json`, and `data/pantry.yml`; intentional source-fixture changes require updating the corresponding contract examples and token together.

Phase 1 verification passed using installed PyYAML/jsonschema and the [published OpenAPI 3.1 document schema](https://spec.openapis.org/oas/3.1/schema/2026-08-03.html), supplied with `--openapi-schema /tmp/meal-planner-openapi-schema.json`. That temporary downloaded schema is not a repository dependency. Component schemas and response examples were checked separately with JSON Schema Draft 2020-12 and date/time format checking. The current environment lacks pip and `openapi-spec-validator`, so its package-based path was not run here; the strict flag also accepts the supplied official-schema path. Running without either full-document validator emits an explicit warning, and the strict flag fails. Future runtime/HTTP conformance checks belong to phase 2.

### Phase 2 — Run the local web app against a file-backed API

- Add a Flask API whose storage adapter reads existing JSON plan/shopping output. Keep YAML and the existing solver available for CLI generation.
- Implement a YAML planner-input adapter behind a small interface for recipes, ingredients, rules, and pantry. Separate source-format checks from reusable business validation.
- Extract generation/shopping services from CLI orchestration and replace their `DataLoader` dependency with explicit validated input data. The CLI wires the YAML reader into those services and retains its generation/export workflow. Keep the solver algorithm unchanged.
- Bind the undated fixture to an explicitly configured Monday, with stable plan/assignment IDs, actual meal dates, and a baseline revision. Do not infer the intended week from `generated_at` or the date of a request.
- Serve a coherent plan/shopping snapshot. Validate both source files before making a replacement fixture visible; a failed or interrupted CLI run must not mix old groceries with a new plan. An explicit local reload/restart is acceptable for this milestone.
- Route frontend data access through an API client. Remove direct static plan/shopping fetches.
- Preserve the weekly cards, recipe modal, statistics, theme, and grocery checkmarks. Add loading/error handling and display the fixture's actual date range; scope browser-local grocery checks to the plan/revision so they do not carry over to a changed fixture.
- Keep browser generation, multiple saved weeks, shared checkmarks, history, calendar navigation, and swaps for subsequent milestones.
- Implement Flask routes calling application read services through a small storage interface, with the JSON adapter wired in `create_app`. Routes translate HTTP inputs/results; services coordinate reads; the adapter owns filesystem access. Domain concepts and rules must not depend on Flask globals or storage implementations.
- Implement the domain concepts needed for dated reads and existing generation inputs now. Keep mutation aggregates, revision repositories, and new generation workflows for phase 3.
- Supply one documented local startup workflow. Keep the API local-only until household authorization exists.

**Exit:** today's viewer works entirely through the API and matches the source files for assignments, quantities, recipe details, and statistics. Changing the API's fixture is reflected after explicit reload. API failure/empty states work, and a frontend/API integration check plus contract checks pass. The read service can be exercised without a Flask request context; generation/shopping can run on supplied data without YAML or filesystem access. Verify CLI results and grocery quantities against pre-refactor behavior with representative inputs, the same seed, and the same solver version. Storage access is confined to adapters and export code. No database or public deployment is needed.

### Phase 3 — Implement the plan domain and saved dated weeks

Complete the following subphases in order. Domain and application behavior comes first; persistence and HTTP integration follow. This is the main DDD implementation milestone.

#### Phase 3a — Implement and test the plan lifecycle

- Implement the weekly plan aggregate and its accepted revision behavior, following the [lifecycle rules](#week-identity-regeneration-and-revisions). Model assignments and proposal inputs explicitly while keeping historical snapshots outside the active aggregate's normal load.
- Implement domain policies for dates, linked assignments, valid generated plans, no-op changes, stale proposals, and protection of started weeks. Keep rule evaluation separate from Flask and database access.
- Implement application workflows for first generation, regeneration preview, discard, and acceptance. Coordinate repository reads and writes through explicit interfaces; cross-plan uniqueness and concurrent commits are enforced by the real database in phase 3b.
- Reuse the storage-independent generation/shopping services established in phase 2. Add plan lifecycle orchestration around them without reintroducing file access or changing the existing read interfaces solely to accommodate persistence.
- Test domain transitions using plain objects and application workflows with small in-memory test adapters. Cover initial revision 1, preview/discard leaving the current plan untouched, accepted changes preserving predecessors, rejection of stale proposals, and identical results producing no new revision.
- Test generation rules with representative inputs, including linked lunch/dinner assignments. Save actual assignments and input snapshots; a seed does not guarantee a distinct or reconstructible result.

**Exit:** plan lifecycle and application workflow checks pass without Flask or SQLite. Domain rules are identifiable in code and business decisions are not deferred to routes or SQL. Database concurrency guarantees remain to be verified in phase 3b.

#### Phase 3b — Persist the domain in SQLite

- Define migrations for ingredients, recipes and ingredient quantities, rules/pantry settings, plans, dated assignments, recipe versions or snapshots, and revisions.
- Use SQLite initially, with foreign keys, explicit transactions, and a persistent data location outside the application image/build.
- Import YAML and the configured dated fixture with validation and a repeatable import policy. Report conflicts; do not overwrite edits on subsequent startup.
- Make the database authoritative after import. YAML remains an import/export format rather than a second writable source of truth.
- Implement SQLite adapters for both phase-2 interfaces: planner inputs and plan reads. Wire them into the application while preserving existing read contracts, domain calculations, and frontend behavior.
- Add revision-write repository operations and the explicit transaction boundary required by the new application workflows. These extend the established read architecture.
- Verify adapter compatibility with shared representative datasets: imported YAML and SQLite inputs must represent the same recipes/rules/quantities, and imported JSON and database plan reads must yield equivalent application data. Confirm generation/shopping calculations agree for equivalent inputs and the same solver configuration.
- Enforce one plan per calendar week with a uniqueness constraint. Append immutable full-plan snapshots and move the current-revision pointer in one transaction, checking the expected revision at commit. Keep solving outside the transaction.
- Handle request retry identifiers atomically with committed mutations so repeated requests do not create duplicate plans/revisions. Persist proposal state as needed for the chosen acceptance flow and reject stale inputs.
- Store the recipe/rules/portion information needed to preserve saved plans when catalog data changes. Derive groceries from saved assignment data rather than mutable current recipes.
- Add database backup, restore, and export procedures locally before deployment.

**Exit:** the existing viewer works on database storage, both input/read adapters satisfy their established interfaces, and imports match their sources. The storage switch requires no changes to the read API contract, frontend, or solver rules. Database integration checks cover simultaneous creation for one week, stale acceptance, retries, and rollback after a failed write. Persisted plans survive restart, and a backup restores locally. The same domain behavior works through the real repository.

#### Phase 3c — Expose generation and revision workflows

- Extend OpenAPI with write contracts, error responses, retry identifiers, and expected-revision semantics before implementing the routes.
- Add Flask routes that delegate to the tested application workflows and map results/errors to HTTP. Keep domain decisions in the domain/application layers.
- Add browser week selection, generation, regeneration preview, acceptance/discard, and navigation among saved weeks. Handle stale proposals and failed generation without losing the current plan.
- Bound solver runtime and generation concurrency on the server. Prevent repeated submissions in the UI as an additional convenience.
- Add minimal revision history and restore only if needed now; preserving snapshots and conflict checks is required even if the history UI comes later.

**Exit for phase 3:** from the browser, generate two dated weeks, revisit them after restart, preview and discard a regeneration without changing the active plan, then accept a change and preserve its predecessor. Duplicate requests and stale updates are safe; failed writes do not leave partial plans. Contract, calculation, persistence, and frontend/API checks pass.

### Phase 4 — Build the calendar and shared household state

- Extend calendar queries over existing weekly plans. Keep recipe catalog edits and shopping checkmarks separate from meal-plan revisions; do not introduce a competing monthly aggregate for the calendar view.
- Add a monthly view over dated weekly plans, with drill-down to a week and clear unplanned dates.
- Fetch all weeks overlapping the selected month. A week spanning two months is one saved week, not two independently generated plans.
- Keep weekly protein/repetition rules scoped to their calendar week. Decide and document whether adjacency rules also apply across planning-week boundaries; the current weekday solver does not establish that behavior.
- Keep weekends visibly outside the cooking schedule unless explicitly changing that policy. Do not silently rescale exact weekly counts for skipped meals or partial weeks.
- Support weekly shopping ranges first; define custom/monthly aggregation separately if needed.
- Persist grocery checkmarks per shopping list/period and ingredient. Decide how quantity increases after edits affect purchased status rather than carrying checks across unrelated weeks.
- Add minimal recipe editing through the API/UI if required for household operation; historical assignments retain their saved recipe version/snapshot.
- Reuse revision checks from phase 3 so calendar actions cannot silently overwrite changes from another device.

**Exit:** navigation works across month/year boundaries, including a month intersecting six weeks. Shared grocery state survives reloads and appears on another client. Recipe edits do not change old saved plans. Stale updates produce a recoverable conflict.

### Phase 5 — Add safe recipe replacements

Initial meaning of “swap”: replace the recipe/variant in a dated assignment, not exchange two scheduled meals. Exchanging meals can be a later action.

- Extend weekly-plan domain policies with replacement eligibility and locks, and add application workflows for replacement preview, acceptance, and undo. Test these rules independently before wiring API and storage changes; reuse the existing revision commit boundary.
- Define contract additions for discovering alternatives, previewing a replacement, and committing it against an expected plan revision.
- Validate meal eligibility, carbs, exact protein counts, weekly recipe limits, and applicable adjacency rules.
- Account for linked lunch/dinner recipes: preview all affected assignments and apply or reject the linked change together.
- Keep unrelated assignments unchanged. Return a useful reason when there is no valid single replacement.
- Recompute grocery totals and distribution statistics on successful changes, using the same committed plan revision.
- Reuse plan revisions from phase 3 for change history and undo. Each accepted replacement (including linked meals) creates one revision; undo appends a revision rather than deleting history. Reject stale previews and stale undo attempts.
- Add locked assignments before offering regeneration that could replace manually chosen meals.

**Exit:** a compatible replacement persists and updates groceries; invalid or stale replacements leave the plan intact; linked changes are atomic; undo restores the previous assignment set without overwriting later edits.

Later extension: “rebalance this week” may propose changes to other unlocked meals. Show the complete diff and require explicit acceptance. Whole-month generation and cross-week variety optimization are also separate extensions, not prerequisites for the monthly calendar.

### Phase 6 — Prepare and deploy to Lightsail

- Package the frontend and API reproducibly; use one instance and a persistent SQLite location. Start with one application process and bounded solver concurrency.
- Measure memory and runtime for representative generation and replacement requests before choosing instance size.
- Choose access explicitly: private access through Tailscale, or HTTPS with household-only authentication. No public registration; authorization is enforced server-side. Protect application data, not just the generation button.
- Configure secrets, restricted inbound access, process restart, health checks, limited log retention, and an update/rollback procedure.
- Review the full expected bill: instance, snapshots, independent database backups, transfer overages, DNS/domain if used, and any optional services. Avoid autoscaling and unnecessary metered dependencies. Budget alerts are notifications, not hard spending caps.
- Back up the live database with a SQLite-consistent method, keep a copy independent of the instance, define retention, and test recovery. VM snapshots alone do not replace a database restore test.
- Verify schema migration and rollback compatibility so a failed deployment does not destroy saved plans.

**Exit:** household devices can use the deployed app; unauthorized clients cannot access data or invoke mutations; data survives redeployment/restart; a backup restores to a clean instance; expected costs and remaining variable charges are documented.

Deployment can move earlier, after phase 3, if remote household testing becomes useful. Complete the same access, cost, and recovery requirements before that earlier launch; calendar and swaps do not have to block deployment.

## Decisions to settle as work reaches them

| Decision | Proposed default | Needed by |
| --- | --- | --- |
| Household timezone | `America/Bogota`, confirm household preference | Phase 1 |
| Week definition | Monday–Sunday; cooking on configured weekdays | Phase 1 |
| Storage | SQLite with migrations | Phase 3 |
| Week identity/history | One plan per calendar week; immutable accepted revisions | Contract in phase 1, writes in phase 3 |
| Recipe changes | Preserve saved versions/snapshots | Phase 3 |
| Cross-week adjacency | Explicit policy; no assumed new constraint | Phase 4 |
| Replacement behavior | Preserve unrelated meals; explain conflicts | Phase 5 |
| Remote access | Choose private Tailscale or household login over HTTPS | Before deployment |
| Hosting budget | Confirm full base cost and acceptable variable charges | Before deployment |

## Immediate next milestone

Phase 1 is complete. Continue with phase 2: implement the agreed read contract in Flask, connect today's frontend to it, and put YAML planner inputs and JSON plan reads behind the defined interfaces. Decouple CLI generation from file loading while preserving its calculations. Phase 3 then implements new lifecycle behavior, connects SQLite through those interfaces, and exposes generation/revision workflows. Cloud provisioning remains a separate milestone.
