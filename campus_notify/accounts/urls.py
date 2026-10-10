from __future__ import annotations

from django.contrib.auth import views as auth_views
from django.urls import path

from accounts import views
from accounts.forms import EmailLoginForm

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            authentication_form=EmailLoginForm,
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    # Django 5 起退出登录要求 POST，模板中已使用表单提交
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # 注册（邮箱验证码绑定）
    path("register/", views.register, name="register"),
    path("register/send-code/", views.send_register_code, name="send_register_code"),
    # 找回密码（邮箱验证码）
    path("password-reset/", views.password_reset_request, name="password_reset"),
    path("password-reset/send-code/", views.send_reset_code, name="send_reset_code"),
    path("password-reset/confirm/", views.password_reset_confirm, name="password_reset_confirm"),
    # 账号与数据
    path("profile/", views.profile, name="profile"),
    path("delete/", views.delete_account, name="delete_account"),
]
