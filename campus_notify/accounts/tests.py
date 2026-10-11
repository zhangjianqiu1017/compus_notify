"""账号单测：邮箱验证码注册、邮箱/账号密码登录、邮箱验证码登录。

测试用的邮件后端被 settings 强制为 locmem，所以既不会真的发信，
也能从 mail.outbox 里取出验证码做断言。
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import EmailVerificationCode, UserPreference
from accounts.services import (
    CodeError,
    consume_code,
    email_taken,
    find_user_by_email,
    issue_code,
    normalize_email,
    seconds_until_resend,
    verify_code,
)
from events.models import Event
from extraction.pipeline import process_batch
from ingest.models import Message
from ingest.services import create_batch
from tests_helpers import make_user


def last_code() -> str:
    """从最近一封邮件里取验证码。"""
    assert mail.outbox, "没有发出任何邮件"
    match = re.search(r"验证码是：(\d{6})", mail.outbox[-1].body)
    assert match, f"邮件正文里没有验证码：{mail.outbox[-1].body!r}"
    return match.group(1)


class CodeServiceTests(TestCase):
    """验证码服务层：过期、错次、冷却、单次有效。"""

    def setUp(self):
        self.email = "Student@Example.com"

    def test_issue_and_verify(self):
        code = issue_code(self.email, EmailVerificationCode.Purpose.REGISTER)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("注册绑定邮箱", mail.outbox[0].subject)
        # 邮箱统一小写存储
        record = EmailVerificationCode.objects.get()
        self.assertEqual(record.email, "student@example.com")
        # 不落明文
        self.assertNotIn(code, record.code_hash)
        self.assertIsNone(verify_code("student@example.com", "register", code))

    def test_wrong_code_counts_attempts(self):
        issue_code(self.email, "register")
        with self.assertRaises(CodeError) as ctx:
            verify_code(self.email, "register", "000000")
        self.assertIn("不正确", str(ctx.exception))
        self.assertEqual(EmailVerificationCode.objects.get().attempts, 1)

    def test_attempt_limit_locks_code(self):
        code = issue_code(self.email, "register")
        for _ in range(5):
            with self.assertRaises(CodeError):
                verify_code(self.email, "register", "999999")
        with self.assertRaises(CodeError) as ctx:
            verify_code(self.email, "register", code)
        self.assertIn("次数过多", str(ctx.exception))

    def test_expired_code_rejected(self):
        code = issue_code(self.email, "register")
        record = EmailVerificationCode.objects.get()
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save(update_fields=["expires_at"])
        with self.assertRaises(CodeError) as ctx:
            verify_code(self.email, "register", code)
        self.assertIn("已过期", str(ctx.exception))

    def test_resend_cooldown(self):
        issue_code(self.email, "register")
        self.assertGreater(seconds_until_resend(self.email, "register"), 0)
        with self.assertRaises(CodeError) as ctx:
            issue_code(self.email, "register")
        self.assertIn("太频繁", str(ctx.exception))

    def test_new_code_invalidates_old(self):
        first = issue_code(self.email, "register")
        EmailVerificationCode.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        second = issue_code(self.email, "register")
        self.assertNotEqual(first, second)
        with self.assertRaises(CodeError):
            verify_code(self.email, "register", first)
        self.assertIsNone(verify_code(self.email, "register", second))

    def test_code_is_single_use(self):
        code = issue_code(self.email, "register")
        verify_code(self.email, "register", code)
        consume_code(self.email, "register")
        with self.assertRaises(CodeError) as ctx:
            verify_code(self.email, "register", code)
        self.assertIn("已使用", str(ctx.exception))

    def test_used_code_does_not_block_resend(self):
        """回归：用过的码不该继续触发 60 秒重发冷却。

        注册成功就会消费掉注册码（used_at 被写上），此时库里留着的是一条废码。
        如果还按它的 created_at 算冷却，用户注销账号后用同一邮箱重新注册时，
        会在「发送验证码」这一步被「发送太频繁」挡住，看起来就像邮箱被永久占用。
        """
        issue_code(self.email, "register")
        consume_code(self.email, "register")
        self.assertEqual(seconds_until_resend(self.email, "register"), 0)
        # 用过的码不挡发码
        issue_code(self.email, "register")
        self.assertEqual(len(mail.outbox), 2)

    def test_unused_code_still_blocks_resend(self):
        """反面：没被消费的有效码仍然受冷却保护（防刷不能被顺手削掉）。"""
        issue_code(self.email, "register")
        self.assertGreater(seconds_until_resend(self.email, "register"), 0)
        with self.assertRaises(CodeError) as ctx:
            issue_code(self.email, "register")
        self.assertIn("太频繁", str(ctx.exception))
        self.assertEqual(len(mail.outbox), 1)

    def test_purpose_is_isolated(self):
        register_code = issue_code(self.email, "register")
        with self.assertRaises(CodeError):
            verify_code(self.email, "reset", register_code)

    def test_email_taken_and_lookup(self):
        user = make_user("someone")
        user.email = "someone@example.com"
        user.save(update_fields=["email"])
        self.assertTrue(email_taken("Someone@Example.com"))
        self.assertEqual(find_user_by_email("SOMEONE@example.com").pk, user.pk)
        self.assertFalse(email_taken("nobody@example.com"))
        self.assertEqual(normalize_email("  A@B.Com "), "a@b.com")

    def test_smtp_failure_rolls_back_and_allows_retry(self):
        """发信失败时：不留下无效验证码，也不该被冷却时间挡住重试。"""
        from unittest.mock import patch

        with patch("accounts.services.send_mail", side_effect=OSError("smtp down")):
            with self.assertRaises(CodeError) as ctx:
                issue_code(self.email, "register")
        self.assertIn("不可用", str(ctx.exception))
        self.assertFalse(EmailVerificationCode.objects.exists())
        self.assertEqual(seconds_until_resend(self.email, "register"), 0)

        # 恢复后立刻可以重发
        code = issue_code(self.email, "register")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNone(verify_code(self.email, "register", code))

    def test_smtp_failure_keeps_previous_code_valid(self):
        """发新码失败不应把旧的有效码一起作废掉。"""
        from unittest.mock import patch

        old_code = issue_code(self.email, "register")
        EmailVerificationCode.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        with patch("accounts.services.send_mail", side_effect=OSError("smtp down")):
            with self.assertRaises(CodeError):
                issue_code(self.email, "register")
        self.assertIsNone(verify_code(self.email, "register", old_code))

    def test_unknown_recipient_gives_actionable_message(self):
        """邮箱拼错时服务商会回 550，提示要让用户去检查地址，而不是"稍后重试"。"""
        import smtplib
        from unittest.mock import patch

        refusal = smtplib.SMTPDataError(
            550, b"The recipient may contain a non-existent account"
        )
        with patch("accounts.services.send_mail", side_effect=refusal):
            with self.assertRaises(CodeError) as ctx:
                issue_code(self.email, "register")
        self.assertIn("可能不存在", str(ctx.exception))
        self.assertFalse(EmailVerificationCode.objects.exists())

    def test_connect_failure_gives_retry_message(self):
        from unittest.mock import patch

        import smtplib

        with patch(
            "accounts.services.send_mail",
            side_effect=smtplib.SMTPConnectError(421, "service not available"),
        ):
            with self.assertRaises(CodeError) as ctx:
                issue_code(self.email, "register")
        self.assertIn("暂时不可用", str(ctx.exception))


class RegistrationTests(TestCase):
    def _send_code(self, email="newbie@example.com"):
        return self.client.post(reverse("accounts:send_register_code"), {"email": email})

    def _payload(self, code, email="newbie@example.com", **overrides):
        data = {
            "email": email,
            "code": code,
            "password1": "Str0ngPass!2026",
            "password2": "Str0ngPass!2026",
        }
        data.update(overrides)
        return data

    def test_send_code_requires_valid_email(self):
        response = self._send_code("not-an-email")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_code_rejects_already_registered_email(self):
        user = make_user("exists")
        user.email = "taken@example.com"
        user.save(update_fields=["email"])
        response = self._send_code("taken@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailVerificationCode.objects.exists())
        # 明确告诉用户「已注册」，别让人一直等一封不会来的邮件
        page = self.client.get(reverse("accounts:register"))
        self.assertContains(page, "该邮箱已注册")

    def test_send_code_button_skips_native_validation(self):
        """回归：点「发送验证码」不能被浏览器原生校验拦下。

        表单里「邮箱验证码」是 required，而点发送验证码时它必然还是空的。
        如果按钮没有 formnovalidate，浏览器会直接拦下提交并提示"请填写此字段"，
        请求根本发不出去——这是只有真人用浏览器才会踩到的坑。
        """
        response = self.client.get(reverse("accounts:register"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        button = re.search(r"<button[^>]*class=\"input-group-action\"[^>]*>", html, re.S)
        self.assertIsNotNone(button, "没有找到发送验证码按钮")
        markup = button.group(0)
        self.assertIn("formnovalidate", markup)
        self.assertIn(reverse("accounts:send_register_code"), markup)

        # 验证码登录页的发码按钮同理
        code_login_button = re.search(
            r"<button[^>]*class=\"input-group-action\"[^>]*>",
            self.client.get(reverse("accounts:code_login")).content.decode(),
            re.S,
        )
        self.assertIsNotNone(code_login_button)
        self.assertIn("formnovalidate", code_login_button.group(0))

    def test_send_code_button_gets_countdown_state(self):
        """发过码之后，页面要把剩余冷却秒数渲染给按钮，刷新也绕不过。"""
        self._send_code()
        response = self.client.get(reverse("accounts:register"))
        html = response.content.decode()
        button = re.search(r"<button[^>]*data-resend-button.*?</button>", html, re.S).group(0)
        self.assertIn("data-resend-in=", button)
        self.assertIn("disabled", button)
        self.assertIn("重新发送", button)
        self.assertEqual(
            re.search(r'data-resend-email="([^"]*)"', button).group(1), "newbie@example.com"
        )

    def test_register_requires_code(self):
        response = self.client.post(
            reverse("accounts:register"),
            self._payload(""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请输入邮箱验证码")
        self.assertFalse(get_user_model().objects.filter(email="newbie@example.com").exists())

    def test_register_without_sending_code_first(self):
        response = self.client.post(reverse("accounts:register"), self._payload("123456"))
        self.assertContains(response, "请先获取验证码")

    def test_register_with_wrong_code(self):
        self._send_code()
        response = self.client.post(reverse("accounts:register"), self._payload("000000"))
        self.assertContains(response, "验证码不正确")
        self.assertFalse(get_user_model().objects.filter(email="newbie@example.com").exists())

    def test_register_success_creates_account_and_logs_in(self):
        self._send_code()
        code = last_code()
        response = self.client.post(reverse("accounts:register"), self._payload(code))
        self.assertEqual(response.status_code, 302)

        user = get_user_model().objects.get(email="newbie@example.com")
        # 账号即邮箱
        self.assertEqual(user.username, "newbie@example.com")
        self.assertTrue(user.check_password("Str0ngPass!2026"))
        self.assertTrue(UserPreference.objects.filter(user=user).exists())
        # 注册后直接登录
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)
        # 验证码已消费
        self.assertIsNotNone(EmailVerificationCode.objects.get().used_at)

    def test_registered_email_cannot_register_twice(self):
        self._send_code()
        code = last_code()
        self.client.post(reverse("accounts:register"), self._payload(code))
        self.client.logout()
        self._send_code()
        response = self.client.post(reverse("accounts:register"), self._payload(last_code()))
        self.assertContains(response, "该邮箱已注册")

    def test_same_email_can_register_again_after_delete(self):
        """回归：注销账号后，同一邮箱必须能立刻重新注册。

        注册成功会消费掉注册码（在库里留下一条 used_at 非空的废码），注销账号
        只删 User 行、不删验证码记录。若这条废码仍参与「60 秒重发冷却」计算，
        用户注销后点「发送验证码」就会收到「发送太频繁」，再也注册不上——
        看起来完全像邮箱被永久占用。
        """
        self._send_code()
        self.client.post(reverse("accounts:register"), self._payload(last_code()))
        user = get_user_model().objects.get(username="newbie@example.com")

        self.client.force_login(user)
        response = self.client.post(reverse("accounts:delete_account"), {"confirm": "删除"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(get_user_model().objects.filter(username="newbie@example.com").exists())

        # 紧接着就重来：这一步在修复前会失败（0 封邮件）
        mail.outbox.clear()
        self._send_code()
        self.assertEqual(len(mail.outbox), 1, "注销后应该能立刻收到新的注册验证码")

        response = self.client.post(reverse("accounts:register"), self._payload(last_code()))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_user_model().objects.filter(username="newbie@example.com").exists())

    def test_email_is_normalized_to_lowercase(self):
        self._send_code("MiXeD@Example.com")
        code = last_code()
        self.client.post(
            reverse("accounts:register"),
            self._payload(code, email="mixed@example.com"),
        )
        self.assertTrue(get_user_model().objects.filter(username="mixed@example.com").exists())

    def test_weak_password_rejected_and_code_not_wasted(self):
        """密码不合规时不能把验证码消费掉，否则用户得重新收码。"""
        self._send_code()
        code = last_code()
        response = self.client.post(
            reverse("accounts:register"), self._payload(code, password1="12345678", password2="12345678")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="newbie@example.com").exists())
        self.assertIsNone(EmailVerificationCode.objects.get().used_at)

        # 同一个码换个合规密码仍然可用
        response = self.client.post(reverse("accounts:register"), self._payload(code))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_user_model().objects.filter(email="newbie@example.com").exists())

    @override_settings(DEBUG=True, EMAIL_DEV_SHOW_CODE=True)
    def test_dev_mode_shows_code_on_page(self):
        self._send_code()
        response = self.client.get(reverse("accounts:register"))
        self.assertContains(response, last_code())

    @override_settings(DEBUG=False, EMAIL_DEV_SHOW_CODE=True)
    def test_code_never_shown_when_not_debug(self):
        self._send_code()
        code = last_code()
        response = self.client.get(reverse("accounts:register"))
        self.assertNotContains(response, code)


class LoginTests(TestCase):
    def setUp(self):
        self.password = "Passw0rd!234"
        self.user = get_user_model().objects.create_user(
            username="student@example.com", email="student@example.com", password=self.password
        )

    def test_login_with_email(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "student@example.com", "password": self.password}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)

    def test_login_with_email_is_case_insensitive(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "STUDENT@Example.com", "password": self.password}
        )
        self.assertEqual(response.status_code, 302)

    def test_legacy_username_login_still_works(self):
        legacy = make_user("demo_legacy", self.password)
        response = self.client.post(
            reverse("accounts:login"), {"username": legacy.username, "password": self.password}
        )
        self.assertEqual(response.status_code, 302)

    def test_wrong_password_rejected(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "student@example.com", "password": "wrong-pass"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "请输入一个正确的")

    def test_login_page_shows_recovery_links(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:code_login"))
        self.assertContains(response, reverse("accounts:register"))

    def test_logout(self):
        self.client.force_login(self.user)
        self.client.post(reverse("accounts:logout"))
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


class CodeLoginTests(TestCase):
    """FR-01 验证码登录：邮箱 + 验证码两个输入框，验证码对了就登录。"""

    EMAIL = "student@example.com"

    def setUp(self):
        self.password = "Passw0rd!234"
        self.user = get_user_model().objects.create_user(
            username=self.EMAIL, email=self.EMAIL, password=self.password
        )

    def _send_code(self, email=None):
        return self.client.post(
            reverse("accounts:send_login_code"), {"email": email or self.EMAIL}
        )

    def test_page_has_exactly_email_and_code_fields(self):
        """页面就是两个输入框：邮箱 + 验证码，没有密码框。"""
        response = self.client.get(reverse("accounts:code_login"))
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual([name for name in form.fields], ["email", "code"])
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'name="code"')
        self.assertNotContains(response, 'type="password"')
        # 发送验证码按钮指向发码接口，并且带 formnovalidate（点它时验证码框还是空的）
        self.assertContains(response, reverse("accounts:send_login_code"))

    def test_send_code_then_login(self):
        self._send_code()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("登录验证码", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.EMAIL])

        response = self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": last_code()}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:home"))
        # 真的登录进去了，能打开需要登录的页面
        self.assertEqual(self.client.get(reverse("core:home")).status_code, 200)

    def test_login_works_without_typing_password(self):
        """不需要密码：整个流程里没有任何 password 字段参与。"""
        self._send_code()
        self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": last_code()}
        )
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(
            self.client.session["_auth_user_backend"], "accounts.backends.EmailCodeLoginBackend"
        )

    def test_wrong_code_rejected(self):
        self._send_code()
        response = self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": "000000"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "验证码不正确")
        self.assertNotIn("_auth_user_id", self.client.session)
        record = EmailVerificationCode.objects.get()
        self.assertEqual(record.attempts, 1)
        self.assertIsNone(record.used_at, "验证码失败不该被消费掉")

    def test_code_is_single_use(self):
        self._send_code()
        code = last_code()
        self.client.post(reverse("accounts:code_login"), {"email": self.EMAIL, "code": code})
        self.client.logout()

        response = self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": code}
        )
        self.assertContains(response, "该验证码已使用")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_is_case_insensitive_email(self):
        self._send_code(email="STUDENT@Example.COM")
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.post(
            reverse("accounts:code_login"), {"email": "Student@example.com", "code": last_code()}
        )
        self.assertEqual(response.status_code, 302)

    def test_unregistered_email_gets_no_code(self):
        """不存在的邮箱绝不发信（否则就是一台给任意地址发信的机器）。"""
        response = self._send_code(email="nobody@example.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(EmailVerificationCode.objects.exists())
        # 明确告诉用户没这个账号，别让人一直等一封不会来的邮件
        page = self.client.get(reverse("accounts:code_login"))
        self.assertContains(page, "该邮箱还没有账号")

    def test_unregistered_email_cannot_login(self):
        response = self.client.post(
            reverse("accounts:code_login"), {"email": "nobody@example.com", "code": "123456"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "还没有账号")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_account_cannot_login(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self._send_code()
        response = self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": last_code()}
        )
        self.assertContains(response, "已停用")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_register_code_cannot_be_used_to_login(self):
        """注册码和登录码互不通用。"""
        other = "newbie@example.com"
        self.client.post(reverse("accounts:send_register_code"), {"email": other})
        register_code = last_code()
        response = self.client.post(
            reverse("accounts:code_login"), {"email": other, "code": register_code}
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(response.status_code, 200)

    def test_resend_needs_cooldown_but_unused_code_blocks(self):
        self._send_code()
        self._send_code()  # 冷却中，不会发第二封
        self.assertEqual(len(mail.outbox), 1)

    def test_relogin_after_code_login_is_not_blocked(self):
        """回归：登录成功消费掉登录码后，再点发送验证码不该被 60 秒冷却挡住。

        登录码被消费后留在库里的是一条废码；若它还参与冷却计算，
        用户退出后想再次用验证码登录就会收到「发送太频繁」。
        """
        self._send_code()
        self.client.post(reverse("accounts:code_login"), {"email": self.EMAIL, "code": last_code()})
        self.client.logout()

        mail.outbox.clear()
        self._send_code()
        self.assertEqual(len(mail.outbox), 1, "登录成功后再发登录码不该被冷却拦住")
        response = self.client.post(
            reverse("accounts:code_login"), {"email": self.EMAIL, "code": last_code()}
        )
        self.assertEqual(response.status_code, 302)

    def test_login_page_links_to_password_login_and_register(self):
        response = self.client.get(reverse("accounts:code_login"))
        self.assertContains(response, reverse("accounts:login"))
        self.assertContains(response, reverse("accounts:register"))

    def test_logged_in_user_is_redirected_home(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("accounts:code_login"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:home"))

    def test_send_login_code_requires_post(self):
        self.assertEqual(self.client.get(reverse("accounts:send_login_code")).status_code, 405)

    def test_code_backend_and_password_backend_stay_separate(self):
        """回归：两条登录路径不能互相串味。

        caller 拿密码走密码后端是完全正常的；但如果请求里带着 `code_login=True`
        （也就是想走验证码那条路），密码后端必须明确拒收，否则「验证码登录」这个
        入口在语义上就被稀释了。Django 的 authenticate() 会把额外的 kwarg 原样透传
        给每个后端，所以这条边界只能在密码后端里显式写出来。
        """
        from django.contrib.auth import authenticate

        # 纯密码凭证：密码后端正常放行
        ok = authenticate(username=self.EMAIL, password=self.password)
        self.assertEqual(ok.backend, "accounts.backends.EmailOrUsernameBackend")
        # 带验证码登录标记的凭证：密码后端拒收
        self.assertIsNone(
            authenticate(username=self.EMAIL, password=self.password, code_login=True)
        )
        self.assertIsNone(
            authenticate(email=self.EMAIL, password=self.password, code_login=True)
        )

    def test_code_backend_only_accepts_one_identity_argument(self):
        """验证码后端只认「email= + code_login=True」，其余一律拒。"""
        from django.contrib.auth import authenticate

        # email 与 username 同时给（第二个会落到 **kwargs）-> 硬拒，避免被当成密码登录形态
        self.assertIsNone(authenticate(username=self.EMAIL, email=self.EMAIL, code_login=True))
        # 正常形态：认
        self.assertEqual(authenticate(email=self.EMAIL, code_login=True).pk, self.user.pk)
        # 没带 code_login 标记：不认
        self.assertIsNone(authenticate(email=self.EMAIL))


class PreferenceTests(TestCase):
    def setUp(self):
        self.user = make_user("pref")
        self.client.force_login(self.user)

    def test_profile_updates_preference(self):
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "store_raw_messages": "",  # 取消勾选 → 不保存原文
                "warn_sensitive": "on",
                "default_reminder_enabled": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        preference = UserPreference.for_user(self.user)
        self.assertFalse(preference.store_raw_messages)
        self.assertFalse(preference.default_reminder_enabled)

    def test_no_default_reminders_when_disabled(self):
        preference = UserPreference.for_user(self.user)
        preference.default_reminder_enabled = False
        preference.save()
        batch, _messages, _warnings = create_batch(self.user, "【通知】请于10月20日前提交实验报告")
        event = process_batch(batch).drafts[0]
        from events.services import confirm_event

        confirm_event(event)
        self.assertEqual(event.reminders.count(), 0)

    def test_import_discards_raw_when_disabled(self):
        preference = UserPreference.for_user(self.user)
        preference.store_raw_messages = False
        preference.save()
        response = self.client.post(
            reverse("ingest:import"), {"text": "【通知】请于10月20日前提交实验报告", "source": "paste"}
        )
        self.assertEqual(response.status_code, 302)
        message = Message.objects.get(owner=self.user)
        self.assertTrue(message.raw_discarded)


class DeleteAccountTests(TestCase):
    def setUp(self):
        self.user = make_user("bye")
        self.client.force_login(self.user)
        batch, _messages, _warnings = create_batch(self.user, "【通知】请于10月20日前提交实验报告")
        process_batch(batch)

    def test_delete_requires_confirmation_word(self):
        self.client.post(reverse("accounts:delete_account"), {"confirm": "nope"})
        self.assertTrue(get_user_model().objects.filter(username="bye").exists())

    def test_delete_account_removes_everything(self):
        self.client.post(reverse("accounts:delete_account"), {"confirm": "删除"})
        self.assertFalse(get_user_model().objects.filter(username="bye").exists())
        self.assertEqual(Event.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)
