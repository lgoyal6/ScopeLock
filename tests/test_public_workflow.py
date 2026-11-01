import importlib.util
import unittest
from pathlib import Path
spec = importlib.util.spec_from_file_location("public_demo", Path(__file__).parents[1] / "public-demo/api/demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

class PublicWorkflowTests(unittest.TestCase):
    def test_only_explicit_holder_approval_commits_once(self):
        approved = demo.evaluate_scenario("appointment", "approve")
        self.assertEqual((approved["effect_count"], approved["repeat_status"]), (1, "duplicate"))
        for answer in ("pending", "decline", "revoke"):
            with self.subTest(answer=answer):
                refused = demo.evaluate_scenario("appointment", answer)
                self.assertEqual(refused["effect_count"], 0)
    def test_forbidden_action_stays_forbidden_after_approval(self):
        self.assertEqual(demo.evaluate_scenario("payment", "approve")["effect_count"], 0)
    def test_requests_are_isolated(self):
        demo.evaluate_scenario("reference", "revoke")
        self.assertEqual(demo.evaluate_scenario("reference")["effect_count"], 1)


def test_live_completion_still_translates_when_english_was_not_supplied(tmp_path, monkeypatch):
    from scopelock.core import consult, db, relay, translate
    previous_path = db.DB_PATH
    previous_connection = getattr(db._local, "conn", None)
    db.DB_PATH = tmp_path / "consult.sqlite3"
    db._local.conn = None
    try:
        case = db.create_case(holder_name="Fictional holder", holder_lang="spanish", institution="Synthetic service", issue_type="demo", issue_summary="Fictional appointment")
        policy = demo.PolicyService()
        policy.create_draft(case)
        policy.confirm_mandate(case, "Sí, estoy de acuerdo.")
        translated = []
        def render(text, language):
            translated.append((text, language))
            return "Yes, I agree."
        monkeypatch.setattr(translate, "translate", render)
        monkeypatch.setattr(relay, "set_turn", lambda *args: None)
        consult.queue_holder_verb(case, "reschedule")
        entry = consult.begin(case, "Move the appointment?", "¿Cambio la cita?")
        assert consult.complete(case, "Sí, de acuerdo.") == entry["decision_id"]
        assert translated == [("Sí, de acuerdo.", "english")]
    finally:
        connection = getattr(db._local, "conn", None)
        if connection is not None:
            connection.close()
        db._local.conn = previous_connection
        db.DB_PATH = previous_path
        consult.PENDING.clear()
