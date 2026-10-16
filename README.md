# active_function 适配层（astrbot_plugin_active_function_candidate）

给 AstrBot 插件 [`astrbot_plugin_active_function`](https://github.com/L1ke40oz/astrbot_plugin_active_function) 加的一层**隔离适配**：
不修改市场插件本体，而是在运行时把它**限制在指定的 owner 私聊**里，并补上控制流卫生与防重放。

> 版本 `0.1.0`｜作者 Codex｜适用 AstrBot 4.29.x

---

## 为什么需要它

市场版 `active_function`（戳一戳 / 撤回 / 引用等主动动作）本身不区分会话，
在这台机器上可能被别的会话或别的事件触发。本插件作为一层薄封装：

| 能力 | 作用 |
|---|---|
| **限制上游插件** | 启动时拿到上游插件实例，把它的事件范围**限制到 owner 私聊**（`_restrict_upstream_plugin`），退出时还原 |
| **标记戳来源与作用域** | 给事件打上来源标记，便于后续判断"这一轮是不是真的来自 owner 的戳一戳" |
| **清理目标引用缓存** | 避免引用缓存跨轮串味 |
| **控制流卫生** | `strip_unhandled_control_tags` / `_sanitize_control_stream`：清掉本不该出现在正文里的控制标签 |
| **事件重放守卫** | `EventReplayGuard`（TTL + 容量上限）：同一个事件不重复处理 |
| **所有权判定** | `is_owner_private_turn()`：只有 owner 私聊那一轮才放行 |

---

## 配置

本仓库中 owner / 机器人 QQ 号是**占位符**：

| 占位符 | 含义 |
|---|---|
| `100000001` | 使用者本人（owner）的 QQ 号 |
| `100000002` | 机器人自己的 QQ 号 |

部署前请在 `active_function_candidate.py` 中把 `OWNER_UMO` 与 `OWNER_SENDER_ID`
改成你自己的值（`OWNER_UMO` 形如 `qq_napcat_<机器人QQ>:FriendMessage:<你的QQ>`）。

> ⚠️ 判定是**严格相等**：填错时本层会拒绝所有回合（fail-closed），不会误放行到别的会话。

---

## 目录结构

| 文件 | 作用 |
|---|---|
| `main.py` | 插件入口：装载/还原上游插件、注册事件钩子 |
| `active_function_candidate.py` | 核心判定与卫生工具（owner 判定、控制流清理、重放守卫）|
| `action_sender.py` | 主动动作发送（含延迟撤回等）|
| `tests/` | 离线测试 |

## 测试

```bash
PYTHONPATH=. python -m pytest tests -q
```
