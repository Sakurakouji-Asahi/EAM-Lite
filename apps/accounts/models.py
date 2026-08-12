from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    REQUIRED_FIELDS = [*AbstractUser.REQUIRED_FIELDS, "display_name"]

    display_name = models.CharField("显示名称", max_length=100)
    email = models.EmailField("电子邮箱", blank=True)
    mobile = models.CharField("手机号码", max_length=32, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "用户"
        verbose_name_plural = "用户"

    def __str__(self):
        return self.display_name or self.username
