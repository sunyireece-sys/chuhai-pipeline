import json
import os
import tempfile
import unittest
from pathlib import Path

_APP_BOOTSTRAP_DIR = tempfile.TemporaryDirectory()
os.environ["FEEDBACK_DB_PATH"] = str(Path(_APP_BOOTSTRAP_DIR.name) / "feedback.db")

from webui import app as app_module
from webui.lead_priority import RankingInputs, compute_final_score


class LeadPriorityTest(unittest.TestCase):
    def test_gosport_case(self) -> None:
        inp = RankingInputs(
            status="未发",
            rating="A",
            customer_type="Direct Buyer",
            primary_vertical="fitness_equipment",
            food_supplement_focus="marginal",
            input_country="RU",
            verdict_country="RU",
            has_verified_email=True,
            feedback_score=None,
        )
        self.assertLess(compute_final_score(inp), 0.35)

    def test_ideal_lead(self) -> None:
        inp = RankingInputs(
            status="已询价",
            rating="S",
            customer_type="Direct Buyer",
            primary_vertical="supplement",
            food_supplement_focus="core",
            input_country="RU",
            verdict_country="RU",
            has_verified_email=True,
            feedback_score=0.8,
        )
        self.assertGreaterEqual(compute_final_score(inp), 0.95)

    def test_country_mismatch_penalty(self) -> None:
        inp_match = RankingInputs(rating="A", input_country="RU", verdict_country="RU")
        inp_mismatch = RankingInputs(rating="A", input_country="RU", verdict_country="US")
        self.assertGreaterEqual(
            compute_final_score(inp_match) - compute_final_score(inp_mismatch),
            0.15,
        )

    def test_feedback_score_independent_from_baseline(self) -> None:
        no_fb = RankingInputs(rating="C", feedback_score=None)
        high_fb = RankingInputs(rating="C", feedback_score=0.9)
        self.assertLess(compute_final_score(high_fb) - compute_final_score(no_fb), 0.20)


class CustomerTypeAutoManualTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        app_module.DB_PATH = root / "feedback.db"
        app_module.RUNS_DIR = root / "runs"
        app_module._init_db()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_profile(self, run_id: str, slug: str, customer_type: str) -> None:
        profiles_dir = app_module.RUNS_DIR / run_id / "05_profiles" / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        profile = {"company": {"step4_customer_type": customer_type}}
        (profiles_dir / f"{slug}.json").write_text(
            json.dumps(profile, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_effective_type_falls_back_to_auto_type(self) -> None:
        with app_module._db() as conn:
            conn.execute(
                """
                INSERT INTO customer_type (profile_slug, run_id, auto_type, type, source)
                VALUES (?, ?, ?, NULL, 'auto')
                """,
                ("001-alpha", "run_auto", "品牌商"),
            )

        ctype = app_module.get_customer_type("001-alpha", "run_auto")
        self.assertEqual(ctype["auto_type"], "品牌商")
        self.assertIsNone(ctype["manual_type"])
        self.assertEqual(ctype["effective_type"], "品牌商")
        self.assertFalse(ctype["is_manual_override"])

    def test_manual_type_overrides_auto_type(self) -> None:
        with app_module._db() as conn:
            conn.execute(
                """
                INSERT INTO customer_type (profile_slug, run_id, auto_type, type, source)
                VALUES (?, ?, ?, ?, 'manual')
                """,
                ("001-alpha", "run_manual", "原料分销商", "OEM制造商"),
            )

        ctype = app_module.get_customer_type("001-alpha", "run_manual")
        self.assertEqual(ctype["manual_type"], "OEM制造商")
        self.assertEqual(ctype["effective_type"], "OEM制造商")
        self.assertTrue(ctype["is_manual_override"])

    def test_empty_manual_type_clears_override(self) -> None:
        with app_module._db() as conn:
            conn.execute(
                """
                INSERT INTO customer_type (profile_slug, run_id, auto_type, type, source)
                VALUES (?, ?, ?, ?, 'manual')
                """,
                ("001-alpha", "run_clear", "原料分销商", "品牌商"),
            )

        app_module.set_customer_type("001-alpha", "run_clear", "", "tester")

        ctype = app_module.get_customer_type("001-alpha", "run_clear")
        self.assertIsNone(ctype["manual_type"])
        self.assertEqual(ctype["effective_type"], "原料分销商")
        self.assertFalse(ctype["is_manual_override"])

    def test_migration_skips_competitors(self) -> None:
        self._write_profile("run_competitor", "001-competitor", "竞争对手")

        with app_module._db() as conn:
            app_module._migrate_customer_type(conn)
            count = conn.execute("SELECT COUNT(*) FROM customer_type").fetchone()[0]

        self.assertEqual(count, 0)

    def test_migration_remaps_legacy_manual_type(self) -> None:
        with app_module._db() as conn:
            conn.execute(
                """
                INSERT INTO customer_type (profile_slug, run_id, type, source)
                VALUES (?, ?, ?, 'manual')
                """,
                ("001-alpha", "run_legacy", "分销商"),
            )
            app_module._migrate_customer_type(conn)
            row = conn.execute(
                """
                SELECT type FROM customer_type
                WHERE profile_slug = ? AND run_id = ?
                """,
                ("001-alpha", "run_legacy"),
            ).fetchone()

        self.assertEqual(row["type"], "原料分销商")


if __name__ == "__main__":
    unittest.main()
