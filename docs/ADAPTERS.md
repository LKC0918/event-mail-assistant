# 接入自己的活动来源

捆绑的 `demo` 完全离线；`json` 只读本地文件。真正的目标网站需要独立适配器。请先确认可使用的官方接口或页面读取方式，自己完成登录，不把登录态提交到仓库。

## 统一数据

列表返回完整快照，每个对象至少提供：

```json
{
  "id": "1001",
  "title": "演示活动A",
  "url": "https://events.example.com/events/1001",
  "signup_start_time": "2030-01-01T18:30:00+08:00",
  "signup_end_time": "2030-01-02T11:30:00+08:00",
  "start_time": "2030-01-02T14:00:00+08:00",
  "end_time": "2030-01-02T16:00:00+08:00"
}
```

时间可用秒、毫秒时间戳或 ISO 8601；无时区文本按北京时间解释，建议一律带偏移。ID 必须稳定且仅用字母数字、点、下划线、短横线。URL 必须公开且不含认证信息、查询串和片段；原网站若依赖查询参数，应在私有适配器内保留真实地址映射，公开记录只给安全入口链接。

列表为空、重复 ID、分页读取不完整或字段异常都应抛出 `SourceError`，不覆盖上一次成功快照。此模板选择保守策略：**合法的零活动列表也暂不作为完整快照处理**，需要空集合语义的应用应另行设计并测试。

报名详情另外需要以下字段，完整示例见 `examples/events.json`：

- `is_signup`：当前用户是否已报名，必须是布尔值。
- `event_status`：适配器标准值 `2` 表示当前开放，其余值表示未开放。
- 活动及分组 `total_quota` / `signup_quota`：非负整数；本模板约定 total 为 0 表示不限额，目标平台语义不同时必须转换。
- `sessions`：每项有整数 `id`、`mode`（线上或线下）、`location`、`groups`。
- 每个 group 有整数 `id` 和 `audiences: [{"eligibility_code": "public"}]`。

`profile()` 只返回 `{actor_id, eligibility_code}` 等必要数据。别返回姓名、联系方式或完整个人资料；任务库仅保存账号摘要及确认所需事实，原始用户标识只用于提交。

## 插件接口

在自己的私有模块中实现下列工厂或类，把 `config.json` 的 `adapter` 设为 `my_adapter:Adapter`。该模块须能被 Python 导入，仓库无需收录真实接口实现。

```python
class Adapter:
    supports_submission = False  # 完成验证前保持只读
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.deadline = None
    def events(self, now): ...  # 完整列表，不是某一页
    def detail(self, identity): ...  # 单场详情，ID 必须相符
    def profile(self): ...  # actor_id / eligibility_code
    def registered(self): ...  # 已报名活动及完整起止时间
    def session_version(self): ...  # 账号/会话版本标记，不要返回密码或 Token
    def submit(self, payload): ...  # 仅一次写操作
    def result(self, group_id): ...  # 2=明确成功，1=尚未证实成功
```

执行器会创建多个独立实例并发读，请为每个实例使用自己的连接，不共享可变请求状态。会话变化时让所有实例的 `session_version()` 同步变化，阻止混合账号读写。

每次读取前 `Source` 会设置 `adapter.deadline`（`time.monotonic()` 时基准）。适配器必须把剩余时间传给网络 timeout，并让分页总耗时也受约束。模板会在返回后再检查超时，但 **Python 无法强制中止一个忽略 deadline 的插件线程**。开报并行核验的预算是 15 秒；平时完整扫描默认 120 秒，具体请求默认 15 秒。

提交收到 `{session_id, group_id, actor_id}`，只在私有适配器里映射目标表单。`result` 必须来自权威报名结果，不能把 HTTP 200、请求已接收或页面跳转当成成功。提交异常可能已产生副作用，不要在适配器里偷偷重发 POST。

抛出固定短错误码，例如 `SourceError('network_timeout')` 或 `AuthNeeded('login_required')`。不要把响应体、认证 URL、密码或个人信息塞进错误文本。

## 明确授权

真实报名需要三个条件：支持提交的适配器、配置开启 `signup_enabled` 与 `allow_live_submissions`、当前活动有有效确认。任何一个缺失都不能提交。JSON 适配器即使开关都打开也仍然只读。

持续候补的 `Registration.arm_waitlist(...)`、二选一的 `arm_choice_waitlists(...)` 和 `set_choice_deadline(...)` 是供开发者接入的核心 API，**不是当前 CLI 的按钮，也不会从一句“报名这个”自动推断授权**。调用前需有明确的单场/互斥组范围、方式和截止时间，保留授权引用，不把用户聊天原文存入公开代码。

当前模板只针对一个人自用的邮箱与任务库；多用户服务需要再做身份、租户隔离和配额设计。
