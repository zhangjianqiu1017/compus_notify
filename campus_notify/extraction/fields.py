"""字段契约：抽取层与存储层共用的字段/分类/关系定义。

单独成模块是为了避免 events 与 extraction 之间的循环依赖。
"""
from __future__ import annotations

FIELD_TITLE = "title"
FIELD_CATEGORY = "category"
FIELD_COURSE = "course"
FIELD_LOCATION = "location"
FIELD_ORGANIZER = "organizer"
FIELD_LINK = "link"
FIELD_CONTACT = "contact"
FIELD_REQUIREMENT = "requirement"
FIELD_DEADLINE = "deadline"
FIELD_START_AT = "start_at"
FIELD_END_AT = "end_at"

# EventField.field 可选值（title/category 直接落在 Event 主表，其余进 EventField）
FIELD_CHOICES = [
    (FIELD_COURSE, "课程"),
    (FIELD_LOCATION, "地点"),
    (FIELD_ORGANIZER, "主办方"),
    (FIELD_LINK, "链接"),
    (FIELD_CONTACT, "联系人"),
    (FIELD_REQUIREMENT, "要求/材料"),
    (FIELD_DEADLINE, "截止时间"),
    (FIELD_START_AT, "开始时间"),
    (FIELD_END_AT, "结束时间"),
]

FIELD_LABELS = dict(FIELD_CHOICES)
FIELD_LABELS[FIELD_TITLE] = "事件名"

# 可累加字段：多条消息都在补充要求/链接时不应互相覆盖
MULTI_VALUE_FIELDS = {FIELD_REQUIREMENT, FIELD_LINK, FIELD_CONTACT}
# 需要时间归一化的字段
DATETIME_FIELDS = {FIELD_DEADLINE, FIELD_START_AT, FIELD_END_AT}
# 直接映射到 Event 主表的字段（用于列表页排序/筛选）
EVENT_COLUMNS = {
    FIELD_COURSE: "course",
    FIELD_LOCATION: "location",
    FIELD_ORGANIZER: "organizer",
    FIELD_LINK: "link",
    FIELD_CONTACT: "contact",
    FIELD_DEADLINE: "deadline",
    FIELD_START_AT: "start_at",
    FIELD_END_AT: "end_at",
}

CATEGORY_COURSE = "course"
CATEGORY_EXAM = "exam"
CATEGORY_HOMEWORK = "homework"
CATEGORY_ACTIVITY = "activity"
CATEGORY_CONTEST = "contest"
CATEGORY_FEE = "fee"
CATEGORY_OTHER = "other"

CATEGORY_CHOICES = [
    (CATEGORY_COURSE, "课程"),
    (CATEGORY_EXAM, "考试"),
    (CATEGORY_HOMEWORK, "作业"),
    (CATEGORY_ACTIVITY, "活动"),
    (CATEGORY_CONTEST, "竞赛"),
    (CATEGORY_FEE, "缴费"),
    (CATEGORY_OTHER, "其他"),
]

RELATION_PRIMARY = "primary"
RELATION_SUPPLEMENT = "supplement"
RELATION_CORRECTION = "correction"
RELATION_DUPLICATE = "duplicate"

RELATION_CHOICES = [
    (RELATION_PRIMARY, "原始通知"),
    (RELATION_SUPPLEMENT, "补充信息"),
    (RELATION_CORRECTION, "更正信息"),
    (RELATION_DUPLICATE, "重复通知"),
]

RELATION_LABELS = dict(RELATION_CHOICES)

# 分类关键词（规则抽取器与展示兜底共用）
CATEGORY_KEYWORDS = {
    CATEGORY_EXAM: ("考试", "测验", "补考", "期末", "期中", "机考", "笔试", "面试"),
    CATEGORY_HOMEWORK: ("作业", "实验报告", "大作业", "课程设计", "提交", "论文"),
    CATEGORY_CONTEST: ("竞赛", "大赛", "比赛", "挑战赛", "选拔赛", "初赛", "复赛", "决赛"),
    CATEGORY_FEE: ("缴费", "缴纳", "学费", "住宿费", "报名费", "费用"),
    CATEGORY_ACTIVITY: ("活动", "讲座", "报告会", "分享会", "沙龙", "志愿", "社团", "晚会", "运动会"),
    CATEGORY_COURSE: ("课程", "上课", "调课", "停课", "补课", "教学", "选课", "学分"),
}
