"""认证后端：允许用邮箱或用户名登录。

注册时 username 存的就是邮箱，但历史账号（如 demo）只有用户名，
所以两种都要认，且邮箱比较不区分大小写。
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
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
