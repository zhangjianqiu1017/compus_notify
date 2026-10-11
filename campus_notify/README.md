# 校园通知智能管理 APP · Web 版（Django）

把 QQ 群 / 微信群里散落的校园通知，变成可确认、可提醒、可检索的待办与日程。

对应需求文档：`../校园通知智能管理 APP 需求分析文档.md`。本仓库实现该文档中的
**全部 Must 级需求**，并落地了“信息提取与事件归并”的完整处理流程。

---

## 一、快速开始

前提：Python 解释器版本 >= 3.10

### 1.首次启动项目（环境配置 + 数据库迁移 + 启动Django项目）

```shell
cd campus_notify
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 127.0.0.1:8000
```

然后浏览器打开 <http://127.0.0.1:8000>，用演示账号登录：

```shell
账号：demo
密码：DemoPass!2026
```

由于第一次启动的时候已经配置好了环境和数据库，所以之后启动只需要执行“启动Django服务”就好了


```shell
python manage.py runserver 127.0.0.1:8000
```

这个时候可以打开，但是涉及邮箱和大模型识别的任务还做不了，因为没有配置这两项，下面我们需要在.env中配置好这两项


### 2.配置大模型API-Key

创建一个 .env 文件，将 .env.example 文件的内容全部复制过来，然后进行 API-key 和 SMTP 的配置

> 接入大模型是为了解析我们复制过去的通知，将其转化为格式化的数据

在 `campus_notify\.env` 里填：

```ini
EXTRACTION_PROVIDER=auto
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-你的key
LLM_MODEL=deepseek-chat
```

例如：

<img width="1244" height="503" alt="image" src="https://github.com/user-attachments/assets/6f7708af-469a-46b3-bbb6-840c42b1a73e" />


### 3.配置SMTP服务

> 用于注册、登录的验证码发送，而且我们设置的“提醒”也是通过邮件的形式提醒的

以 QQ 邮箱为例，在 `.env` 里填：

```ini
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.qq.com
EMAIL_PORT=465
EMAIL_USE_SSL=1
EMAIL_USE_TLS=0
EMAIL_HOST_USER=你的QQ号@qq.com
EMAIL_HOST_PASSWORD=你的授权码
DEFAULT_FROM_EMAIL=校园通知助手 <你的QQ号@qq.com>
```

例如：

<img width="2020" height="787" alt="image" src="https://github.com/user-attachments/assets/7d884c2c-ce08-4916-aff4-9b0283ae9436" />


