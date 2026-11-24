from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.server.accounts import AccountError, AccountService
from dagrunner.server.auth import (
    AuthService,
    client_password_hash,
    ensure_admin_account,
    store_password,
)
from dagrunner.database import StateDatabase
from dagrunner.server.webapp import create_app


ADMIN_PASSWORD = "admin-password-1234"
USER_PASSWORD = "user-password-123456"


def definition(name: str) -> str:
    return f"name: {name}\ntasks:\n  a:\n    command: echo a\n"


class AccountServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.database = StateDatabase(Path(self._tmp.name) / "scheduler.db")
        self.service = AccountService(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_legacy_users_keep_full_workflow_access(self) -> None:
        self.database.upsert_user("legacy", "hash")
        user = self.database.get_user("legacy")
        self.assertTrue(user["unrestricted"])
        self.assertTrue(self.service.is_unrestricted("legacy"))
        self.assertTrue(self.service.can("legacy", "anything", "edit"))

    def test_new_accounts_are_restricted_by_default(self) -> None:
        self.database.create_user("fresh", "hash")
        user = self.database.get_user("fresh")
        self.assertFalse(user["unrestricted"])
        self.assertFalse(self.service.can("fresh", "anything", "read"))

    def test_grants_control_workflow_access(self) -> None:
        store_password(self.database, "admin", ADMIN_PASSWORD)
        key = self.database.create_workflow("One", definition("One"))["workflow_key"]
        store_password(self.database, "bob", USER_PASSWORD, unrestricted=False)
        self.service.set_grants("bob", {key: "execute"})
        self.assertTrue(self.service.can("bob", key, "read"))
        self.assertTrue(self.service.can("bob", key, "execute"))
        self.assertFalse(self.service.can("bob", key, "edit"))
        self.assertFalse(self.service.can_create("bob"))
        self.assertEqual(self.service.visible_workflows("bob"), {key})

    def test_edit_grant_allows_create(self) -> None:
        key = self.database.create_workflow("One", definition("One"))["workflow_key"]
        store_password(self.database, "bob", USER_PASSWORD, unrestricted=False)
        self.service.set_grants("bob", {key: "edit"})
        self.assertTrue(self.service.can_create("bob"))

    def test_set_grants_rejects_unknown_workflow(self) -> None:
        store_password(self.database, "bob", USER_PASSWORD)
        with self.assertRaises(AccountError):
            self.service.set_grants("bob", {"workflow_missing": "read"})

    def test_admin_is_username_based(self) -> None:
        store_password(self.database, "admin", ADMIN_PASSWORD)
        self.assertTrue(self.service.is_admin("admin"))
        self.assertFalse(self.service.is_admin("bob"))

    def test_ensure_admin_account_runs_once(self) -> None:
        password = ensure_admin_account(self.database)
        self.assertIsNotNone(password)
        self.assertEqual(ensure_admin_account(self.database), None)

    def test_auth_context_loads_grants_in_one_query(self) -> None:
        store_password(self.database, "bob", USER_PASSWORD, unrestricted=False)
        key = self.database.create_workflow("One", definition("One"))["workflow_key"]
        self.database.set_user_workflow("bob", key, "execute")
        service = AuthService(self.database)
        result = service.login("bob", client_password_hash(USER_PASSWORD), "127.0.0.1")
        self.assertEqual(result.status, "success")
        context = service.context(result.token)
        self.assertIsNotNone(context)
        self.assertEqual(context["username"], "bob")
        self.assertFalse(context["unrestricted"])
        self.assertEqual(context["permissions"], {key: "execute"})

    def test_delete_user_cascades_grants(self) -> None:
        key = self.database.create_workflow("One", definition("One"))["workflow_key"]
        store_password(self.database, "bob", USER_PASSWORD)
        self.service.set_grants("bob", {key: "read"})
        self.service.delete_account("bob")
        self.assertIsNone(self.database.get_user("bob"))
        self.assertEqual(self.database.list_user_workflows("bob"), [])

    def test_delete_admin_is_rejected(self) -> None:
        store_password(self.database, "admin", ADMIN_PASSWORD)
        with self.assertRaises(AccountError):
            self.service.delete_account("admin")

    def test_admin_cannot_be_disabled(self) -> None:
        store_password(self.database, "admin", ADMIN_PASSWORD)
        with self.assertRaises(AccountError):
            self.service.update_account("admin", enabled=False)


class AccountApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database = StateDatabase(root / "scheduler.db")
        store_password(self.database, "admin", ADMIN_PASSWORD, unrestricted=True)
        store_password(self.database, "bob", USER_PASSWORD, unrestricted=False)
        store_password(self.database, "vera", USER_PASSWORD, unrestricted=False)
        self.key_one = self.database.create_workflow("One", definition("One"))["workflow_key"]
        self.key_two = self.database.create_workflow("Two", definition("Two"))["workflow_key"]
        self.database.set_user_workflow("bob", self.key_one, "execute")
        self.database.set_user_workflow("vera", self.key_two, "read")
        self.app = create_app(
            database_path=str(self.database.path),
            logs_path=root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
        )

    def tearDown(self) -> None:
        self.app.extensions["schedule_service"].shutdown()
        self.app.extensions["execution_service"].shutdown()
        self._tmp.cleanup()

    def _login(self, client, username: str, password: str):
        response = client.post(
            "/api/auth/login",
            json={
                "username": username,
                "password_hash": client_password_hash(password),
            },
        )
        self.assertEqual(response.status_code, 200, response.data)

    def _wait_run(self, run_id: str):
        import time

        run = None
        for _ in range(100):
            run = self.database.get_run(run_id)
            if run is not None and run["status"] in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.1)
        return run

    def test_workflow_list_is_filtered_by_permission(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            data = client.get("/api/workflows").get_json()
            self.assertEqual([item["name"] for item in data["workflows"]], [self.key_one])
            self.assertEqual(data["workflows"][0]["permission"], "execute")

    def test_invisible_workflow_is_not_found(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            self.assertEqual(client.get(f"/api/workflows/{self.key_two}/view").status_code, 404)

    def test_execute_and_edit_are_separate(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            response = client.post(f"/api/workflows/{self.key_one}/run", json={})
            self.assertEqual(response.status_code, 202)
            self.assertIsNotNone(self._wait_run(response.get_json()["run_id"]))
            edit_response = client.put(
                f"/api/workflows/{self.key_one}",
                json={"definition": definition("One")},
            )
            self.assertEqual(edit_response.status_code, 403)

    def test_read_only_cannot_run(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "vera", USER_PASSWORD)
            response = client.post(f"/api/workflows/{self.key_two}/run", json={})
            self.assertEqual(response.status_code, 403)

    def test_admin_only_endpoints(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            self.assertEqual(client.get("/api/accounts").status_code, 403)
            self.assertEqual(client.get("/api/notifier").status_code, 403)
            self.assertEqual(client.get("/api/satellites").status_code, 403)
            self.assertEqual(client.get("/api/update/status").status_code, 403)

    def test_admin_manages_accounts(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "admin", ADMIN_PASSWORD)
            created = client.post(
                "/api/accounts",
                json={
                    "username": "carol",
                    "password": "carol-password-1234",
                    "enabled": True,
                    "workflows": {self.key_one: "edit"},
                },
            )
            self.assertEqual(created.status_code, 201, created.data)
            accounts = {
                item["username"]: item
                for item in client.get("/api/accounts").get_json()["accounts"]
            }
            self.assertIn("carol", accounts)
            self.assertEqual(accounts["carol"]["workflows"][0]["permission"], "edit")

            updated = client.put(
                f"/api/accounts/carol", json={"enabled": False, "workflows": {}}
            )
            self.assertEqual(updated.status_code, 200)
            accounts = {
                item["username"]: item
                for item in client.get("/api/accounts").get_json()["accounts"]
            }
            self.assertFalse(accounts["carol"]["enabled"])
            self.assertEqual(accounts["carol"]["workflows"], [])

            reset = client.post(
                "/api/accounts/carol/password",
                json={"password": "carol-new-password-1234"},
            )
            self.assertEqual(reset.status_code, 200)
            deleted = client.delete("/api/accounts/carol")
            self.assertEqual(deleted.status_code, 200)

    def test_admin_cannot_delete_self(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "admin", ADMIN_PASSWORD)
            response = client.delete("/api/accounts/admin")
            self.assertEqual(response.status_code, 400)

    def test_change_password(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            wrong = client.post(
                "/api/auth/change-password",
                json={
                    "old_password_hash": client_password_hash("wrong-password-12345"),
                    "new_password": "bob-new-password-1234",
                },
            )
            self.assertEqual(wrong.status_code, 400)
            ok = client.post(
                "/api/auth/change-password",
                json={
                    "old_password_hash": client_password_hash(USER_PASSWORD),
                    "new_password": "bob-new-password-1234",
                },
            )
            self.assertEqual(ok.status_code, 200, ok.data)
            # The old session was cleared by the password change.
            self.assertEqual(client.get("/api/workflows").status_code, 401)

    def test_manual_run_records_trigger_user(self) -> None:
        with self.app.test_client() as client:
            self._login(client, "bob", USER_PASSWORD)
            run_id = client.post(
                f"/api/workflows/{self.key_one}/run", json={}
            ).get_json()["run_id"]
            run = self._wait_run(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run["trigger_user"], "bob")


if __name__ == "__main__":
    unittest.main()
