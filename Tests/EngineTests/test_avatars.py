import base64
from concurrent.futures import Future
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Engine"))
import listener_core as core
from avatars import SenderAvatars, avatar_url, lookup_sender_avatar


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.contact = core.ContactCandidate("测试成员", "employee", "exact-open-id", "", "", "")
        self.url = "https://static-legacy.dingtalk.com/avatar.jpg"

    def test_media_id_uses_source_dimensions_and_requests_a_thumbnail(self):
        raw = b"\x94\x00\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x0b\xd0\xcd\x0b\xd5"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + media), f"https://static-legacy.dingtalk.com/media/{media}_3029_3024.jpg_128x128.jpg")

    def test_png_source_keeps_its_extension_when_requesting_a_jpeg_thumbnail(self):
        raw = b"\x94\x02\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x05\x00\xcd\x05\x00"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + media), f"https://static-legacy.dingtalk.com/media/{media}_1280_1280.png_128x128.jpg")

    def test_png_source_preserves_width_and_height_order(self):
        raw = b"\x94\x02\xcd\x01\x02\x40\xcc\x80"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + media), f"https://static-legacy.dingtalk.com/media/{media}_128_64.png_128x128.jpg")

    def test_unrecognised_source_format_is_not_assumed_to_be_jpeg(self):
        raw = b"\x94\x7f\xcd\x01\x02\x40\xcc\x80"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + media), "")

    def test_urls_and_missing_or_unrecognised_media_ids(self):
        self.assertEqual(avatar_url(self.url), self.url)
        for value in (None, "", "   ", "@invalid", "https://[", {}, "file:///tmp/image.png"):
            with self.subTest(value=value): self.assertEqual(avatar_url(value), "")

    def test_legacy_http_and_protocol_relative_avatars_are_upgraded(self):
        self.assertEqual(
            avatar_url("http://static-legacy.dingtalk.com/avatar.jpg?x=1"),
            "https://static-legacy.dingtalk.com/avatar.jpg?x=1",
        )
        self.assertEqual(
            avatar_url("//static-legacy.dingtalk.com/avatar.jpg"),
            "https://static-legacy.dingtalk.com/avatar.jpg",
        )
        self.assertEqual(avatar_url("http://evil.test/avatar.jpg"), "")

    def test_standard_base64_and_padding_are_accepted(self):
        raw = b"\x94\x00\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x02\x80\xcd\x02\x80"
        encoded = base64.b64encode(raw).decode()
        padded = encoded + "=" * (-len(encoded) % 4)
        for value in (encoded, padded):
            with self.subTest(value=value):
                self.assertIn(
                    "_640_640.jpg_128x128.jpg",
                    avatar_url("@" + value),
                )

    def test_media_id_accepts_small_dimensions_but_rejects_truncated_integers(self):
        raw = b"\x94\x00\xcd\x01\x02\x40\xcc\x80"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertIn("_128_64.jpg_128x128.jpg", avatar_url("@" + media))
        malformed = base64.urlsafe_b64encode(raw[:-1]).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + malformed), "")

    def test_five_item_media_id_uses_dimensions_and_accepts_the_opaque_suffix(self):
        raw = (b"\x95\x02\xcf" + (123456).to_bytes(8, "big")
               + b"\xcd\x05\x00\xcd\x05\x00" + b"\xb0" + b"0123456789abcdef")
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(
            avatar_url("@" + media),
            f"https://static-legacy.dingtalk.com/media/{media}_1280_1280.png_128x128.jpg",
        )
        malformed = base64.urlsafe_b64encode(raw[:-1]).decode().rstrip("=")
        self.assertEqual(avatar_url("@" + malformed), "")

        nil_suffix = (b"\x95\x02\xcf" + (123456).to_bytes(8, "big")
                      + b"\xcd\x05\x00\xcd\x05\x00" + b"\xc0")
        nil_media = base64.urlsafe_b64encode(nil_suffix).decode().rstrip("=")
        self.assertIn(
            "_1280_1280.png_128x128.jpg",
            avatar_url("@" + nil_media),
        )

    def lookup(self, rows):
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        result = subprocess.CompletedProcess([], 0, json.dumps({"success": True, "result": rows}), "")
        empty_contacts = subprocess.CompletedProcess([], 0, json.dumps({"success": True, "result": []}), "")
        with patch("listener_core.run_read_command", return_value=empty_contacts), \
                patch("avatars.core.run_command", return_value=result) as call:
            url = lookup_sender_avatar(dws, self.contact)
            aisearch = next(
                item for item in call.call_args_list
                if "aisearch" in item.args[0]
            )
            self.assertEqual(aisearch.args[0], [
                "/fixture/dws", "--profile", "corp:account",
                "aisearch", "person", "--query", "测试成员",
                "--dimension", "name", "--format", "json",
            ])
            return url

    def test_same_name_never_substitutes_for_exact_group_member_identity(self):
        self.assertEqual(self.lookup([
            {"title": "测试成员", "openDingTalkId": "other-person", "authorAvatar": "https://static-legacy.dingtalk.com/wrong.jpg"},
            {"title": "测试成员", "openDingTalkId": self.contact.open_dingtalk_id, "authorAvatar": self.url},
        ]), self.url)
        self.assertEqual(self.lookup([{"title": "测试成员", "openDingTalkId": "other-person", "authorAvatar": self.url}]), "")

    def test_contact_search_avatar_and_user_id_fallback_are_supported(self):
        contact_payload = {
            "success": True,
            "result": [{
                "name": self.contact.name,
                "userId": "employee-id",
                "openDingTalkId": self.contact.open_dingtalk_id,
                "avatarUrl": self.url,
            }],
        }
        with patch("listener_core.run_read_command",
                   return_value=subprocess.CompletedProcess([], 0, json.dumps(contact_payload), "")) as command:
            self.assertEqual(
                lookup_sender_avatar(core.DwsClient("/fixture/dws", Mock(), "corp:account"), self.contact),
                self.url,
            )
        self.assertEqual(command.call_args.args[0], [
            "/fixture/dws", "--profile", "corp:account",
            "contact", "user", "search", "--query", self.contact.name, "--format", "json",
        ])

        ai_payload = {
            "success": True,
            "result": [{"userId": "employee-id", "profilePhotoUrl": self.url}],
        }
        contact = replace(self.contact, avatar_media_id="")
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch.object(dws, "search_contacts", return_value=[
            core.ContactCandidate(self.contact.name, "employee-id", self.contact.open_dingtalk_id,
                                  "", "", "", ""),
        ]), patch("avatars.core.run_command",
                  return_value=subprocess.CompletedProcess([], 0, json.dumps(ai_payload), "")):
            self.assertEqual(lookup_sender_avatar(dws, contact), self.url)

    def test_missing_and_conflicting_avatars_use_the_placeholder(self):
        self.assertEqual(self.lookup([{"openDingTalkId": self.contact.open_dingtalk_id, "authorAvatar": None}]), "")
        self.assertEqual(self.lookup([
            {"openDingTalkId": self.contact.open_dingtalk_id, "authorAvatar": self.url},
            {"openDingTalkId": self.contact.open_dingtalk_id, "authorAvatar": "https://static-legacy.dingtalk.com/other.jpg"},
        ]), "")

    def test_exact_sender_lookup_uses_the_returned_png_format(self):
        raw = b"\x94\x02\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x05\x00\xcd\x05\x00"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        url = self.lookup([{"openDingTalkId": self.contact.open_dingtalk_id, "authorAvatar": "@" + media}])
        self.assertEqual(url, f"https://static-legacy.dingtalk.com/media/{media}_1280_1280.png_128x128.jpg")

    def test_group_avatar_is_used_without_an_extra_aisearch_lookup(self):
        raw = b"\x94\x00\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x02\x80\xcd\x02\x80"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        contact = replace(self.contact, avatar_media_id="@" + media)
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch("avatars.core.run_command") as call:
            self.assertEqual(
                lookup_sender_avatar(dws, contact),
                f"https://static-legacy.dingtalk.com/media/{media}_640_640.jpg_128x128.jpg",
            )
            call.assert_not_called()

    def test_avatar_aliases_are_found_inside_nested_sender_objects(self):
        self.assertEqual(
            core.first_avatar_value({"sender": {"avatarUrl": self.url}}),
            self.url,
        )
        self.assertEqual(
            core.first_avatar_value({"member_info": {"avatar": {"url": self.url}}}),
            self.url,
        )

    def test_group_members_keep_the_supplied_avatar_media_id(self):
        raw = b"\x95\x02\xcf" + (123456).to_bytes(8, "big") + b"\xcd\x02\x80\xcd\x02\x80" + b"\xb0" + b"0123456789abcdef"
        media = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        payload = {
            "success": True,
            "result": {
                "hasMore": False,
                "list": [{
                    "memberEmpName": self.contact.name,
                    "openDingtalkId": self.contact.open_dingtalk_id,
                    "memberAvatarMediaId": "@" + media,
                }],
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch("listener_core.run_read_command", return_value=completed):
            members = dws.group_members("group-id", "测试群")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].avatar_media_id, "@" + media)
        self.assertEqual(
            lookup_sender_avatar(dws, members[0]),
            f"https://static-legacy.dingtalk.com/media/{media}_640_640.png_128x128.jpg",
        )

    def test_group_members_accept_direct_avatar_alias(self):
        payload = {
            "success": True,
            "result": {
                "hasMore": False,
                "list": [{
                    "memberEmpName": self.contact.name,
                    "openDingtalkId": self.contact.open_dingtalk_id,
                    "groupMemberAvatar": "http://static-legacy.dingtalk.com/group.jpg",
                }],
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch("listener_core.run_read_command", return_value=completed):
            members = dws.group_members("group-id", "测试群")
        self.assertEqual(members[0].avatar_media_id, "http://static-legacy.dingtalk.com/group.jpg")
        with patch("avatars.core.run_command") as command:
            self.assertEqual(
                lookup_sender_avatar(dws, members[0]),
                "https://static-legacy.dingtalk.com/group.jpg",
            )
            command.assert_not_called()

    def test_group_members_accept_nested_avatar_object(self):
        payload = {
            "success": True,
            "result": {
                "hasMore": False,
                "list": [{
                    "memberEmpName": self.contact.name,
                    "openDingtalkId": self.contact.open_dingtalk_id,
                    "memberAvatar": {"mediaId": self.url},
                }],
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch("listener_core.run_read_command", return_value=completed):
            members = dws.group_members("group-id", "测试群")
        self.assertEqual(members[0].avatar_media_id, self.url)

    def test_group_members_skip_entries_without_a_usable_identity(self):
        payload = {
            "success": True,
            "result": {
                "hasMore": False,
                "list": [
                    {"memberEmpName": "无开放 ID 的成员"},
                    {"memberEmpName": self.contact.name,
                     "openDingtalkId": self.contact.open_dingtalk_id},
                    {"memberEmpName": "", "openDingtalkId": "ignored"},
                ],
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        dws = core.DwsClient("/fixture/dws", Mock(), "corp:account")
        with patch("listener_core.run_read_command", return_value=completed):
            members = dws.group_members("group-id", "测试群")
        self.assertEqual([member.name for member in members], [self.contact.name])

    def resolver(self, lookup, publish):
        resolver = SenderAvatars(lookup, publish)
        self.addCleanup(lambda: resolver.pool.shutdown(wait=True, cancel_futures=True))
        self.addCleanup(resolver.close)
        return resolver

    def test_one_background_lookup_updates_waiting_requests_and_is_cached(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        updates = []
        def lookup(_):
            entered.set(); release.wait(3); return self.url
        def publish(task_id, url):
            updates.append((task_id, url))
            if len(updates) == 2: done.set()
        lookup = Mock(side_effect=lookup)
        resolver = self.resolver(lookup, publish)
        try:
            resolver.request(self.contact, "task-1")
            self.assertTrue(entered.wait(2))
            resolver.request(self.contact, "task-2")
            self.assertEqual(updates, [])
            lookup.assert_called_once_with(self.contact)
            release.set(); self.assertTrue(done.wait(2))
            self.assertEqual(set(updates), {("task-1", self.url), ("task-2", self.url)})
            resolver.request(self.contact, "task-3")
            self.assertEqual(updates[-1], ("task-3", self.url))
            lookup.assert_called_once()
        finally: release.set()

    def test_failed_lookup_stays_optional_and_can_be_retried_later(self):
        updates = []
        resolver = self.resolver(Mock(), lambda task_id, url: updates.append((task_id, url)))
        failure, success = Future(), Future()
        failure.set_exception(RuntimeError("temporarily unavailable"))
        success.set_result(self.url)
        with patch.object(resolver.pool, "submit", side_effect=[failure, success]) as submit, \
                patch("avatars.time.monotonic", return_value=100) as now:
            resolver.request(self.contact, "task-1")
            resolver.request(self.contact, "task-2")
            submit.assert_called_once()
            self.assertEqual(updates, [])
            now.return_value = 161
            resolver.request(self.contact, "task-3")
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(updates, [("task-3", self.url)])

    def test_event_avatar_can_seed_the_session_cache(self):
        updates = []
        resolver = self.resolver(Mock(), lambda task_id, url: updates.append((task_id, url)))
        resolver.remember(self.contact.open_dingtalk_id, self.url)
        resolver.request(self.contact, "task-1")
        self.assertEqual(updates, [("task-1", self.url)])
    def test_stopping_does_not_publish_late_avatar_results(self):
        entered, release = threading.Event(), threading.Event()
        updates = []
        def lookup(_):
            entered.set(); release.wait(3); return self.url
        resolver = self.resolver(lookup, lambda *value: updates.append(value))
        try:
            resolver.request(self.contact, "task-1")
            self.assertTrue(entered.wait(2))
            resolver.close(); release.set()
            resolver.pool.shutdown(wait=True)
            resolver.request(self.contact, "task-2")
            self.assertEqual(updates, [])
        finally: release.set()


if __name__ == "__main__": unittest.main()