SMTP授权码获取可以参考这个视频[QQ获取SMTP授权码_哔哩哔哩_bilibili](https://www.bilibili.com/video/BV1nyNB6sELN/?spm_id_from=333.337.search-card.all.click&vd_source=5dead84e1ffb02f1ce68028fdfd527f8)

## 二、Must 级需求实现对照

| 编号 | 需求 | 实现位置 | 说明 |
| --- | --- | --- | --- |
| FR-01 | 注册、登录、退出账号 | `accounts/` | **邮箱验证码注册**（账号即邮箱）→ 自动登录；登录有两条路：**邮箱 + 验证码**（`/accounts/code-login/`，不用密码）与**邮箱或用户名 + 密码**；找回密码当前不提供 |
| FR-02 | 粘贴文字、系统分享导入通知 | `ingest/` | 粘贴为主入口；`/ingest/share/` 作为 PWA `share_target` 接收系统分享（见下） |
| FR-04 | 提取事件名、时间、地点、任务、截止日期 | `extraction/` | 批次抽取 → 时间归一 → 字段落库，每个字段带**原文证据跨度** |
| FR-05 | 展示识别结果并允许编辑 | `events/views.py::review` + `templates/events/review.html` | 确认门：逐字段可改、原文高亮、歧义提示、疑似重复由用户拍板；确认前不写正式事项 |
| FR-06 | 保存为待办事项 | `events/` `todo.html` | 与日程共用同一份数据，只是不同视图；**确认即自动生成，不需要用户选择保存方式**（`Event.confirm`） |
| FR-07 | 保存为日程 | `events/` `calendar.html` | 月历视图；可导出 `.ics` 给系统日历；与待办同源，确认后同时出现 |
| FR-08 | 设置、修改、关闭提醒 | `reminders/` | 确认时按默认模板生成（截止前 1 天 / 2 小时），之后在提醒页可**逐条添加 / 修改 / 关闭 / 删除**；**时间被更正后自动重排提醒**；提醒到点后**发邮件到注册邮箱**（`reminders/services.py::send_reminder_email`），站内通知留痕 |
| FR-11 | 搜索历史通知和事项 | `core/services.py::search` | 标题 / 正文 / 课程 / 地点 / 字段值，同时检索结构化事项与原文 |
| FR-12 | 标记待办完成状态 | `events/views.py::complete/reopen` | 完成后自动停用该事项未触发的提醒 |
| FR-14 | 删除、导出个人数据 | `core/` | 单条删除、批量清空、删除原文（保留事项）、导出 JSON、导出 `.ics`、注销账号 |

同时覆盖的非功能需求：NFR-01/02（同步路径单次识别调用）、NFR-04（离线提醒补偿）、
NFR-05（歧义不静默保存）、NFR-06（所有结果可编辑）、NFR-07（原文保留）、
NFR-12（数据按用户隔离，见 `IsolationTests`）、NFR-13（生产 HTTPS 开关）、
NFR-14/15/16/17（数据最小化、原文开关、隐私说明页）。MVP 无广告（`ENABLE_ADS=False`）。

> **待办与日程是同一份数据的两个视图**：确认一个事项就等于同时加入两者，
> 确认页不再询问保存方式（`events/models.py::Event.confirm`）。首页据此只给一条
> 「最近 7 天」时间轴（`core/services.py::upcoming`）：一条事项只出现一次，按它最近的那个
> 时间点排序并标注是「截止」还是「开始」；已过截止时间的事项在顶部单独成卡，不在时间轴里重复。
> 待办列表同理（`events/views.py::todo_list`）：时间列同时给出「截止」与「开始」两行，
> 活动的开始时间不会被漏掉；排序与事项列表共用 `EventQuerySet.order_by_time()`——
> 有截止时间看截止，只有开始时间的活动按开始时间排，不会沉到列表底部。

---

## 三、信息提取与事件归并流程

这是本项目的核心。多个消息描述同一事件、同一事件被反复转发、时间被更正——
全都由下面这条链路处理（`extraction/pipeline.py`）：

```
输入（粘贴 / 分享 / 手输）
  │
  ▼
[0] 采集与规范化（ingest/services.py）
    按真实边界切分（发送者/时间戳行、「李四：」前缀；**空行不算边界**）·
    保留发送者与发送时间 · 敏感信息本地扫描 · 原文不可变
  │
  ▼
[1] 预处理路由：无时间锚点的闲聊直接判为“无事件”，不消耗识别调用
  │
  ▼
[2] 批次抽取（extraction/providers/）
    整批消息一次调用 → events[] + 字段 + 证据跨度 + 消息归属关系
    provider 可替换：大模型（OpenAI 兼容）或本地规则抽取器
  │
  ▼
[3] 确定性时间归一（extraction/normalize.py）
    相对时间锚定「消息发送时间」而非「当前时间」；跨年推断；校历“第 N 周”；
    缺时刻按字段补默认值（截止类 23:59）
  │
  ├─→ [4] 候选召回（extraction/merge.py::recall_candidates）
  │       指纹 + 相似度打分取 top-k；同一报名链接直接命中
  │        │
  │        ▼
  │     [5] 三区间归并决策（extraction/merge.py::decide）
  │        score ≥ 0.86 自动合并 / < 0.55 新建 / 中间灰区交给用户确认
  │
  ▼
[6] 字段冲突消解（extraction/resolve.py）
    更正表述 > 发送时间更晚 > 置信度更高；被覆盖的值进历史而非删除
  │
  ▼
[7] 用户确认门（events/views.py::review）
    逐字段核对 + 证据就地标在原文里（高亮片段 + 字段名标签）+ 歧义/低置信度集中提示
    + 疑似重复选择合并/独立
  │
  ▼
[8] 落库（Event / EventField / EventMessage，原文去重幂等）
  │
  ├─→ [9] 待办 / 日程两个视图（同一份数据；确认即同时生成，无需选择保存方式）
  └─→ [10] 提醒编排：更正或延期 → 撤销旧提醒并按新时间重排
              │
              ▼
        [11] 提醒送达（一次派发走两条通道）
              ├─ 邮件：发到注册邮箱，用户不必打开页面就能收到（主通道）
              └─ 站内通知：顶栏角标 + 「提醒 → 通知」页（留痕与兜底）
              │
              ▼
        [12] 反馈闭环：用户每次修改都写入 FieldEditLog（可导出，用于改进识别）
```

### 几个关键设计决定

1. **抽取粒度是二级模型**：一条消息 ≠ 一个事件。同一批次的多条消息可以归并成一个事件
   （`EventMessage.relation` 记录 primary / supplement / correction / duplicate）。
2. **不做全库 LLM 比对**：跨批次归并走「指纹召回 + 规则精排 + 灰区才复核」，
   否则成本和延迟都会失控。
3. **「更正 / 补充」字样的启发式**：这类消息几乎必然指向刚发布的那条通知。
   若时间窗口内只有一个候选事项：更正类表述自动合并（可撤销），补充类表述只提示不自动合并。
4. **误合并 ≫ 漏合并**：合并结果可展开、可 `撤销合并`（详情页「拆出这条」），
   因为漏合并只是重复显示，误合并会让用户丢信息。
5. **概率判断在模型里，确定性计算在代码里**：时间解析、阈值、冲突消解全部可单测（107 个测试）。

### 重复导入会怎样

同一用户下，**原文完全相同（规范化后）的消息只保存一份**（`(batch, message_hash)` 唯一约束）。
这是幂等设计：同一条通知被反复粘贴、连点两次提交、截图重传，都不该在你的待办里长出第二条。

* 一批里**部分**重复 → 只跳过重复的那几条，新内容照常识别；
* 一批里**全部**重复 → 不会只丢一句「完全重复」了事，而是返回导入页并给出一句话：
  「这段内容在 xx-xx xx:xx 已导入过，并且仍关联着下面这些事项，因此没有新建原文。」
  同时给出「仍然重新识别」按钮（不铺开原文预览、事项清单和去重原理——那些是噪音）；
* 点「仍然重新识别」→ **复用已有原文**重跑识别，不产生第二份原文副本。
  此时若该原文已属于某个事项（无论是草稿还是已确认），识别结果**直接并回原事项**，
  不会凭空多出一条重复草稿（`pipeline.process_batch` 里的 `linked_events` 逻辑）。

典型的卡死场景因此被解开：把识别出来的草稿删掉后想重新导入——原文还在库里，
以前会被去重规则永久挡住，现在点一下「仍然重新识别」即可。

---

## 三点五、账号与登录（邮箱验证码）

| 能力 | 说明 |
| --- | --- |
| 注册 | **必须绑定邮箱并通过验证码校验**才建号；账号即邮箱（`username` 存邮箱），注册成功直接登录 |
| 验证码登录 | **邮箱 + 验证码两个框，不需要密码**（`/accounts/code-login/`）。邮箱收码 → 填码 → 登录；验证码正确就进，用户不用记密码 |
| 密码登录 | **邮箱或用户名 + 密码**（`accounts/backends.py::EmailOrUsernameBackend`），邮箱不区分大小写；历史账号仍可用用户名登录 |

> 「忘记密码」**当前不提供**：相关页面、视图、表单与路由已整体移除，密码忘了就用上面的**验证码登录**进去。
> 验证码里的 `reset`（重置密码）用途也一并去掉了（模型里只剩 `register` / `login`）。

两条登录入口并存且互不影响：`/accounts/login/`（密码）与 `/accounts/code-login/`（验证码），两个页面互相有链接。
验证码登录由 `accounts/backends.py::EmailCodeLoginBackend` 负责「验证码已通过 → 换成登录态」，
它**不继承 `ModelBackend`、只认 `email=` + `code_login=True`**，也不做任何密码校验——
否则这个后端本身就会变成一台可用的密码登录后端（踩过这个坑，回归测试已钉住）。

验证码的安全设计（都在 `accounts/services.py`）：

* **只存哈希**（`make_password`），不落明文；
* **有效期** `EMAIL_CODE_TTL_MINUTES`（默认 10 分钟）+ **错误次数上限** `EMAIL_CODE_MAX_ATTEMPTS`（默认 5）+ **重发冷却** `EMAIL_CODE_RESEND_SECONDS`（默认 60 秒）；
* 重新发码即**作废旧码**；验证成功后立即标记已用，**一码只能用一次**，且注册/登录两个用途互不通用；
* **已经用过的废码不再触发重发冷却**——注册与验证码登录都会消费掉自己的码，
  若废码仍参与冷却计算，用户注销后（或退出后）再想用同一邮箱就会被「发送太频繁」挡住；
* **密码不合规时不消费验证码**——否则用户得重新收一次码；
* **只给该发码的邮箱发信**（`SendCodeForm` 的两个开关）：注册要求邮箱没注册过，登录要求邮箱已注册。
  否则系统会变成给任意地址发信的机器，可被用来邮件轰炸；而且「该不该发」的校验必须在发信之前，
  顺序反了被拒的邮箱照样会收到邮件；
* 提示语按场景取舍：注册页明说「该邮箱已注册，可直接登录」，验证码登录页明说「该邮箱还没有账号，请先注册」——
  这里优先保住可用性，用户把邮箱打错时不该一直等一封永远不会来的邮件；
* 开发便利开关 `EMAIL_DEV_SHOW_CODE` 只在 `DEBUG=1` 时生效，生产（`DEBUG=0`）永远关闭。

相关页面：`/accounts/register/`、`/accounts/login/`、`/accounts/code-login/`。

## 三点六、提醒怎么送到（邮件为主，站内为辅）

到点后的提醒**发到注册邮箱**，同时在站内留一份通知。两条通道在一次派发里一起走，
邮件是主通道——用户不必打开页面，提醒才真的起到作用。

**派发时机**（都是同一个 `dispatch_due`，谁先到算谁）：

| 时机 | 位置 |
| --- | --- |
| 打开首页 / 提醒页 / 通知页 | `core/views.py::home`、`reminders/views.py` |
| 页面每 60 秒轮询 `GET /api/notifications/` | `core/api.py` + `static/js/app.js` |
| 计划任务 `python manage.py dispatch_reminders`（建议每分钟） | `reminders/management/commands/` |

**邮件长什么样**（`reminders/services.py::build_reminder_email`）：

```
主题：【即将到期】截止前 2 小时：程序设计竞赛报名

你设置的事项提醒到点了。

时间：10月20日 23:59
地点：实验楼B303
要求：请于10月20日前完成报名，报名链接：https://example.edu/contest
相关链接：https://example.edu/contest
事项详情：https://你的域名/events/1/
```

主题前缀是级别（`【提醒】/【即将到期】/【已过期】`），中间是提醒标签，收件箱列表里
不点开就能判断「哪条通知、还剩多久」。

**几个不能省的细节**：

* **一条提醒只发一封**：`InAppNotification.email_sent_at` 记录送达时间，重复派发不会重复发信；
* **发失败会重试**：SMTP 超时/认证失败只写日志、留空 `email_sent_at`，站内通知照常可见，
  下次派发补发——不会因为网络抖一下就把提醒吞掉；
* **离线补偿**：错过到点的提醒在 `REMINDER_COMPENSATE_DAYS`（默认 3 天）内补发，
  邮件里明确写「已过时间」而不是假装还来得及；超过 3 天的直接作废，不追着人打扰；
* **不会一次轰一堆**：单次派发最多 `EMAIL_REMINDER_MAX_PER_DISPATCH`（默认 5）封，
  剩下的留到下次派发，不是丢弃；
* **用户可关**：「账号」页的「提醒同时发到我的邮箱」关掉后只停邮件，
  站内提醒照旧（否则用户会彻底漏掉提醒）；页面上还直接显示收件邮箱，并说明无法收信时怎么办。

相关配置（`.env`）：

```
EMAIL_REMINDER_ENABLED=1              # 总开关，0 = 只发站内通知
EMAIL_REMINDER_MAX_PER_DISPATCH=5     # 单次派发邮件上限
# EMAIL_REMINDER_FROM=校园通知助手 <no-reply@example.edu>
# SITE_BASE_URL=https://notify.example.edu   # 计划任务派发时拼链接用，生产建议配
```

## 三点七、提示怎么显示（轻提示，不挤页面）

提示不再以横幅形式插在页面顶部——那会把首页/列表的第一屏内容整体下推，用户每次操作后都得重新找位置。
现在所有提示都是 `position: fixed` 的轻提示，脱离文档流，页面内容一动不动：

| 类型 | 位置 | 行为 | 例子 |
| --- | --- | --- | --- |
| 一次性回执 | 右上角 | 4.5~7.5 秒后自动消失；鼠标悬停或键盘聚焦时暂停倒计时 | 保存成功、导入结果、「已送达 N 条提醒」 |
| 待处理状态 | 右下角 | 常驻，直到用户点「✕」关掉 | 首页「N 条识别结果待确认」、事项详情页的核对说明 |
| 错误 | 右上角 | 不自动消失，等用户看清楚再关 | 删除失败、提醒提前时间不合法 |

常驻提示放右下角是因为右上角是「导入 / 编辑 / 删除」这些操作按钮，长期挡住不合适。

* 容器在 `templates/base.html`：`{% block toasts %}`（右上角）、`{% block sticky_toasts %}`（右下角），
  服务端直出，禁 JS 也能看到；
* 行为在 `static/js/app.js`：自动消失、手动关闭、同时最多叠 4 条、按顶栏实际高度让开吸顶导航；
* 样式在 `static/css/app.css` 的 `.toast*`。字段级校验错误、开发模式验证码这类「必须贴着输入框说」的提示
  仍用行内 `.msg`——它们本来就在表单里，不会挤动别的内容。

## 四、目录结构

```
campus_notify/
├── config/            # 项目配置、路由、JSON 接口路由
├── accounts/          # FR-01 账号与隐私偏好
├── ingest/            # FR-02 批次、消息切分、敏感扫描、原文隐私
├── extraction/        # FR-04 识别链路（provider / normalize / merge / resolve / pipeline）
├── events/            # FR-05~07 / FR-12 事项、字段溯源、确认门、待办与日历
├── reminders/         # FR-08 提醒编排与派发
├── core/              # FR-11 / FR-14 首页、搜索、隐私、数据导出与删除
├── templates/         # 服务端渲染模板（无前端构建链）
├── static/            # 样式、少量 JS、PWA manifest 与图标
├── scripts/smoke_http.py   # 端到端 HTTP 冒烟测试
└── tests_helpers.py   # 测试辅助
```

---

## 五、测试与验证

```bash
python manage.py test                 # 167 个单测：时间归一、归并打分、冲突消解、确认门、提醒与邮件、隔离、导出
python manage.py runserver 127.0.0.1:8765
python scripts/smoke_http.py http://127.0.0.1:8765   # 23 项端到端 HTTP 检查
```

`scripts/smoke_http.py` 会用真实 HTTP 走一遍完整链路（含 CSRF、会话、模板渲染）：
注册 → 导入 → 确认页证据高亮 → 确认 → 待办 / 日历 / 提醒 → 搜索 → 完成 → 导出 → 隐私页 → 系统分享导入。
注册已按新的邮箱验证码流程走：脚本会自己点「发送验证码」并从页面取码（需要
`DEBUG=1` 且 `EMAIL_DEV_SHOW_CODE=1`）；若配置了真实 SMTP 不回显验证码，
脚本会退回用演示账号登录，链路仍然被完整验证。

测试重点覆盖了最容易出错的几处：

* 相对时间锚定**消息发送时间**（`NormalizeTests.test_relative_time_anchored_to_message_time`）；
* 一批消息里第一条通知 + 第二条「更正」→ 归并为一个事件且地点被更新；
* 跨批次「报名截止 10/20 改为 10/25」→ 当前值唯一、旧值进历史、**提醒重排到 10/24**；
* 灰区（标题相似但信息不足）→ 不静默合并，留给用户确认；
* 用户 A 无法访问用户 B 的任何数据（详情 / 完成 / 删除均 404）。

---

## 六、部署与运维要点

* **计划任务**：提醒靠 `dispatch_due` 统一派发（站内通知 + 邮件）。用户打开页面或轮询
  `/api/notifications/` 时会派发一次，但**要做到准点收到邮件，必须配计划任务**：
  ```bash
  python manage.py dispatch_reminders            # 建议每分钟一次
  python manage.py dispatch_reminders --dry-run  # 只看有多少条到点，不发信
  ```
  离线期间到点的提醒会在下次派发时补偿（超过 3 天的不再打扰，见 `REMINDER_COMPENSATE_DAYS`）。
  **邮件必须配好 SMTP**（默认的 console 后端只把信打到终端，用户收不到），
  并建议设置 `SITE_BASE_URL`，否则计划任务发出的邮件里链接会退回 `http://localhost`。
* **生产配置**：设 `DJANGO_DEBUG=0` 后会自动开启 HTTPS 跳转、Secure Cookie、HSTS（NFR-13），
  记得同时配置 `DJANGO_ALLOWED_HOSTS`、`DJANGO_SECRET_KEY`、`CSRF_TRUSTED_ORIGINS`，
  并用 `python manage.py collectstatic` 收集静态文件。
* **系统分享导入**：`/static/manifest.webmanifest` 里声明了 `share_target`，
  把站点以 PWA 方式安装后，其他 App 的分享列表会出现「校园通知助手」。
  该入口是跨上下文 POST，因此豁免了 CSRF，但只把文本暂存到会话、不产生任何写库副作用，
  真正的识别必须由用户点击触发。
* **数据最小化**：关闭「保存原文」后，识别完成即丢弃原文，只保留结构化事项；
  `ExtractionRun` 只记录耗时 / 条数 / 失败原因等元数据，不额外留存原文副本。

---

## 七、已知边界（有意为之）

* 本地规则抽取器**不拆分一条消息里的多个独立事项**，且对复杂更正语义（如“原定 A 改为 B”）
  只按变更词之后的时间取值；这类场景建议配置大模型 Provider。
* 截图 OCR（FR-03）属于 Should，本次未实现，`Batch.Source.OCR` 已预留；
  识别失败或图片通知可走「手动新建事项」降级路径。
* 跨渠道全局去重、时间冲突检测（FR-13）、日历同步（FR-15）、多设备同步（FR-16）
  属于后续迭代范围。
* 提醒邮件**直接同步发送**（一次派发最多 5 封，`EMAIL_TIMEOUT` 兜底），不引入消息队列；
  用户量上去后应改为队列 + worker 异步发送，`send_reminder_email` 是唯一的发送入口，改动面很小。
* 邮件里**不带「已读/完成」操作链接**：一键操作需要签名 token 防伪造，当前先只做「点进详情页」。
