"""Read-service and HTTP contract tests; run with unittest, no pytest required."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "planner"))

from mealplanner.entrypoints.http.app import create_app
from mealplanner.adapters.json_snapshot import JsonPlanReadRepository
from mealplanner.application.errors import InvalidRequest
from mealplanner.application.plan_reads import PlanReadService
from mealplanner.domain.snapshot import PlanSnapshot


def source_repository() -> JsonPlanReadRepository:
    return JsonPlanReadRepository.from_files(
        REPO_ROOT / "site/public/plan.json",
        REPO_ROOT / "site/public/shopping_list.json",
        REPO_ROOT / "data/pantry.yml",
        date(2026, 10, 19),
    )


class SnapshotAndServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = source_repository()
        cls.plan = cls.repository.get_plan("plan_2026_10_19")
        cls.shopping = cls.repository.get_shopping_list("plan_2026_10_19")

    def test_source_files_normalize_to_checked_in_api_examples(self) -> None:
        expected_plan = json.loads(
            (REPO_ROOT / "docs/migration-to-cloud/examples/plan.json").read_text()
        )
        expected_shopping = json.loads(
            (REPO_ROOT / "docs/migration-to-cloud/examples/shopping-list.json").read_text()
        )
        self.assertEqual(self.plan, expected_plan)
        self.assertEqual(self.shopping, expected_shopping)

    def test_read_service_is_flask_independent_and_returns_copies(self) -> None:
        service = PlanReadService(self.repository)
        result = service.list_plans(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(result["items"][0]["plan_id"], "plan_2026_10_19")
        returned = service.get_plan("plan_2026_10_19")
        returned["slots"].clear()
        self.assertEqual(len(service.get_plan("plan_2026_10_19")["slots"]), 10)
        with self.assertRaises(InvalidRequest):
            service.list_plans(date(2026, 1, 1), date(2027, 1, 2))

    def test_repository_is_immutable_after_source_files_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "plan.json"
            shopping_path = tmp_path / "shopping.json"
            pantry_path = tmp_path / "pantry.yml"
            plan_path.write_text((REPO_ROOT / "site/public/plan.json").read_text())
            shopping_path.write_text((REPO_ROOT / "site/public/shopping_list.json").read_text())
            pantry_path.write_text((REPO_ROOT / "data/pantry.yml").read_text())
            repository = JsonPlanReadRepository.from_files(
                plan_path, shopping_path, pantry_path, date(2026, 10, 19)
            )
            before = repository.get_plan("plan_2026_10_19")
            plan_path.write_text("not valid json")
            self.assertEqual(repository.get_plan("plan_2026_10_19"), before)

    def test_repository_copies_caller_owned_snapshots(self) -> None:
        plan = json.loads(json.dumps(self.plan))
        shopping = json.loads(json.dumps(self.shopping))
        repository = JsonPlanReadRepository([PlanSnapshot(plan, shopping)])
        plan["slots"].clear()
        shopping["sections"].clear()
        self.assertEqual(len(repository.get_plan("plan_2026_10_19")["slots"]), 10)
        self.assertTrue(repository.get_shopping_list("plan_2026_10_19")["sections"])


class FlaskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.repository = source_repository()
            cls.plan = cls.repository.get_plan("plan_2026_10_19")
            cls.app = create_app(repository=cls.repository)
        except RuntimeError as exc:
            if "install mealplanner[api]" in str(exc):
                raise unittest.SkipTest(str(exc)) from exc
            raise
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        cls.spec = yaml.safe_load((REPO_ROOT / "docs/migration-to-cloud/openapi.yaml").read_text())

    def assert_schema(self, body: dict[str, Any], schema_name: str) -> None:
        schema = self.spec["components"]["schemas"][schema_name]
        resolver = RefResolver.from_schema(self.spec)
        Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker()).validate(
            body
        )

    def assert_no_store(self, response: Any) -> None:
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_plan_and_list_responses_match_openapi_schemas(self) -> None:
        plan_response = self.client.get("/api/v1/plans/plan_2026_10_19")
        self.assertEqual(plan_response.status_code, 200)
        self.assert_no_store(plan_response)
        self.assert_schema(plan_response.get_json(), "Plan")

        list_response = self.client.get("/api/v1/plans?from=2026-10-01&to=2026-10-31")
        self.assertEqual(list_response.status_code, 200)
        self.assert_no_store(list_response)
        self.assert_schema(list_response.get_json(), "PlanList")
        self.assertEqual(len(list_response.get_json()["items"]), 1)

        shopping_response = self.client.get(
            "/api/v1/plans/plan_2026_10_19/shopping-list",
            query_string={"revision": 1, "snapshot_id": self.plan["snapshot_id"]},
        )
        self.assertEqual(shopping_response.status_code, 200)
        self.assert_no_store(shopping_response)
        self.assert_schema(shopping_response.get_json(), "ShoppingList")

    def test_inclusive_full_week_overlap_and_empty_results(self) -> None:
        overlap = self.client.get("/api/v1/plans?from=2026-10-25&to=2026-10-25")
        self.assertEqual(overlap.status_code, 200)
        self.assertEqual(len(overlap.get_json()["items"]), 1)
        empty = self.client.get("/api/v1/plans?from=2026-10-26&to=2026-10-26")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.get_json(), {"items": []})
        max_range = self.client.get("/api/v1/plans?from=2026-01-01&to=2027-01-02")
        self.assertEqual(max_range.status_code, 400)
        self.assert_no_store(max_range)

    def test_invalid_dates_ranges_ids_and_revisions_return_contract_errors(self) -> None:
        cases = [
            "/api/v1/plans?to=2026-10-31",
            "/api/v1/plans?from=2026-02-30&to=2026-03-01",
            "/api/v1/plans?from=2026-10-02&to=2026-10-01",
            "/api/v1/plans?from=2026-10-01&to=2027-10-02",
            "/api/v1/plans/bad.id",
            "/api/v1/plans/plan_2026_10_19/shopping-list?revision=0&snapshot_id=x",
            "/api/v1/plans/plan_2026_10_19/shopping-list?revision=one&snapshot_id=x",
            "/api/v1/plans/plan_2026_10_19/shopping-list?revision=1",
        ]
        for path in cases:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 400)
                self.assert_no_store(response)
                body = response.get_json()
                self.assertEqual(body["error"]["code"], "invalid_request")
                self.assertEqual(set(body["error"]), {"code", "message", "details"})
                self.assert_schema(body, "ErrorResponse")

        reversed_range = self.client.get("/api/v1/plans?from=2026-10-02&to=2026-10-01")
        self.assertEqual(reversed_range.status_code, 400)
        self.assertEqual(reversed_range.get_json()["error"]["details"][0]["field"], "to")

    def test_unknown_plan_and_stale_snapshot(self) -> None:
        missing = self.client.get("/api/v1/plans/plan_missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"]["code"], "plan_not_found")
        self.assert_schema(missing.get_json(), "ErrorResponse")
        self.assert_no_store(missing)

        stale = self.client.get(
            "/api/v1/plans/plan_2026_10_19/shopping-list?revision=1&snapshot_id=stale"
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["error"]["code"], "snapshot_changed")
        self.assert_schema(stale.get_json(), "ErrorResponse")
        self.assert_no_store(stale)

    def test_startup_snapshot_is_immutable_after_source_files_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "plan.json"
            shopping_path = tmp_path / "shopping.json"
            pantry_path = tmp_path / "pantry.yml"
            plan_path.write_text((REPO_ROOT / "site/public/plan.json").read_text())
            shopping_path.write_text((REPO_ROOT / "site/public/shopping_list.json").read_text())
            pantry_path.write_text((REPO_ROOT / "data/pantry.yml").read_text())
            app = create_app(
                plan_path=plan_path, shopping_path=shopping_path, pantry_path=pantry_path
            )
            app.config["TESTING"] = True
            client = app.test_client()
            before = client.get("/api/v1/plans/plan_2026_10_19").get_json()
            plan_path.write_text("not valid json")
            after = client.get("/api/v1/plans/plan_2026_10_19").get_json()
            self.assertEqual(before, after)

    def test_missing_or_incoherent_sources_return_safe_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_app = create_app(
                plan_path=tmp_path / "missing-plan.json",
                shopping_path=tmp_path / "missing-shopping.json",
                pantry_path=tmp_path / "missing-pantry.yml",
            )
            missing_app.config["TESTING"] = True
            missing = missing_app.test_client().get("/api/v1/plans?from=2026-10-01&to=2026-10-31")
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.get_json()["error"]["code"], "source_unavailable")
            self.assert_schema(missing.get_json(), "ErrorResponse")
            self.assertNotIn(tmp, missing.get_data(as_text=True))
            self.assert_no_store(missing)

            plan_path = tmp_path / "plan.json"
            shopping_path = tmp_path / "shopping.json"
            pantry_path = tmp_path / "pantry.yml"
            plan_path.write_text((REPO_ROOT / "site/public/plan.json").read_text())
            corrupt = json.loads((REPO_ROOT / "site/public/shopping_list.json").read_text())
            corrupt["sections"]["protein"][0]["quantity"] += 1
            shopping_path.write_text(json.dumps(corrupt))
            pantry_path.write_text((REPO_ROOT / "data/pantry.yml").read_text())
            invalid_app = create_app(
                plan_path=plan_path, shopping_path=shopping_path, pantry_path=pantry_path
            )
            invalid_app.config["TESTING"] = True
            invalid = invalid_app.test_client().get("/api/v1/plans/plan_2026_10_19")
            self.assertEqual(invalid.status_code, 503)
            self.assertEqual(invalid.get_json()["error"]["code"], "source_invalid")
            self.assert_schema(invalid.get_json(), "ErrorResponse")
            self.assertNotIn(str(plan_path), invalid.get_data(as_text=True))
            self.assert_no_store(invalid)

    def test_unsupported_source_ingredient_role_returns_safe_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_path = tmp_path / "plan.json"
            shopping_path = tmp_path / "shopping.json"
            pantry_path = tmp_path / "pantry.yml"
            source = json.loads((REPO_ROOT / "site/public/plan.json").read_text())
            source["slots"][0]["ingredients"][1]["role"] = "not-a-contract-role"
            plan_path.write_text(json.dumps(source))
            shopping_path.write_text((REPO_ROOT / "site/public/shopping_list.json").read_text())
            pantry_path.write_text((REPO_ROOT / "data/pantry.yml").read_text())
            app = create_app(
                plan_path=plan_path, shopping_path=shopping_path, pantry_path=pantry_path
            )
            app.config["TESTING"] = True
            response = app.test_client().get("/api/v1/plans/plan_2026_10_19")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["error"]["code"], "source_invalid")
            self.assert_schema(response.get_json(), "ErrorResponse")
            self.assert_no_store(response)

    def test_loopback_binding_is_enforced_by_development_entrypoint(self) -> None:
        from unittest.mock import patch

        from mealplanner.entrypoints.http import app as api

        with (
            patch.dict("os.environ", {"MEALPLANNER_HOST": "0.0.0.0"}),
            self.assertRaises(SystemExit),
        ):
            api.main()

    def test_api_rejects_non_loopback_remote_addresses(self) -> None:
        response = self.client.get(
            "/api/v1/plans/plan_2026_10_19",
            environ_base={"REMOTE_ADDR": "192.168.1.20"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_request")
        self.assert_no_store(response)

    def test_extremely_large_revision_is_a_conflict_not_server_error(self) -> None:
        response = self.client.get(
            "/api/v1/plans/plan_2026_10_19/shopping-list",
            query_string={"revision": "9" * 5000, "snapshot_id": self.plan["snapshot_id"]},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "snapshot_changed")
        self.assert_no_store(response)


if __name__ == "__main__":
    unittest.main()
