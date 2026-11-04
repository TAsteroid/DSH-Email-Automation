"""Address book and template tests: CRUD, validation, protocol shapes."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from mail_core.protocol import MailCoreError
from mail_core.service import Service
from tests.support import workspace_temporary_directory

CONTACT_KEYS = {
    "id",
    "name",
    "email",
    "phone",
    "company",
    "title",
    "notes",
    "createdAt",
    "updatedAt",
}

TEMPLATE_KEYS = {"id", "name", "subject", "body", "createdAt", "updatedAt"}


class ContactTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _dispatch(self, method: str, params: dict):
        return self.service.dispatch(method, params)

    def _add(self, **overrides) -> str:
        params = {"name": "张三"}
        params.update(overrides)
        return self._dispatch("contact.add", params)["id"]

    def test_add_and_list_roundtrip(self):
        contact_id = self._add(
            name="张三",
            email="zhang@example.com",
            phone="13800000000",
            company="示例公司",
            title="经理",
            notes="老客户",
        )
        self.assertTrue(contact_id.startswith("con_"))

        contacts = self._dispatch("contact.list", {})["contacts"]
        self.assertEqual(len(contacts), 1)
        contact = contacts[0]
        self.assertEqual(set(contact), CONTACT_KEYS)
        self.assertEqual(contact["id"], contact_id)
        self.assertEqual(contact["name"], "张三")
        self.assertEqual(contact["email"], "zhang@example.com")
        self.assertEqual(contact["phone"], "13800000000")
        self.assertEqual(contact["company"], "示例公司")
        self.assertEqual(contact["title"], "经理")
        self.assertEqual(contact["notes"], "老客户")
        self.assertTrue(contact["createdAt"])
        self.assertEqual(contact["createdAt"], contact["updatedAt"])
        json.dumps(contacts, ensure_ascii=False)

    def test_optional_fields_default_to_empty_strings(self):
        self._add(name="李四")
        contact = self._dispatch("contact.list", {})["contacts"][0]
        self.assertEqual(
            [contact[key] for key in ("email", "phone", "company", "title", "notes")],
            ["", "", "", "", ""],
        )

    def test_list_is_sorted_by_name(self):
        for name in ("Carol", "Alice", "Bob"):
            self._add(name=name)
        names = [item["name"] for item in self._dispatch("contact.list", {})["contacts"]]
        self.assertEqual(names, ["Alice", "Bob", "Carol"])

    def test_update_changes_only_given_fields(self):
        contact_id = self._add(name="张三", email="old@example.com", phone="111")
        self.assertEqual(
            self._dispatch(
                "contact.update", {"id": contact_id, "title": "总监", "notes": "已跟进"}
            ),
            {"updated": True},
        )

        contact = self._dispatch("contact.list", {})["contacts"][0]
        self.assertEqual(contact["title"], "总监")
        self.assertEqual(contact["notes"], "已跟进")
        self.assertEqual(contact["name"], "张三")
        self.assertEqual(contact["email"], "old@example.com")
        self.assertEqual(contact["phone"], "111")

    def test_update_unknown_contact_is_not_found(self):
        with self.assertRaises(MailCoreError) as context:
            self._dispatch("contact.update", {"id": "con_missing", "email": "x@y.com"})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_update_rejects_blank_or_non_string_name(self):
        contact_id = self._add()
        for value in ("", "   ", 123):
            with self.subTest(value=value):
                with self.assertRaises(MailCoreError) as context:
                    self._dispatch("contact.update", {"id": contact_id, "name": value})
                self.assertEqual(context.exception.code, "BAD_REQUEST")

    def test_remove(self):
        contact_id = self._add()
        self.assertEqual(
            self._dispatch("contact.remove", {"id": contact_id}), {"removed": True}
        )
        self.assertEqual(self._dispatch("contact.list", {})["contacts"], [])
        self.assertIsNone(self.service.store.get_contact(contact_id))

        with self.assertRaises(MailCoreError) as context:
            self._dispatch("contact.remove", {"id": contact_id})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_add_requires_non_empty_name(self):
        cases = (
            ("missing", {}),
            ("empty", {"name": ""}),
            ("blank", {"name": "   "}),
            ("wrong type", {"name": 123}),
        )
        for label, params in cases:
            with self.subTest(case=label):
                with self.assertRaises(MailCoreError) as context:
                    self._dispatch("contact.add", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertEqual(self._dispatch("contact.list", {})["contacts"], [])

    def test_optional_fields_must_be_strings(self):
        with self.assertRaises(MailCoreError) as context:
            self._dispatch("contact.add", {"name": "张三", "email": 42})
        self.assertEqual(context.exception.code, "BAD_REQUEST")


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = workspace_temporary_directory()
        self.addCleanup(self._tmp.cleanup)
        self.service = Service(Path(self._tmp.name))
        self.addCleanup(self.service.close)

    def _dispatch(self, method: str, params: dict):
        return self.service.dispatch(method, params)

    def _add(self, **overrides) -> str:
        params = {"name": "报价"}
        params.update(overrides)
        return self._dispatch("template.add", params)["id"]

    def test_add_and_list_roundtrip(self):
        template_id = self._add(
            name="报价",
            subject="报价单 - {{客户}}",
            body="您好，报价见附件。",
        )
        self.assertTrue(template_id.startswith("tpl_"))

        templates = self._dispatch("template.list", {})["templates"]
        self.assertEqual(len(templates), 1)
        template = templates[0]
        self.assertEqual(set(template), TEMPLATE_KEYS)
        self.assertEqual(template["id"], template_id)
        self.assertEqual(template["name"], "报价")
        self.assertEqual(template["subject"], "报价单 - {{客户}}")
        self.assertEqual(template["body"], "您好，报价见附件。")
        self.assertTrue(template["createdAt"])
        self.assertEqual(template["createdAt"], template["updatedAt"])
        json.dumps(templates, ensure_ascii=False)

    def test_subject_and_body_default_to_empty(self):
        self._add(name="空白模板")
        template = self._dispatch("template.list", {})["templates"][0]
        self.assertEqual(template["subject"], "")
        self.assertEqual(template["body"], "")

    def test_remove(self):
        template_id = self._add()
        self.assertEqual(
            self._dispatch("template.remove", {"id": template_id}), {"removed": True}
        )
        self.assertEqual(self._dispatch("template.list", {})["templates"], [])
        self.assertIsNone(self.service.store.get_template(template_id))

        with self.assertRaises(MailCoreError) as context:
            self._dispatch("template.remove", {"id": template_id})
        self.assertEqual(context.exception.code, "NOT_FOUND")

    def test_add_requires_non_empty_name(self):
        cases = (
            ("missing", {}),
            ("empty", {"name": ""}),
            ("blank", {"name": "   "}),
            ("wrong type", {"name": 123}),
        )
        for label, params in cases:
            with self.subTest(case=label):
                with self.assertRaises(MailCoreError) as context:
                    self._dispatch("template.add", params)
                self.assertEqual(context.exception.code, "BAD_REQUEST")
        self.assertEqual(self._dispatch("template.list", {})["templates"], [])


if __name__ == "__main__":
    unittest.main()
