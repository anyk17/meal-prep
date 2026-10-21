# Architecture

The planner (`planner/mealplanner/`) is a layered, ports-and-adapters design. Dependencies point inward; the rule is enforced by `planner/tests/test_architecture.py`.

## Layers and dependency rule

| Layer | Purpose | May import |
|---|---|---|
| `domain/` | Pure business model: catalog, variants, input policies, plan snapshot, WeeklyPlan records. stdlib + pydantic only; no I/O, Flask, sqlite3, yaml, ortools, file paths. | `domain` |
| `solver/` | OR-Tools encoding of the domain policies (`model.py`, `solve.py`). | `domain` |
| `application/` | Use cases and ports: generation, shopping list, plan output DTOs, read service, WeeklyPlan workflows. | `domain`, `solver` |
| `adapters/` | Implementations of the ports: YAML inputs, JSON snapshot, in-memory, SQLite, output files. | `domain`, `application` (ports, errors, DTOs) |
| `entrypoints/` | CLIs and the Flask app; compose adapters into services. | anything |

```mermaid
flowchart LR
  subgraph entrypoints
    CLI[cli.py]
    DBCLI[db_cli.py]
    HTTP[http/app.py<br/>read_routes, write_routes]
  end
  subgraph adapters
    YAML[yaml_inputs]
    JSON[json_snapshot]
    MEM[memory]
    SQL[sqlite/store, migrations]
    FILES[files]
  end
  subgraph application
    PORTS{{ports.py<br/>PlannerInputReader<br/>PlanReadRepository<br/>PlanWorkflowRepository}}
    SVC[generation, shopping, plan_output<br/>plan_reads, plan_workflows]
  end
  SOLVER[solver]
  DOMAIN[domain]
  CLI --> SVC
  DBCLI --> SQL
  HTTP --> SVC
  entrypoints -.wires.-> adapters
  YAML -. implements .-> PORTS
  JSON -. implements .-> PORTS
  MEM -. implements .-> PORTS
  SQL -. implements .-> PORTS
  SVC --> PORTS
  SVC --> SOLVER --> DOMAIN
  SVC --> DOMAIN
  adapters --> DOMAIN
```

## Where key concepts live

- **WeeklyPlan lifecycle**: records, errors, diff and linked-pair helpers in `domain/weekly_plan.py`; create / propose / accept / discard / undo in `application/plan_workflows.py`; persistence port `PlanWorkflowRepository`, implemented by `adapters/sqlite/store.py` and `adapters/memory.py`.
- **PlanSnapshot**: `domain/snapshot.py` holds the immutable plan + shopping payload, `canonical_snapshot_id`, `validate_snapshot`, and `normalize_snapshot` (legacy weekday JSON to dated snapshot).
- **Solver policies**: input/business rules in `domain/policies.py`; their OR-Tools encoding in `solver/model.py` and `solver/solve.py`.
- **Planner inputs**: `domain/planner_inputs.py` (value object); read through the `PlannerInputReader` port by `adapters/yaml_inputs.py` or `adapters/sqlite/store.py`.
- **Read / write HTTP**: `entrypoints/http/read_routes.py` and `write_routes.py` only parse, map errors and serialize; rules live in `application/plan_reads.py` and `plan_workflows.py`. `app.py` is the factory (`create_app`) and dev server (`python -m mealplanner.entrypoints.http.app`).
- **CLIs**: `python -m mealplanner.entrypoints.cli` (also the `mealplanner` script) and `python -m mealplanner.entrypoints.db_cli` (import, backup, restore).

## Where do I put new code?

| New code | Goes in |
|---|---|
| A business rule on recipes, ingredients or inputs | `domain/policies.py` (and its solver encoding in `solver/`) |
| A new plan record, value object or domain error | `domain/` |
| A new use case or orchestration step | `application/` |
| A new storage or input boundary | Protocol in `application/ports.py`, implementation in `adapters/` |
| SQL, YAML, file or network access | `adapters/` |
| A new HTTP route or CLI command | `entrypoints/` (delegate to an application service) |
| Wiring adapters to services | `entrypoints/` |
