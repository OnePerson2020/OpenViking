# CAD Understanding 的 OV 消费边界

2026-09-20 清理后。OV 使用普通目录/ZIP 消费当前 DWG、STEP DAG 的产物，不再引入 CAD Bundle 资源模型。

## 当前链路

```text
OV add_resource → Files / Responses → Base Server
  → understand_input → dwg_parse / step_parse → understand_result_storage
  → Responses zip_url → 通用安全解包 → 普通资源发布、摘要和索引
```

STEP ZIP 包含原始 `.step/.stp`、`evidence.json` 和六张 PNG。DWG ZIP 保留原始 DWG、JSON 和各 Sheet 的内容。OV 不执行 CadQuery、OCCT 或 LibreDWG；原生解析和产物检查在算子端完成。普通 ZIP 导入仍使用通用路径安全检查和临时目录失败清理。

## 必要性结论与执行结果

| 范围 | 清理结果 |
| --- | --- |
| Understanding 支持 `.dwg/.step/.stp`，`stp → step` | 保留 |
| Files/Responses、轮询、普通 ZIP 解包和图像消费 | 复用通用实现 |
| 已上传 file_id 创建 response 后的中断恢复 | 保留并改为不依赖文件类型的请求检查点 |
| `_cad_artifact.json` 专属协议、源哈希冻结、Bundle 恢复 | 移除 |
| `atomic_bundle`、`step_bundle`、manifest 封存和原子发布 | 移除 |
| 目录 CAD 子树、同名避让、STEP 专属子请求检查点 | 移除；保留原有通用目录与飞书恢复逻辑 |
| `find_resource` 与对应 HTTP/MCP/SDK 接口 | 移除本次新增部分；普通 find/search 保持原有行为 |
| Bundle 写保护、标签限制、复制移动限制 | 移除本次新增部分 |
| OVPack、reindex、embedding、语义队列的 Bundle 特殊处理 | 移除本次新增部分 |
| STEP 专属 watch 禁令和强制摘要 | 移除；遵循通用 watch 与摘要规则 |
| 旧 Bundle 测试、旧清单校验与发布恢复测试 | 移除，替换为普通 ZIP 和请求恢复回归 |

不再解释旧 `_cad_artifact.json`，也不生成系统 Bundle manifest。旧包不再享有 Bundle 校验、封存、不可变性或模型级检索保证。此改动针对尚未发布的本地实现，不提供已入库 Bundle 数据迁移。

## 请求恢复

队列已有 `understanding_response_id` 时直接复用。队列只有 `understanding_file_id` 时，创建远端 response 后、开始轮询前，将 response ID 保存到任务元数据；同一任务重新执行时改用已保存的 response ID，不重复提交已知请求，也不依赖已经清理的本地上传文件。不同账号不能读取或覆盖检查点，已保存的 response ID 不能替换，同 ID 重复保存不重复写任务。

只保存远端请求身份，不保存 Bundle 发布状态；这不保证整个入库过程具备额外的事务语义。若进程在远端创建成功但本地检查点保存之前退出，仍可能重复提交请求，未新增跨系统幂等协议。普通目录子文件沿用原有处理方式，没有保留 STEP 专属逐文件恢复；已有飞书检查点不变。

## 配置与验证

需要按目标环境启用 `parser_api.enable` 并将 `dwg`、`step`、`stp` 追加到 `parser_api.extensions`；仅增加 supported_extensions 不会自动开启路由。默认配置、部署配置、Base Server 类型/租户准入及 DAG 配置本次均未修改。

主要回归入口：

```sh
.venv/bin/python -m pytest --no-cov \
  tests/parse/test_understanding_api.py \
  tests/parse/test_understanding_api_artifact_images.py \
  tests/service/test_understanding_source_recovery.py \
  tests/service/test_resource_service_understanding_routing.py \
  tests/test_task_tracker.py
```

新测试覆盖普通 DWG/STEP/STP ZIP 的源文件与产物保留、源文件已清理后的 response 恢复、任务存储重载、watch 参数不被 CAD 规则拒绝、检查点异常时的目标清理，以及普通 PDF 请求恢复。HTTP 和存储使用测试替身；线上完整链路与真实模型检索质量另行验收，不把普通目录导入等同于 fork 的 Bundle 检索语义。

本次验证：针对上述代码及目录、ZIP、watch、文件写入/复制锁、OVPack、语义队列和 reindex 共运行 504 项：499 passed、4 failed、1 error。4 个失败均位于原有 `test_resource_service_understanding_routing.py`，原因是预期结果未包含 `source_path`；在进程中载入 HEAD 原版 ResourceService 后复测，同样 4 failed / 1 passed，未改动这些无关断言。1 个 error 是原有 `test_reindex_file_lock.py` 缺少 `agfs_client` fixture；相关生产文件已经恢复到 HEAD，未改动该测试。直接涉及请求恢复、普通解包及飞书兼容的集中回归 172 项全部通过。Ruff 检查与格式检查通过；未进行线上发布或真实模型 E2E。
