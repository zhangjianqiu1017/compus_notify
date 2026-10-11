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
    # 验证码登录（邮箱 + 验证码，不需要密码）
    path("code-login/", views.code_login, name="code_login"),
    path("code-login/send-code/", views.send_login_code, name="send_login_code"),
    # 账号与数据
    path("profile/", views.profile, name="profile"),
    path("delete/", views.delete_account, name="delete_account"),
]
