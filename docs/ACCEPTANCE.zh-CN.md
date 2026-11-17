# 验收指南（中文）

本文档帮助你在本机逐项核对需求。英文版完整说明见 [README](../README.md)。

## 1. 启动

```bash
make setup     # 创建 .venv、安装依赖、从 .env.example 生成 .env（装了 uv 会自动用 Python 3.12）
make run       # 启动服务，看板地址 http://127.0.0.1:8000
```

## 2. 一键验收（约 30 秒）

另开一个终端：

```bash
make test      # 40 个自动化测试，约 3 秒，不需要网络
make demo      # 重置演示数据后，通过真实 HTTP 依次跑 8 个场景，输出 PASS/FAIL
```

`make demo` 最后一行应为 `8/8 scenarios passed`，并且
`Max updates applied to any single order: 1`（任何订单最多被更新 1 次）。

## 3. 需求 → 实现 → 证据

| 需求 | 实现位置 | 自动化证据 |
|---|---|---|
| 用官方方式验证原始请求体签名 | `app/webhooks.py`：`await request.body()` + `stripe.Webhook.construct_event` | `test_webhooks.py` 中无效签名的 5 种变体、原始字节测试 |
| 先持久化再返回 2xx | `app/events.py::record_delivery` 在事务提交后才返回 200；写库失败返回 5xx，Stripe 会重投 | `test_valid_signature_persists_event_before_2xx` |
| 按事件 ID 去重 | `events.id` 为主键，重复投递只增加 `delivery_count` | `test_duplicate_delivery_is_deduplicated_by_event_id` |
| 超时 / 429 / 5xx 有上限的退避重试 | `app/worker.py`：指数退避（带抖动、上限、遵守 Retry-After），默认最多 5 次 | `test_transient_failures_then_recovery`、`test_backoff_is_exponential_and_capped` |
| 永久性错误进入失败状态 | 其他 4xx、缺少订单号 → `failed`，不重试 | `test_permanent_errors_fail_without_retry` 等 |
| 重试耗尽 | 超过次数 → `failed`，错误信息为 "retries exhausted…" | `test_retries_exhausted_moves_to_failed` |
| 看板：状态、尝试次数、最后错误 | `/` 首页 + `/events/{id}` 详情（含尝试时间线） | `test_dashboard_shows_status_attempts_and_last_error` |
| 手动重放失败事件 | 行内 **Replay** 按钮、**Replay all failed** 批量重放 | `test_manual_replay_recovers_failed_event`、`test_replay_all_failed` |
| 重放与重复投递不会重复更新订单 | 四层保护：事件 ID 主键、租约 + fencing、按 Checkout Session 的幂等键、`pending→paid` 条件更新 | `test_replays_and_redeliveries_together_update_order_once`、`test_concurrent_workers_update_each_order_once` |
| 只用合成数据 | `app/db.py` 中 6 个虚构订单（`*@example.test`） | — |
| 密钥从环境变量读取 | `app/config.py`；仓库只提交 `.env.example`，`.env` 已被 gitignore | — |

## 4. 手动验收（看板里点一遍）

打开 http://127.0.0.1:8000，右侧有 **Offline simulator**（离线模拟器）和 **Fault injection**（故障注入）。
看板每 1.5 秒自动刷新。随时可以点 **Reset demo data** 重来。

1. **正常流程**：模拟器选 `ord_demo_1001` → *Send signed event*。事件变为 `succeeded`，订单 Updates = 1。
2. **重复投递**：在该事件行点 **Redeliver** 两次。提示 "deduplicated"，Deliveries = 3，尝试次数仍为 1。
3. **无效签名**：Signature 选 *invalid* 再发送。提示 400，列表里不会新增事件。
4. **暂时性失败后恢复**：故障注入 `503` × 2，再加 `timeout` × 1，然后发送 `ord_demo_1002`。
   状态会显示 `retrying` 和倒计时，大约 10 秒后变为 `succeeded`（4 次尝试）。
5. **重试耗尽**：故障注入 `503` × 5，发送 `ord_demo_1003`。约 15 秒后变为 `failed`，最后错误为 "retries exhausted after 5 attempts"。
6. **手动重放**：点该行的 **Replay**。变为 `succeeded`，显示 "replayed ×1"，订单 Updates = 1。
7. **响应丢失**：故障注入 `timeout_after_commit`，发送 `ord_demo_1004`。订单服务其实已经更新成功，重试被识别为幂等的空操作，Updates 仍为 1。
8. **永久性错误**：订单选 `ord_does_not_exist`，1 次尝试后直接 `failed`（HTTP 404）。
9. **异步支付**：payment_status 选 *unpaid*，事件为 `ignored`；再点该行的 **Settle payment**，订单变为已支付。
10. **详情页**：点任意事件 ID，可以看到幂等键、每次尝试前等待了多久、每次尝试耗时，以及验证过的原始 payload。

## 5. Stripe CLI 沙箱路径

步骤见 README 的 "Demo path B"。说明：

- 我在本机**没有实际跑过**这条路径（本机没有 Stripe CLI，也没有沙箱账号）。
  离线路径使用的是同一套官方签名验证、持久化和去重代码。
- `stripe trigger … --add checkout_session:metadata.order_id=…` 的参数格式没能在线核实；
  如果你的 CLI 版本不接受，请运行 `stripe trigger --help` 查看。
  推荐用 `scripts/create_checkout_session.py` 创建真实的 Checkout Session，用测试卡 4242 付款。
- 脚本只接受 `sk_test_` / `rk_test_` 开头的测试密钥，拒绝正式密钥。
