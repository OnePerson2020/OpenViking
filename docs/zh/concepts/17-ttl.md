# 目录 TTL

TTL 默认关闭，作用于用户和 peer 的 `events/YYYY/MM/DD` 日期目录及 `sessions/{session_id}`。resources 和其他记忆类别不在范围内。

## 配置与全量应用

配置入口保留库全局、类型默认值，以及以下根目录：

- `viking://user/{user_id}/memories/events`
- `viking://user/{user_id}/peers/{peer_id}/memories/events`
- `viking://user/{user_id}/sessions`

优先级为具体根目录 → 类型默认值（`user_events`、`peer_events`、`sessions`）→ 库全局 → 关闭。`disabled` 阻断继承，`inherit` 回退。account 配置沿用现有库配置入口；user/account 身份不增加新的优先级。

年月、日期、单 Session、子目录和文件只展示期限，不支持编辑。首次启用和修改根策略会按优先级更新仍有效的已有目录，并为历史未纳管目录补 TTL。相对期限按原业务时间加新天数重算，绝对期限取配置值；缩短策略可能立即过期。关闭有效策略会清除仍有效目录的期限，已过期或已删除的对象不会恢复。

配置请求按页扫描、限制并发，并等待元数据和到期索引更新。部分失败会返回未完成的目录；重试同一份配置即可继续。相对策略下，历史目录缺少可靠原始时间时会报告未完成，不使用修改配置的时间或目录 modTime 代替。空 event 日期目录可以创建，首次写入正文后才开始计时。

## 期限与续期

每个生命周期目录在 `.meta.json` 保存一个 `expires_at`，相对策略额外保存 `ttl_days`；沿用 `received_at` 记录内容时间。events 兼容读取旧 `.ttl.json`。AGFS 元数据更新保留同一文件中的其他业务字段，目录 stat 返回该目录自己的 `expires_at`。

- events：第一次成功写入内容时开始计时，路径日期仅用于归组。后续追加或修改正文不自动续期；用户修改根策略可以调整仍有效目录的期限。
- Session：创建时继承 `sessions` 根策略。相对策略在成功追加消息、完成有内容的 commit 后，按保存的 `ttl_days` 续期；根策略更新会同步修改该天数。任务重放沿用原完成时间。
- 读取、摘要生成、重建索引、失败写入、空消息批次和空 commit 不续期。绝对时间不自动延长。

目录内的消息、附件、归档、L0/L1/L2 共用同一到期时间，不做 JSONL 消息级 TTL。无 `ttl_generation`、单 Session 覆盖或逐文件模式。

## 可见性

UTC 时间达到 `expires_at` 后，目录及全部后代不可见。直接访问返回 404；Session 列表、文件列表、find/search/recall、grep/glob 会过滤到期内容，并补足可见候选。

结构化对象回显实际所属目录的 `expires_at`，未开启时明确返回 `null`。跨目录结果逐项回显；单对象的文本或列表响应在外层提供期限。纯 URI 列表兼容模式和下载字节格式不变，仍执行服务端过滤。年月及根目录没有共同期限，返回 `null`；根目录另展示 `policy`、`effective_policy`。

## 清理任务与性能

任务沿用 Session commit 的 QueueFS 离线执行框架。调度器按持久化到期索引领取候选，受每轮数量、字节和时间预算限制，默认在天级窗口内分散物理删除。

删除前以元数据文件锁复查目录期限；Session 复用现有 Session 写入互斥锁。目录内逐文件加锁删除，锁忙则重试，不使用 tree 锁，不逐文件判断过期时间。删除包括目录内全部正文、消息、附件、L0/L1、向量和 Meta；元数据最后删除，确认存储与索引清空后才移除到期登记。目录外的上层摘要保持原样，删除不触发 LLM、embedding 或摘要重建。

查询在同一批结果中复用 owner 元数据读取。清理重试保存在到期登记中，任务历史的保留时长不影响正确性。显示和计费允许随异步物理删除延迟；云端计费、网关转发和备份不由 OV 清理完成状态自动证明。

## 接口

- [TTL 配置](../configuration/01-server.md#ttl)：库、类型及根目录策略。
- [期限查询](../api/12-content.md#文档到期时间)：`GET /api/v1/content/ttl`，SDK/MCP `get_ttl`，CLI `ov ttl get`。
- [Session](../api/05-sessions.md#session-ttl)：统一继承根目录策略，创建/配置接口不接收 TTL 参数。

异步 Session commit 写回会在短暂的 Session 锁内核对原 Phase 1 的 `task_id`。旧任务不能把已删除 Session 的结果写入复用同 ID 的新 Session，也不能从已删除来源重建 event。新的 Session 仍可导入历史日期的事件。普通正文 I/O 不持有公共元数据锁；清理在阻止新写入后检查已有文件锁，包括正文尚未落盘的写入。
