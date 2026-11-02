# 从本机演示到常驻服务

## 分阶段接入

先跑 README 的离线演示和测试，再接数据，最后接邮箱和提交。模板只做逻辑演示，不提供某个真实网站的登录脚本。

`config.json` 由 `init` 在你指定的数据目录生成，权限 600，不入 Git。所有相对输入路径都以该数据目录为根。演示模式使用根目录下 `state/demo-events.json`，JSON 模式使用 `source_file` 指定的文件。

接真实邮箱前，在邮箱设置中开启 SMTP 与 IMAP，使用专用授权码；填写服务商公布的主机名。默认使用 TLS 的 465 / 993，不包含 STARTTLS 端口的实现。

```json
{
  "adapter": "demo",
  "mail_transport": "smtp",
  "mail_enabled": true,
  "signup_enabled": true,
  "allow_live_submissions": false,
  "sender": "you@example.com",
  "recipient": "you@example.com",
  "smtp_host": "smtp.example.com",
  "smtp_port": 465,
  "imap_host": "imap.example.com",
  "imap_port": 993,
  "sent_folder": "\"Sent\"",
  "mail_password_env": "EVENT_MAIL_PASSWORD"
}
```

地址和主机均为示例，须换成自己的私有配置。发送和接收使用同一邮箱；适合自己给自己发送提醒并回复。IMAP 的已发送文件夹名称依服务商而异，填准确才能验证回复。某些客户端或服务商的已发送副本、邮件关联头行为不同，必须实际测试，不能只看 SMTP 返回成功。

授权码放在 `EVENT_MAIL_PASSWORD` 环境变量。用密码管理器或权限为 600 的私有环境文件注入，不把它写进命令行、源码、GitHub Secrets 以外的公开文本或聊天。示例 `.env.example` 仅示意变量名，程序不会自动加载 `.env`。

先保留 `adapter=demo`、`allow_live_submissions=false`：这时能测试真实邮件往返，但所有报名仍是假数据。`demo` CLI 本身永远只生成本地预览；测试真实邮件应使用 `run` 并回复它发出的单场活动提醒。

邮件通过后再切换私有适配器；按验收表验证后才允许真实提交。改配置后重启服务；动态关闭 `signup_enabled` 是执行器的额外保护，不代替完整配置重载。

## Linux systemd

示例目录：只读代码 `/opt/event-mail-assistant`；私有配置和状态 `/var/lib/event-mail-assistant`；专用非 root 用户 `event-assistant`。由服务器管理员创建并授权这些目录，把代码复制到代码目录，初始化数据目录：

```bash
cd /opt/event-mail-assistant
python3 -m event_assistant --root /var/lib/event-mail-assistant init
```

初始化须以服务用户或管理员正确设置目录所有权；配置、SQLite 和邮件预览只能由服务用户读取。将私有环境文件放在 `/etc/event-assistant.env`，权限 600，由 systemd 读取。不要把真实值放入本仓库提供的 service 文件。

复制 `deploy/event-assistant.service` 到系统 unit 目录，核对用户和路径后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now event-assistant
sudo systemctl status event-assistant --no-pager
python3 -m event_assistant --root /var/lib/event-mail-assistant status
```

最后一条以有权读该私有数据目录的用户运行。成功判断需要同时满足：unit 在运行、心跳新鲜、最近完整读取正常、邮箱测试实际收到、回信被识别；开启报名后还要看独立执行器心跳。`active` 本身不证明业务成功。

默认轮询 300 秒、提前 15 分钟、回信轮询 30 秒。数据读取和通知还受网络影响，没有固定送达 SLA；接到点任务前预留时间并确认安排回执。

服务自动重启会保留 SQLite 状态，防止重复通知与重复提交。私下备份 `config.json` 和 `state/`，保留权限；**不能把备份上传到公开仓库**。迁移后先只读核验已有任务，别删状态库再直接恢复写入。

如果需要手动到平台报名，先关闭自动报名并停止服务，再到平台查已有结果，防止人工与程序重复操作。重启前核对已有提交状态。
