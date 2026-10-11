"""认证后端：允许用邮箱或用户名登录。

注册时 username 存的就是邮箱，但历史账号（如 demo）只有用户名，
所以两种都要认，且邮箱比较不区分大小写。
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend, ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        # 请求带了 code_login 标记就走验证码那条路，密码后端明确让位。
        # （Django 的 authenticate() 会把额外 kwarg 原样透传给每个后端，
        #   不显式挡这一下，两条登录路径的语义就混在一起了。）
        if kwargs.get("code_login"):
            return None
        identifier = (username or kwargs.get("email") or "").strip()
        if not identifier or password is None:
            return None

        user = UserModel.objects.filter(username__iexact=identifier).first()
        if user is None:
            user = UserModel.objects.filter(email__iexact=identifier).order_by("id").first()
        if user is None:
            # 与 ModelBackend 保持一致：不暴露账号是否存在
            UserModel().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def get_user(self, user_id):
        UserModel = get_user_model()
        try:
            return UserModel._default_manager.get(pk=user_id)
        except UserModel.DoesNotExist:
            return None


class EmailCodeLoginBackend(BaseBackend):
    """验证码登录用的后端：只认身份，不认密码。

    验证码的校验已经在 LoginCodeForm 里做完了，这里只负责把
    `authenticate(request, email=..., code_login=True)` 映射到具体用户。

    两条必须守住的边界（缺一条就等于给密码登录开了后门）：
    * **不能继承 ModelBackend**。父类自带 `username` + `password` 校验，一旦继承，
      这个后端本身就是一台可用的密码登录后端，下面那句 `code_login` 检查形同虚设
      （踩过：`authenticate(username=..., password=..., code_login=True)` 会直接放行）；
    * 只认 `email=` 与 `code_login=True` 这一种组合，其余一律返回 None。
      注意 Django 的 `authenticate()` 会把 `password` 从 kwargs 里摘出来，只把
      `**credentials` 转给后端，所以「检查 kwargs 里有没有 password」是无效防御，
      只能靠参数白名单来挡。

    这里不实现权限方法，所以 `get_user()` 必须自己写全。
    """

    def authenticate(self, request, email=None, code_login=False, **kwargs):
        if not code_login or kwargs:
            return None
        identifier = (email or "").strip()
        if not identifier:
            return None

        UserModel = get_user_model()
        user = UserModel.objects.filter(email__iexact=identifier).order_by("id").first()
        if user is None:
            user = UserModel.objects.filter(username__iexact=identifier).order_by("id").first()
        if user is None or not user.is_active:
            return None
        return user

    def get_user(self, user_id):
        UserModel = get_user_model()
        try:
            return UserModel._default_manager.get(pk=user_id)
        except UserModel.DoesNotExist:
            return None
