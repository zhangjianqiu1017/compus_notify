from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from accounts.models import EmailVerificationCode, UserPreference
from accounts.services import (
    CodeError,
    email_taken,
    find_user_by_email,
    normalize_email,
    verify_code,
)

MAX_USERNAME_LENGTH = 150  # auth.User.username 的上限，邮箱长度不能超过它


class RegisterForm(forms.Form):
    """FR-01 注册：邮箱必须通过验证码校验后才建号。

    账号即邮箱（username 存邮箱），所以登录用「邮箱 + 密码」或「邮箱 + 验证码」。
    """

    email = forms.EmailField(label="邮箱", max_length=254)
    code = forms.CharField(
        label="邮箱验证码", max_length=8, error_messages={"required": "请输入邮箱验证码"}
    )
    password1 = forms.CharField(label="密码", widget=forms.PasswordInput, strip=False)
    password2 = forms.CharField(label="确认密码", widget=forms.PasswordInput, strip=False)

    def clean_email(self) -> str:
        email = normalize_email(self.cleaned_data["email"])
        if len(email) > MAX_USERNAME_LENGTH:
            raise ValidationError("邮箱过长，请换一个。")
        if email_taken(email):
            raise ValidationError("该邮箱已注册，可直接登录。")
        return email

    def clean_code(self) -> str:
        return (self.cleaned_data["code"] or "").strip().replace(" ", "")

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        code = cleaned.get("code")
        if email and code:
            try:
                verify_code(email, EmailVerificationCode.Purpose.REGISTER, code)
            except CodeError as exc:
                self.add_error("code", str(exc))
        password1, password2 = cleaned.get("password1"), cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致。")
        elif password1:
            try:
                validate_password(password1)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self) -> User:
        email = self.cleaned_data["email"]
        user = User.objects.create_user(
            username=email, email=email, password=self.cleaned_data["password1"]
        )
        UserPreference.for_user(user)
        # 建号成功后才消费验证码
        from accounts.services import consume_code

        consume_code(email, EmailVerificationCode.Purpose.REGISTER)
        return user


class EmailLoginForm(AuthenticationForm):
    """登录标识改为邮箱或账号。"""

    username = forms.CharField(
        label="邮箱或账号",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )
    password = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class SendCodeForm(forms.Form):
    """「发送验证码」按钮复用的表单，只关心邮箱。

    用途（注册 / 登录）由视图决定，不作为隐藏字段提交——
    否则可以被前端改成任意值绕过校验。

    两个开关守着「只给该发码的邮箱发信」这条底线（否则系统会变成给任意地址
    发信的机器，可被用来邮件轰炸、也会被当成垃圾邮件跳板）：
    * `require_unregistered`：注册用，已注册的邮箱不发；
    * `require_registered`：登录用，没注册的邮箱不发。
    """

    email = forms.EmailField(
        label="邮箱",
        max_length=254,
        error_messages={"required": "请先填写邮箱", "invalid": "邮箱格式不正确"},
    )

    def __init__(
        self,
        *args,
        require_unregistered: bool = False,
        require_registered: bool = False,
        **kwargs,
    ):
        self.require_unregistered = require_unregistered
        self.require_registered = require_registered
        super().__init__(*args, **kwargs)

    def clean_email(self) -> str:
        email = normalize_email(self.cleaned_data["email"])
        taken = email_taken(email)
        if self.require_unregistered and taken:
            raise ValidationError("该邮箱已注册，可直接登录。")
        if self.require_registered and not taken:
            raise ValidationError("该邮箱还没有账号，请先注册。")
        return email


class LoginCodeForm(forms.Form):
    """FR-01 验证码登录：邮箱 + 验证码，两个输入框，不需要密码。

    验证码正确就把人放进去；邮箱必须已经注册过（这里明说而不是含糊过去，
    否则用户把邮箱打错会一直等一封永远不会来的邮件，不知道该去注册）。
    """

    email = forms.EmailField(label="邮箱", max_length=254)
    code = forms.CharField(
        label="邮箱验证码", max_length=8, error_messages={"required": "请输入邮箱验证码"}
    )

    def clean_email(self) -> str:
        return normalize_email(self.cleaned_data["email"])

    def clean_code(self) -> str:
        return (self.cleaned_data["code"] or "").strip().replace(" ", "")

    def clean(self):
        cleaned = super().clean()
        email = cleaned.get("email")
        code = cleaned.get("code")
        if not email:
            return cleaned

        user = find_user_by_email(email)
        if user is None:
            self.add_error("email", "该邮箱还没有账号，请先注册。")
            return cleaned

        if not user.is_active:
            self.add_error("email", "该账号已停用，请联系管理员。")
            return cleaned

        if code:
            try:
                verify_code(email, EmailVerificationCode.Purpose.LOGIN, code)
            except CodeError as exc:
                self.add_error("code", str(exc))

        cleaned["user"] = user
        return cleaned

    def save(self):
        """验证通过后消费验证码并返回用户（真正的登录动作由视图完成）。"""
        from accounts.services import consume_code

        user = self.cleaned_data["user"]
        consume_code(self.cleaned_data["email"], EmailVerificationCode.Purpose.LOGIN)
        return user


class PreferenceForm(forms.ModelForm):
    class Meta:
        model = UserPreference
        fields = (
            "store_raw_messages",
            "warn_sensitive",
            "default_reminder_enabled",
            "reminder_email_enabled",
        )
        labels = {
            "store_raw_messages": "保存通知原文",
            "warn_sensitive": "检测到敏感信息时提示我",
            "default_reminder_enabled": "保存事项时默认创建提醒（含截止时间的待办）",
            "reminder_email_enabled": "提醒同时发到我的邮箱",
        }
