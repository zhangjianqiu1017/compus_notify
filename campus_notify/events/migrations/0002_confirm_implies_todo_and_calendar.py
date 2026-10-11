"""确认即同时进入待办与日程：把历史数据补齐成同一口径。

新规则下「已确认」等价于「既在待办里、也在日程里」——确认页不再让用户选择
保存方式（见 `Event.confirm`）。这份迁移把此前确认过、却只落在其中一边的
事项补齐，避免新旧数据在待办 / 日历两个视图里表现不一致。

反向迁移不做处理：无法还原用户当初的选择，还原也没有业务意义。
"""
from django.db import migrations

CONFIRMED = "confirmed"
TODO_STATUS_NONE = "none"
TODO_STATUS_PENDING = "pending"


def backfill(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    Event.objects.filter(status=CONFIRMED).update(saved_as_todo=True, saved_as_calendar=True)
    Event.objects.filter(status=CONFIRMED, todo_status=TODO_STATUS_NONE).update(
        todo_status=TODO_STATUS_PENDING
    )


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
