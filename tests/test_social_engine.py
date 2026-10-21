"""Comprehensive test suite for the Social Media Engine, Brand Voice, and Buffer Integration (Stage 4)."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from orchestration.telegram_bot import TelegramOperatorBot
from social.brand import BANNED_BUZZWORDS, score_brand_voice, validate_brand_voice
from social.cli import main as cli_main
from social.generator import SocialPostGenerator
from social.models import (
    ContentArchetype,
    DraftStatus,
    SocialDraft,
    VERIFIED_CHANNELS,
    normalize_channel,
)
from social.service import SocialDraftService, SocialServiceError
from tracking.store import TrackingStore


class SocialModelsTestCase(unittest.TestCase):
    def test_normalize_channel(self):
        self.assertEqual(normalize_channel("twitter"), "twitter")
        self.assertEqual(normalize_channel("x"), "twitter")
        self.assertEqual(normalize_channel("TWITTER"), "twitter")
        self.assertEqual(normalize_channel("linkedin"), "linkedin")
        self.assertEqual(normalize_channel("instagram"), "instagram")
        with self.assertRaises(ValueError):
            normalize_channel("tiktok")

    def test_verified_channel_specifications(self):
        self.assertIn("twitter", VERIFIED_CHANNELS)
        self.assertIn("linkedin", VERIFIED_CHANNELS)
        self.assertIn("instagram", VERIFIED_CHANNELS)

        tw = VERIFIED_CHANNELS["twitter"]
        self.assertEqual(tw.channel_id, "6aa054f7cd8b9c702c2f1a35")
        self.assertEqual(tw.max_length, 280)
        self.assertFalse(tw.requires_media)

        li = VERIFIED_CHANNELS["linkedin"]
        self.assertEqual(li.channel_id, "6a5f45a7e2638b94d7a8bc3e")
        self.assertEqual(li.max_length, 3000)
        self.assertFalse(li.requires_media)

        ig = VERIFIED_CHANNELS["instagram"]
        self.assertEqual(ig.channel_id, "6acf807e778b240e2f07ec61")
        self.assertEqual(ig.max_length, 2200)
        self.assertTrue(ig.requires_media)

    def test_draft_to_buffer_approval_payload(self):
        draft = SocialDraft(
            id="soc-tw-test001",
            channel="twitter",
            topic="Website Performance",
            caption="Fast websites increase conversion.",
            archetype="tech_insight",
            status=DraftStatus.APPROVED.value,
        )
        payload = draft.to_buffer_approval_payload()
        self.assertEqual(payload["action_type"], "buffer_publish")
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["prepared_content"], draft.caption)
        self.assertEqual(payload["metadata"]["service"], "twitter")
        self.assertEqual(payload["metadata"]["channel_id"], "6aa054f7cd8b9c702c2f1a35")
        self.assertTrue(payload["metadata"]["saveToDraft"])
        self.assertEqual(payload["metadata"]["execution_authorized"], "true")
        self.assertEqual(payload["metadata"]["is_synthetic_test"], "false")
        self.assertTrue(len(payload["metadata"]["fingerprint"]) > 0)


class BrandVoiceValidationTestCase(unittest.TestCase):
    def test_clean_text_passes(self):
        text = "Semantic HTML and fast load times improve search rankings and user retention."
        is_valid, violations = validate_brand_voice(text, channel="twitter")
        self.assertTrue(is_valid)
        self.assertEqual(violations, [])

    def test_banned_buzzwords_rejected(self):
        for word in ["synergy", "10x", "revolutionary", "game-changer", "paradigm shift"]:
            text = f"We deliver {word} results for businesses."
            is_valid, violations = validate_brand_voice(text, channel="twitter")
            self.assertFalse(is_valid, f"Expected '{word}' to fail brand validation.")
            self.assertTrue(any(word in v for v in violations))

    def test_excessive_exclamation_rejected(self):
        text = "This is a great website!!!"
        is_valid, violations = validate_brand_voice(text, channel="twitter")
        self.assertFalse(is_valid)
        self.assertTrue(any("exclamation" in v.lower() for v in violations))

    def test_excessive_yelling_rejected(self):
        text = "BUY OUR NEW PRODUCT TODAY FOR SUCCESS"
        is_valid, violations = validate_brand_voice(text, channel="twitter")
        self.assertFalse(is_valid)
        self.assertTrue(any("capitalization" in v.lower() for v in violations))

    def test_length_exceeded_rejected(self):
        text = "a" * 285
        is_valid, violations = validate_brand_voice(text, channel="twitter")
        self.assertFalse(is_valid)
        self.assertTrue(any("length" in v.lower() for v in violations))

    def test_score_brand_voice(self):
        perfect = "Grounded engineering and clean layouts improve user trust. #WebDev"
        score = score_brand_voice(perfect, channel="twitter")
        self.assertEqual(score, 1.0)

        flawed = "Our 10x synergy revolutionary tool is HERE NOW!!!"
        flawed_score = score_brand_voice(flawed, channel="twitter")
        self.assertLess(flawed_score, 0.5)


class SocialGeneratorTestCase(unittest.TestCase):
    def setUp(self):
        self.generator = SocialPostGenerator()

    def test_generate_twitter_under_limit(self):
        for archetype in ContentArchetype:
            draft = self.generator.generate(
                "Mobile page speed optimization",
                channel="twitter",
                archetype=archetype.value,
            )
            self.assertEqual(draft.channel, "twitter")
            self.assertLessEqual(len(draft.caption), 280)
            self.assertGreater(draft.brand_voice_score, 0.7)

    def test_generate_linkedin_structured(self):
        draft = self.generator.generate(
            "Local restaurant SEO and discoverability",
            channel="linkedin",
            archetype=ContentArchetype.CASE_STUDY.value,
        )
        self.assertEqual(draft.channel, "linkedin")
        self.assertIn("\n", draft.caption)
        self.assertLessEqual(len(draft.caption), 3000)
        self.assertGreaterEqual(len(draft.caption), 100)

    def test_generate_instagram_includes_prompt(self):
        draft = self.generator.generate(
            "Minimalist cafe brand identity",
            channel="instagram",
            archetype=ContentArchetype.LOCAL_GROWTH.value,
        )
        self.assertEqual(draft.channel, "instagram")
        self.assertIsNotNone(draft.media_prompt)
        self.assertIn("photograph", draft.media_prompt.lower())


class SocialDraftServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.storage_file = Path(self.temp_dir.name) / "drafts.json"
        self.service = SocialDraftService(storage_path=self.storage_file)

    def test_create_and_get_draft(self):
        draft = self.service.create_draft(
            "E-commerce site architecture",
            channel="twitter",
            archetype="tech_insight",
        )
        self.assertIsNotNone(draft.id)
        fetched = self.service.get_draft(draft.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.topic, "E-commerce site architecture")
        self.assertEqual(fetched.status, DraftStatus.DRAFT.value)

    def test_list_drafts_filtering(self):
        d1 = self.service.create_draft("Topic A", channel="twitter")
        d2 = self.service.create_draft("Topic B", channel="linkedin")

        all_drafts = self.service.list_drafts()
        self.assertEqual(len(all_drafts), 2)

        tw_only = self.service.list_drafts(channel="twitter")
        self.assertEqual(len(tw_only), 1)
        self.assertEqual(tw_only[0].id, d1.id)

    def test_approve_draft_lifecycle(self):
        draft = self.service.create_draft("Reliable background workers", channel="twitter")
        self.assertEqual(draft.status, DraftStatus.DRAFT.value)

        approved = self.service.approve_draft(draft.id, operator_note="Approved for publishing")
        self.assertEqual(approved.status, DraftStatus.APPROVED.value)
        self.assertEqual(approved.operator_note, "Approved for publishing")

        payload = self.service.export_buffer_payload(draft.id)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["metadata"]["execution_authorized"], "true")

    def test_approve_instagram_without_media_rejected(self):
        draft = self.service.create_draft("Visual branding", channel="instagram")
        with self.assertRaises(SocialServiceError) as ctx:
            self.service.approve_draft(draft.id)
        self.assertEqual(ctx.exception.code, "MEDIA_URL_REQUIRED")

    def test_reject_draft(self):
        draft = self.service.create_draft("Needs revision", channel="twitter")
        rejected = self.service.reject_draft(draft.id, reason="Topic not relevant this week")
        self.assertEqual(rejected.status, DraftStatus.REJECTED.value)
        self.assertEqual(rejected.operator_note, "Topic not relevant this week")


class SocialCliTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_cli_generate_and_show(self):
        exit_code = cli_main(["generate", "--topic", "Clean code practices", "--channel", "twitter"])
        self.assertEqual(exit_code, 0)

        list_exit = cli_main(["list"])
        self.assertEqual(list_exit, 0)


class TelegramSocialCommandsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        state_file = Path(self.temp_dir.name) / "state.json"
        self.store = TrackingStore(state_file)
        self.bot = TelegramOperatorBot(
            store=self.store,
            allowed_user_ids=[12345],
            allowed_chat_ids=[98765],
        )

    def test_help_includes_social_and_post(self):
        res = self.bot.dispatch("/help", user_id=12345, chat_id=98765)
        self.assertEqual(res["code"], "HELP")
        self.assertIn("/social", res["reply"])
        self.assertIn("/post", res["reply"])

    def test_social_status_command(self):
        res = self.bot.dispatch("/social", user_id=12345, chat_id=98765)
        self.assertEqual(res["code"], "SOCIAL_STATUS")
        self.assertIn("Social Media Engine", res["reply"])
        self.assertIn("Bigcherem", res["reply"])
        self.assertIn("Joseph Unaogu", res["reply"])

    def test_post_lifecycle_via_telegram(self):
        # 1. Generate new draft
        new_res = self.bot.dispatch(
            "/post new twitter Practical API design principles",
            user_id=12345,
            chat_id=98765,
        )
        self.assertEqual(new_res["code"], "POST_GENERATED")
        draft_id = new_res["draft_id"]

        # 2. View draft
        view_res = self.bot.dispatch(f"/post view {draft_id}", user_id=12345, chat_id=98765)
        self.assertEqual(view_res["code"], "POST_VIEW")
        self.assertIn("TWITTER", view_res["reply"])

        # 3. Approve draft
        app_res = self.bot.dispatch(f"/post approve {draft_id}", user_id=12345, chat_id=98765)
        self.assertEqual(app_res["code"], "POST_APPROVED")
        self.assertIn("APPROVED for Buffer scheduling", app_res["reply"])


if __name__ == "__main__":
    unittest.main()
