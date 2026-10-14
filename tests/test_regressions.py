"""回归测试：覆盖本轮审查修复的缺陷。

这些用例都是**纯函数 / 纯逻辑**测试，不需要 MySQL、Redis 或上游网络，
因此可以在 CI 里直接跑：

    python -m unittest discover -s tests -p "test_*.py"

每个测试都对应一个真实修过的 bug，并在 docstring 里写明"修之前会怎样"，
避免以后有人重构时把修复悄悄改回去。
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desensitize  # noqa: E402
import upstream_compat  # noqa: E402


class TestDesensitizeKeepsUserInput(unittest.TestCase):
    """修复前：user 消息只要**包含** harness marker 子串就被整条替换成套话，
    用户真实提问被静默丢弃（实测 6 条里有 5 条中招）。"""

    USER_CASES = [
        "<system-reminder>Remember to run the tests</system-reminder>\nNow fix the bug.",
        "Why does my prompt contain <permissions instructions>? Explain it.",
        "Explain what # AGENTS.md instructions does in Codex CLI.",
        "Here is the file:\n<environment_context>\n os: win32\n</environment_context>\nSummarize it.",
        "Please review <skills_instructions> in the docs and tell me what it means.",
        "hello, just a normal question",
    ]

    def test_short_user_questions_are_never_replaced(self):
        for text in self.USER_CASES:
            with self.subTest(text=text[:48]):
                self.assertIsNone(
                    desensitize._compact_harness_message("user", text),
                    "用户消息被 compact 替换掉了（真实提问丢失）")
                self.assertEqual(
                    desensitize._prune_runtime_fragments("user", text), text,
                    "用户消息被 prune 改写了（真实提问被篡改）")

    def test_genuine_injection_is_still_compacted(self):
        """真正的 harness 注入块（以标签开头、成对闭合、超长）仍要被压缩，
        否则脱敏功能形同废止。"""
        real = (
            "<environment_context>\n<cwd>x</cwd>\n</environment_context>\n"
            "<permissions instructions>\n"
            + ("Filesystem sandboxing defines what can be read. " * 30)
            + "</permissions instructions>\n"
        )
        self.assertTrue(desensitize._looks_like_harness_user_message(real))
        self.assertIsNotNone(desensitize._compact_harness_message("user", real))

    def test_system_role_still_compacted(self):
        """system 角色的 Codex harness 提示词仍要被压缩（脱敏的核心用途）。"""
        sys_text = ("You are a coding agent running in the Codex CLI. "
                    "Be precise and helpful. " * 100)
        self.assertIsNotNone(
            desensitize._compact_harness_message("system", sys_text),
            "Codex system prompt 未被压缩 —— 脱敏功能失效")


class TestRuntimeTailMarkersAreAscii(unittest.TestCase):
    """修复前：`_RUNTIME_TAIL_MARKERS` 里混入了零宽空格（U+200B），
    导致「截断巨型 skill 清单」的规则**永远匹配不上**（静默失效）。"""

    def test_no_invisible_characters_in_markers(self):
        for marker in desensitize._RUNTIME_TAIL_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn("\u200b", marker)
                self.assertTrue(marker.isascii(), f"marker 含非 ASCII 字符: {marker!r}")
                self.assertTrue(marker.isprintable(), f"marker 含不可打印字符: {marker!r}")

    def test_skill_tail_marker_matches_real_text(self):
        real = "The following skills are available for use with the Skill tool:\n- x: y"
        self.assertTrue(any(m in real for m in desensitize._RUNTIME_TAIL_MARKERS))


class TestToolArgumentsAreNotRewritten(unittest.TestCase):
    """修复前：sanitize_messages 会对 tool_calls[].function.arguments 做关键词
    改写，把**会被真正执行**的载荷改掉（JSON 仍合法、不报错、无人察觉）：
        {"command": "grep -n 11128 app.log"} -> "grep -n 11-128 app.log"
    """

    def _batch(self, command):
        return {"messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "A", "type": "function",
                 "function": {"name": "bash",
                              "arguments": json.dumps({"command": command})}}]},
            {"role": "tool", "tool_call_id": "A", "content": "ok"},
        ]}

    def test_arguments_survive_full_pipeline(self):
        for command in ("grep -n 11128 app.log",
                        "grep -rn 'x-anthropic-billing-header' src/",
                        "cat docs/11128-notes.md"):
            with self.subTest(command=command):
                obj = self._batch(command)
                upstream_compat.prepare_upstream_body(obj, sanitize=True)
                args = obj["messages"][0]["tool_calls"][0]["function"]["arguments"]
                self.assertIn(command, args)
                self.assertEqual(json.loads(args)["command"], command)


class TestOrphanToolCallCleanup(unittest.TestCase):
    """修复前：assistant 的 tool_calls 是 all-or-nothing —— 只要有一个调用
    还没回结果，**已配对**的调用连同其结果会被一起删除，模型上下文凭空
    少一轮工具往返（可能重复执行已完成的动作）。"""

    def test_paired_call_is_preserved_when_sibling_is_pending(self):
        obj = {"messages": [
            {"role": "user", "content": "do two things"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "A", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                {"id": "B", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "A", "content": "result A"},
        ]}
        upstream_compat.cleanup_orphan_tool_calls(obj)
        asst = [m for m in obj["messages"] if m.get("role") == "assistant"][0]
        ids = [tc["id"] for tc in asst.get("tool_calls") or []]
        self.assertIn("A", ids, "已配对的 tool_call 被误删")
        self.assertNotIn("B", ids, "未配对的 tool_call 未被剔除")
        self.assertTrue(any(m.get("tool_call_id") == "A" for m in obj["messages"]),
                        "A 的结果被连带删除")

    def test_all_orphans_removes_key(self):
        obj = {"messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "Z", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
        ]}
        upstream_compat.cleanup_orphan_tool_calls(obj)
        self.assertNotIn("tool_calls", obj["messages"][0])

    def test_fully_paired_batch_untouched(self):
        obj = {"messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "A", "type": "function", "function": {"name": "b", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "A", "content": "r"},
        ]}
        before = json.dumps(obj, sort_keys=True)
        self.assertEqual(upstream_compat.cleanup_orphan_tool_calls(obj), 0)
        self.assertEqual(json.dumps(obj, sort_keys=True), before)


class TestPrepareUpstreamBody(unittest.TestCase):
    """管理后台此前**没有**调用 prepare_upstream_body，缺少 developer→system
    归一、孤儿 tool_call 清理等，导致上游 400（code=11101/11150）甚至会话
    永久不可用。这里锁定该函数的关键行为。"""

    def test_developer_role_normalised(self):
        b = {"messages": [{"role": "developer", "content": "d"},
                          {"role": "user", "content": "u"}]}
        upstream_compat.prepare_upstream_body(b)
        self.assertEqual(b["messages"][0]["role"], "system")

    def test_stream_forced_with_usage(self):
        b = {"messages": [{"role": "user", "content": "x"}], "stream": False}
        upstream_compat.prepare_upstream_body(b)
        self.assertTrue(b["stream"])
        self.assertTrue(b["stream_options"]["include_usage"])

    def test_idempotent(self):
        b = {"messages": [{"role": "developer", "content": "d"},
                          {"role": "user", "content": "u"}]}
        upstream_compat.prepare_upstream_body(b)
        first = json.dumps(b, sort_keys=True)
        upstream_compat.prepare_upstream_body(b)
        self.assertEqual(json.dumps(b, sort_keys=True), first)


class TestJobRunner(unittest.TestCase):
    """修复前：同 key 的第二次 start() 会把**正在跑**的 Job 从字典里挤掉，
    第一个任务仍在运行却永远取不到进度/结果（幽灵任务）；snapshot() 无锁
    读取；percent 未截断可超过 100%。"""

    def test_concurrent_start_reuses_running_job(self):
        from admin import jobrunner
        runner = jobrunner.JobRunner()
        started, release = threading.Event(), threading.Event()

        def worker(job):
            started.set()
            release.wait(5)
            return {"ok": True}

        first = runner.start("dup", 1, worker)
        self.assertTrue(started.wait(3))
        second = runner.start("dup", 1, worker)
        self.assertEqual(first.id, second.id, "并发 start 覆盖了正在运行的任务")
        self.assertEqual(runner.get("dup").id, first.id)
        release.set()
        time.sleep(1.0)
        self.assertIsNotNone(runner.by_id(first.id), "原任务结果不可达")

    def test_percent_is_clamped(self):
        from admin import jobrunner
        job = jobrunner.Job("k", 3)
        for i in range(5):
            job.add_item({"i": i})
        pct = job.snapshot()["percent"]
        self.assertLessEqual(pct, 100)
        self.assertGreaterEqual(pct, 0)

    def test_snapshot_is_consistent_under_concurrency(self):
        from admin import jobrunner
        job = jobrunner.Job("k", 1000)
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                job.add_item({"i": i})
                i += 1

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for _ in range(200):
                snap = job.snapshot()
                # items 与 done 必须一致（持锁读取的直接收益）
                self.assertEqual(len(snap["items"]), snap["done"])
        finally:
            stop.set()
            t.join(3)


class TestUidWriteLock(unittest.TestCase):
    """修复前：淘汰遍历会把别的线程已取出但尚未进入的锁 pop 掉，
    导致同一 uid 出现两把不同的锁 → 两个线程同时进入临界区 →
    注册去重失效（历史故障：8 并发建出 6 条重复账号）。"""

    def setUp(self):
        import admin.routers.login as login
        self.login = login
        login._uid_locks.clear()

    def test_same_uid_is_serialised(self):
        cur, maxc = [0], [0]
        guard = threading.Lock()

        def worker():
            with self.login._uid_write_lock("u-1"):
                with guard:
                    cur[0] += 1
                    maxc[0] = max(maxc[0], cur[0])
                time.sleep(0.03)
                with guard:
                    cur[0] -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(maxc[0], 1, "同一 uid 出现了并发进入临界区")

    def test_table_drains_and_stays_bounded(self):
        for i in range(300):
            with self.login._uid_write_lock(f"uid-{i}"):
                pass
        self.assertEqual(len(self.login._uid_locks), 0,
                         "锁表没有回收（引用计数未归零）")


class TestSmsQuota(unittest.TestCase):
    """修复前：(a) 失败时无条件清掉手机号 60 秒冷却，攻击者可用
    「失败→归还→再发」循环持续轰炸同一号码；(b) 节流表只增不减（内存泄漏）；
    (c) 使用可伪造的 XFF 首段作为 IP。"""

    def setUp(self):
        import admin.routers.login as login
        self.login = login
        login._sms_log.clear()
        login._sms_mobile_last.clear()

    def test_cooldown_written_only_after_success(self):
        ip, mobile = "203.0.113.1", "13800000001"
        self.login._check_sms_quota(ip, mobile)
        self.assertNotIn(mobile, self.login._sms_mobile_last,
                         "冷却在发送成功之前就被写入（失败归还时会破坏防线）")
        self.login._note_sms_sent(mobile)
        self.assertIn(mobile, self.login._sms_mobile_last)

    def test_failed_requests_cannot_bypass_cooldown(self):
        ip, mobile = "203.0.113.2", "13800000002"
        self.login._check_sms_quota(ip, mobile)
        self.login._note_sms_sent(mobile)          # 成功发出一次 -> 锁定 60s
        for _ in range(5):                          # 反复失败请求
            with self.assertRaises(Exception):
                self.login._check_sms_quota(ip, mobile)
        self.assertIn(mobile, self.login._sms_mobile_last,
                      "失败请求清掉了号码冷却（短信轰炸防线被绕过）")

    def test_refund_restores_ip_bucket(self):
        ip, mobile = "203.0.113.3", "13800000003"
        issued = self.login._check_sms_quota(ip, mobile)
        self.assertEqual(len(self.login._sms_log[ip]), 1)
        self.login._refund_sms_quota(ip, mobile, issued)
        self.assertEqual(self.login._sms_log.get(ip, []), [])

    def test_tables_are_bounded(self):
        maxn = self.login._SMS_TABLE_MAX
        for i in range(maxn + 1000):
            self.login._check_sms_quota(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}",
                                        f"139{i:08d}")
        self.assertLessEqual(len(self.login._sms_log), maxn)
        self.assertLessEqual(len(self.login._sms_mobile_last), maxn)

    def test_per_ip_hourly_cap(self):
        ip = "203.0.113.4"
        allowed = 0
        for i in range(self.login.SMS_MAX_PER_HOUR + 5):
            try:
                self.login._check_sms_quota(ip, f"137{i:08d}")
                allowed += 1
            except Exception:
                break
        self.assertEqual(allowed, self.login.SMS_MAX_PER_HOUR)


class TestGrowthPlans(unittest.TestCase):
    """修复前：TASK_PLANS 里 `RichMeow_Chat` 重复定义（后者静默生效）。"""

    def test_no_duplicate_task_plan_keys(self):
        src = (ROOT / "admin" / "growth_plans.py").read_text(encoding="utf-8")
        keys = re.findall(r'^    "([A-Za-z0-9_]+)":\s*TaskPlan\(', src, re.M)
        dupes = {k for k in keys if keys.count(k) > 1}
        self.assertFalse(dupes, f"TASK_PLANS 存在重复键: {dupes}")

    def test_richtmeow_chat_stays_automated(self):
        from admin import growth_plans
        plan = growth_plans.plan_for("RichMeow_Chat")
        self.assertEqual(plan.level, "auto")
        self.assertEqual(plan.firer, "fire_desktop_chat")

    def test_unknown_task_requires_manual(self):
        from admin import growth_plans
        self.assertEqual(growth_plans.plan_for("完全没登记过的任务").level, "manual")


class TestModelLimitsRetryHeuristic(unittest.TestCase):
    """修复前：`raw >= 1000` 作为秒/毫秒分界，使 999（毫秒语义约 1 秒）
    被当成 999 秒（账号凭空冷却 16.6 分钟）。"""

    def test_millisecond_threshold_is_not_at_1000(self):
        """999 必须按**秒**处理（保守），不能被当成毫秒而压成 0.999 秒。

        原实现 `raw / 1000 if raw >= 1000 else raw` 的问题出现在
        反向区间：任何 ≥1000 的**秒**值会被强行除以 1000。
        """
        from datetime import datetime, timedelta
        from admin.model_limits import queue_retry_time
        now = datetime(2026, 1, 1)
        body = json.dumps({"code": "6020", "data": {"retry_after": 999}})
        when = queue_retry_time(body, now)
        self.assertIsNotNone(when, "queue 错误码未被识别")
        self.assertEqual(when - now, timedelta(seconds=999),
                         "999 秒被误当成毫秒（压成千分之一 → 过早放开限流）")

    def test_large_values_are_treated_as_milliseconds(self):
        from datetime import datetime, timedelta
        from admin.model_limits import queue_retry_time
        now = datetime(2026, 1, 1)
        body = json.dumps({"code": "6020", "data": {"retry_after": 150000}})
        when = queue_retry_time(body, now)
        self.assertIsNotNone(when)
        self.assertEqual(when - now, timedelta(seconds=150),
                         "大数值（毫秒语义）未被折算成秒")


class TestFrontendEscaping(unittest.TestCase):
    """修复前：index.html 的 esc() 不转义单引号，且被用在
    `onclick="fn('${esc(x)}')"` 这种「HTML 属性内的 JS 字符串」上下文 ——
    账号名（来自上游 nickname）里的单引号可注入任意 JS。
    注意：仅给 esc() 补单引号**不够**（HTML 解析器会把 &#39; 解码回 '），
    因此引入了两层转义的 escJs()。"""

    def setUp(self):
        self.html = (ROOT / "admin" / "static" / "index.html").read_text(encoding="utf-8")

    def test_esc_escapes_single_quote(self):
        line = next(l for l in self.html.splitlines() if l.startswith("function esc("))
        self.assertIn("&#39;", line)

    def test_escjs_exists_with_js_escaping(self):
        self.assertIn("function escJs(", self.html)
        m = re.search(r"function escJs\(s\)\{(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(m, "escJs 未定义")
        body = m.group(1)
        self.assertIn("\\\\'", body, "escJs 没有对单引号做 JS 转义")

    def test_inline_handlers_use_escjs_not_esc(self):
        """所有把数据插进内联 JS 字符串参数的 handler 都必须用 escJs。"""
        offenders = []
        for i, line in enumerate(self.html.splitlines(), 1):
            if line.lstrip().startswith("//") or line.lstrip().startswith("*"):
                continue
            for m in re.finditer(r'on(?:click|change|input|submit)\s*=\s*"[^"]*"', line):
                attr = m.group(0)
                # 属性里出现了 JS 字符串参数（'...'）且含 esc( 而非 escJs(
                if re.search(r"'\s*\$\{esc\(", attr):
                    offenders.append((i, attr[:90]))
        self.assertFalse(offenders, f"仍在用 esc() 做 JS 字符串转义: {offenders}")

    def test_statcard_escapes_value(self):
        self.assertIn('class="stat-value">${esc(value)}', self.html)

    def test_toast_uses_textcontent(self):
        self.assertIn("el.textContent = String(msg", self.html)

    def test_importone_has_no_quote_injection(self):
        """encodeURIComponent 不转义单引号，所以不能直接拼进 JS 字符串。"""
        self.assertNotIn("importOne('${encodeURIComponent", self.html)
        self.assertIn("data-file=", self.html)


class TestDockerAndCompose(unittest.TestCase):
    """修复前：Dockerfile 没有 COPY admin/，而 compose 的 admin 服务执行
    `uvicorn admin.server:app` → 容器内 ModuleNotFoundError、无限崩溃重启。
    另外 compose 默认口令是 123456。"""

    def test_dockerfile_copies_admin_package(self):
        df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(df, r"(?m)^\s*COPY\s+admin\s",
                         "Dockerfile 未 COPY admin 包，admin 服务无法启动")

    def test_compose_has_no_weak_default_password(self):
        """检查 env **赋值行**里没有弱口令兜底（注释里提到旧写法不算）。"""
        yml = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        offenders = []
        for line in yml.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"^-\s*ADMIN_PASSWORD\s*=", stripped):
                if ":-" in stripped or "123456" in stripped:
                    offenders.append(stripped)
        self.assertFalse(offenders, f"compose 仍提供弱口令兜底: {offenders}")
        # 必须改为"未设置即失败"的形式
        self.assertRegex(yml, r"ADMIN_PASSWORD=\$\{ADMIN_PASSWORD:\?")

    def test_dockerignore_excludes_env(self):
        di = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertRegex(di, r"(?m)^\.env$", ".dockerignore 未排除 .env（凭据可能进镜像）")


class TestSyncReceiveValidatesCredential(unittest.TestCase):
    """修复前：/api/sync/receive 仅凭共享 HMAC 密钥就往号池写入未校验的
    auth_json（畸形凭据、按 uid 覆盖已有账号）。"""

    def test_sync_receive_calls_validate_credential(self):
        src = (ROOT / "admin" / "routers" / "sync.py").read_text(encoding="utf-8")
        self.assertIn("validate_credential", src)
        self.assertIn("skipped_invalid", src)


class TestConfigFailsClosedOnJwtSecret(unittest.TestCase):
    """修复前：ADMIN_JWT_SECRET 未配置时回退到源码里公开的硬编码弱密钥，
    任何人都能签出合法 admin JWT（默认还监听 0.0.0.0）。"""

    def test_no_hardcoded_default_secret(self):
        # 检查字面量已从源码中移除（注释里出现不算，必须不在赋值中）
        src = (ROOT / "admin" / "config.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("dev-insecure-jwt-secret-change-me", stripped,
                             "仍在使用硬编码 JWT 弱密钥")
        self.assertIn("secrets.token_urlsafe", src)


class TestChatSseAggregation(unittest.TestCase):
    """第三轮审计 F1 / F2：非流式聚合器静默丢内容。

    F1 修复前：`saw_content_delta` 标志定义在 choice 循环**之外**且永不重置，
    只要更早的 chunk 出现过 delta.content，之后所有 `message.content` 回退形态
    都被丢弃 —— "delta:AAA" 后接 "message:BBB" 得到 'AAA'，BBB 消失且不报错。
    F2 修复前：所有 choice 的内容被拼进同一个 message，且固定只输出 index:0，
    第 2 个及以后的候选静默消失。
    """

    @staticmethod
    def _sse(*chunks):
        return "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)

    def test_message_content_fallback_not_dropped(self):
        from admin.routers.proxy import _aggregate_chat_sse
        text = self._sse(
            {"id": "x", "model": "m",
             "choices": [{"index": 0, "delta": {"content": "AAA"}}]},
            {"id": "x", "model": "m",
             "choices": [{"index": 0, "message": {"content": "BBB"}}]},
        )
        out = _aggregate_chat_sse(text)
        self.assertEqual(out["choices"][0]["message"]["content"], "AAABBB",
                         "message.content 回退被丢弃（回答被静默截断）")

    def test_multiple_choices_preserved(self):
        from admin.routers.proxy import _aggregate_chat_sse
        text = self._sse(
            {"id": "y", "model": "m", "choices": [
                {"index": 0, "delta": {"content": "first"}},
                {"index": 1, "delta": {"content": "second"}}]},
            {"id": "y", "model": "m", "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"},
                {"index": 1, "delta": {}, "finish_reason": "stop"}]},
        )
        out = _aggregate_chat_sse(text)
        self.assertEqual(len(out["choices"]), 2, "n>1 的候选被压成了 1 个")
        self.assertEqual(out["choices"][0]["message"]["content"], "first")
        self.assertEqual(out["choices"][1]["message"]["content"], "second")

    def test_reasoning_and_tool_calls_still_aggregate(self):
        from admin.routers.proxy import _aggregate_chat_sse
        text = self._sse(
            {"id": "t", "model": "m", "choices": [{"index": 0, "delta": {
                "reasoning_content": "think ",
                "tool_calls": [{"index": 0, "id": "c1",
                                "function": {"name": "grep",
                                             "arguments": '{"a"'}}]}}]},
            {"id": "t", "model": "m", "choices": [{"index": 0, "delta": {
                "tool_calls": [{"index": 0,
                                "function": {"arguments": ':1}'}}]}}]},
        )
        out = _aggregate_chat_sse(text)
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["reasoning_content"], "think ")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"a":1}')


class TestStickyKeyTenantIsolation(unittest.TestCase):
    """F5：粘性键原本是裸的 sha256(首条 user 消息)。

    攻击者只要猜到受害者首条消息（"hi" / 公开系统提示词），就能算出同一个键，
    把自己的请求钉到受害者的上游账号上（账号定位 + 缓存侧信道 + 单号并发饱和）。
    """

    BODY = {"messages": [{"role": "user", "content": "hi"}]}

    def test_different_keys_get_different_sticky_keys(self):
        from admin import pool
        k1 = pool.session_key_for(self.BODY, "k1")
        k2 = pool.session_key_for(self.BODY, "k2")
        self.assertNotEqual(k1, k2, "不同租户共享了粘性键")
        self.assertTrue(k1)

    def test_sticky_key_is_stable_within_a_tenant(self):
        from admin import pool
        self.assertEqual(pool.session_key_for(self.BODY, "k1"),
                         pool.session_key_for(self.BODY, "k1"))

    def test_attacker_cannot_predict_key_from_prompt(self):
        import hashlib
        from admin import pool
        raw = "d-" + hashlib.sha256(b"hi").hexdigest()[:16]
        self.assertNotEqual(pool.session_key_for(self.BODY, "k1"), raw,
                            "粘性键仍是可预测的裸 prompt 哈希")

    def test_explicit_session_id_still_wins(self):
        from admin import pool
        self.assertEqual(
            pool.session_key_for({"conversationId": "C1", "messages": []}, "k1"),
            "C1")


class TestCsvFormulaInjection(unittest.TestCase):
    """F3：导出的 CSV 里 client_ip / model / 账号名等都是调用方可控的，
    未转义时 Excel/LibreOffice 会把 `=...`、`@...` 当公式**执行**（DDE 可拉起程序）。"""

    def test_dangerous_prefixes_are_neutralised(self):
        from admin.routers.logs import _csv_safe
        for payload in ["=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(1+1)",
                        "\t=1+1", "\r=1+1"]:
            safe = _csv_safe(payload)
            self.assertTrue(safe.startswith("'"),
                            f"{payload!r} 未被中和 -> {safe!r}")

    def test_normal_values_untouched(self):
        from admin.routers.logs import _csv_safe
        for ok in ["192.168.1.1", "gpt-4", "my-account", "", None, 123]:
            self.assertEqual(_csv_safe(ok), ok)


class TestAccountUidUniqueConstraint(unittest.TestCase):
    """F4：代码注释声称「多进程部署时仍需数据库唯一索引兜底」，
    但 DB 层此前**没有任何唯一约束**（全库 0 个），重复 uid 会产生永远清不掉的
    孤儿号 —— 它照常参与选号、照常扣同一份积分。"""

    def test_migration_creates_unique_index(self):
        src = (ROOT / "admin" / "db.py").read_text(encoding="utf-8")
        self.assertIn("_ensure_unique_index", src)
        self.assertIn("uq_accounts_uid", src)
        self.assertIn("_dedupe_accounts_by_uid", src)

    def test_dedupe_runs_before_unique_index(self):
        """顺序不能反：先清理重复行，再建唯一索引，否则建索引必失败。"""
        src = (ROOT / "admin" / "db.py").read_text(encoding="utf-8")
        dedupe_at = src.index('_dedupe_accounts_by_uid() >= 0')
        unique_at = src.index('_ensure_unique_index("accounts", "uq_accounts_uid"')
        self.assertLess(dedupe_at, unique_at)

    def test_sqlite_index_is_partial_to_allow_empty_uid(self):
        """多个空 uid 的账号必须共存 —— 普通唯一索引会让第二个插入失败。"""
        src = (ROOT / "admin" / "db.py").read_text(encoding="utf-8")
        self.assertIn("WHERE", src)
        self.assertIn("IS NOT NULL", src)


class TestCredentialTempFileHardening(unittest.TestCase):
    """F7：AccountSession 用 `tempfile.mktemp()`（不安全 API）把**明文**
    accessToken/refreshToken 写进临时文件，且只靠显式 close() 删除。
    mktemp 只返回名字不创建文件 -> 符号链接竞态可截断任意文件。"""

    def test_uses_mkstemp_not_mktemp(self):
        src = (ROOT / "admin" / "backend.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("tempfile.mktemp(", code, "仍在使用不安全的 mktemp")
        self.assertIn("tempfile.mkstemp(", src)

    def test_finalizer_registered_for_cleanup(self):
        src = (ROOT / "admin" / "backend.py").read_text(encoding="utf-8")
        self.assertIn("weakref.finalize", src)

    def test_file_is_removed_on_close(self):
        import gc
        import os
        import tempfile
        import admin.backend as backend
        s = backend.AccountSession('{"auth":{"accessToken":"X"}}')
        path = s._path
        self.assertTrue(os.path.exists(path))
        s.close()
        self.assertFalse(os.path.exists(path), "close() 未删除凭据临时文件")
        gc.collect()

    def test_finalizer_cleans_up_when_close_is_skipped(self):
        import gc
        import os
        import admin.backend as backend
        s = backend.AccountSession('{"auth":{"accessToken":"Y"}}')
        path = s._path
        del s
        gc.collect()
        self.assertFalse(os.path.exists(path),
                         "未调用 close() 时凭据文件泄漏到磁盘")


class TestBlockingCallSurvivesDoubleCancel(unittest.TestCase):
    """F10：`_blocking_call` 的 `except Exception` 抓不到第二次
    CancelledError（它是 BaseException 子类），on_cancel 永不执行 ——
    AccountSession 及其明文凭据临时文件永不关闭。"""

    def test_on_cancel_runs_even_on_second_cancellation(self):
        import asyncio
        import admin.routers.proxy as proxy_mod

        state = {}

        class Sess:
            closed = False

            def close(self):
                Sess.closed = True

        def slow():
            time.sleep(0.25)
            s = Sess()
            state["s"] = s
            return s

        called = []

        def on_cancel(s):
            called.append(True)
            s.close()

        async def main():
            async def worker():
                return await proxy_mod._blocking_call(slow, on_cancel=on_cancel)
            t = asyncio.create_task(worker())
            await asyncio.sleep(0.05)
            t.cancel()
            await asyncio.sleep(0.22)
            t.cancel()
            try:
                await t
            except BaseException:
                pass
            await asyncio.sleep(0.05)

        asyncio.run(main())
        self.assertTrue(called, "双重取消时 on_cancel 未执行（session 泄漏）")
        self.assertTrue(Sess.closed, "AccountSession 未关闭")


class TestGatewayHeartbeatDoesNotKillHealthyStream(unittest.TestCase):
    """F8：续期失败（或一次 Redis 抖动）就 `owner.cancel()`，
    只配并发限制时 ZSET 会自然过期 -> `_RENEW` 返回 0 -> 健康的 2 分钟长流被强杀。"""

    def test_renew_tolerates_transient_failures(self):
        src = (ROOT / "admin" / "gateway_limits.py").read_text(encoding="utf-8")
        self.assertIn("misses", src, "续期逻辑没有失败容忍计数")
        # 不能出现「一次失败即取消」
        self.assertNotIn("                except Exception:\n                    pass\n                owner.cancel()",
                         src)


class TestSchedulerLogsFailures(unittest.TestCase):
    """F9：调度循环把异常全部吞掉且**零日志**，失败在日志里毫无痕迹。"""

    def test_loop_logs_exceptions(self):
        src = (ROOT / "admin" / "scheduler.py").read_text(encoding="utf-8")
        self.assertIn("_logger", src)
        self.assertIn("_logger.exception", src)


class TestAdminTokenRevocation(unittest.TestCase):
    """安全加固：JWT 带 `ver` 版本号，改密码 / 登出立即作废所有已签发令牌；
    同时强制要求 exp（否则可签出永不过期令牌）；
    旧的明文口令分支已移除（否则可写库即可登录）。"""

    def test_token_carries_version_claim(self):
        src = (ROOT / "admin" / "security.py").read_text(encoding="utf-8")
        self.assertIn('"ver"', src)
        self.assertIn("bump_token_version", src)

    def test_exp_claim_is_required(self):
        src = (ROOT / "admin" / "security.py").read_text(encoding="utf-8")
        self.assertIn('"require": ["exp"]', src)

    def test_plaintext_password_branch_removed(self):
        src = (ROOT / "admin" / "security.py").read_text(encoding="utf-8")
        self.assertNotIn("hmac.compare_digest(pw, stored)  # 旧明文兜底", src)

    def test_pbkdf2_iterations_are_bounded(self):
        src = (ROOT / "admin" / "security.py").read_text(encoding="utf-8")
        self.assertIn("10_000_000", src, "PBKDF2 迭代次数无上限 -> 可被 DoS")

    def test_logout_endpoint_exists(self):
        src = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/admin/logout", src)

    def test_password_change_bumps_version(self):
        src = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
        idx = src.index("def change_password")
        body = src[idx:idx + 2000]
        self.assertIn("bump_token_version", body,
                      "改密码未作废已签发的令牌")


class TestNonStreamAggregationAccumulates(unittest.TestCase):
    """脆弱点修复：非流式消费端原来是 `agg_out = chunk`（覆盖）。
    当前聚合模式恰好只 yield 一次所以能用，但新增一条 yield 就会静默丢数据。"""

    def test_consumer_accumulates_chunks(self):
        src = (ROOT / "admin" / "routers" / "proxy.py").read_text(encoding="utf-8")
        self.assertIn("agg_chunks.append(chunk)", src)
        self.assertNotIn("agg_out = \"\"\n    async for chunk in _stream(aggregate=True):\n        agg_out = chunk",
                         src)


class TestPasswordSourcePrecedence(unittest.TestCase):
    """回归：`_get_stored_hash()` 原本在**没配 ADMIN_PASSWORD 环境变量**时
    直接返回空串，压根不看数据库 —— 于是「后台修改密码」永远不会生效，
    而桌面版恰恰是把随机密码写进数据库、不设环境变量的用法，登录恒定 503。

    修复后：env > 数据库 > 无凭据（拒绝登录）。"""

    def test_env_not_set_falls_back_to_db(self):
        src = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
        idx = src.index("def _get_stored_hash")
        body = src[idx:idx + 2600]
        # 不允许出现「没配 env 就直接 return ""」的短路
        self.assertNotIn('    if not settings.ADMIN_PASSWORD:\n        return ""',
                         body, "未配置 env 时仍直接返回空，数据库密码永远读不到")
        self.assertIn("row.value", body)

    def test_login_uses_resolved_hash_not_raw_env(self):
        """登录的「未配置凭据」判定必须看解析后的哈希，否则桌面版恒定 503。"""
        src = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
        idx = src.index("def login(")
        body = src[idx:idx + 3000]
        self.assertIn("stored_hash = _get_stored_hash()", body)
        self.assertNotIn("not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD",
                         body, "登录仍以原始 env 判定凭据，桌面版会恒定 503")

    def test_change_password_refuses_when_password_comes_from_env(self):
        """env 指定密码时改库无意义（会被覆写），必须明确拒绝而不是假装成功。"""
        src = (ROOT / "admin" / "server.py").read_text(encoding="utf-8")
        idx = src.index("def change_password")
        body = src[idx:idx + 2200]
        self.assertIn("ADMIN_PASSWORD_FROM_ENV", body)
        self.assertIn("409", body, "未明确拒绝 env 来源的改密请求")


class TestDesktopPackagingGuards(unittest.TestCase):
    """打包踩过的两个坑（都已固化成构建期守卫）。

    坑 1：conda 解释器把 libssl / libcrypto / sqlite3 放在 `Library\\bin`，
    PyInstaller 不扫描该目录 → EXE 构建 exit 0、双击即崩
    `ImportError: DLL load failed while importing _ssl`。
    坑 2：`proxy_tools` 间接拉入 setuptools → 触发 `pyi_rth_pkgres` 钩子 →
    `jaraco.context` 需要 `backports` 命名空间包 → EXE 崩在
    `ModuleNotFoundError: No module named 'backports'`。
    """

    def test_build_script_rejects_conda_layout(self):
        src = (ROOT / "scripts" / "build_desktop.py").read_text(encoding="utf-8")
        self.assertIn("_check_interpreter_layout", src)
        self.assertIn("Library", src)
        self.assertIn("libssl", src)

    def test_build_script_self_checks_the_exe(self):
        """只看构建退出码会把坏包发出去 —— 必须真的把 EXE 拉起来验证。"""
        src = (ROOT / "scripts" / "build_desktop.py").read_text(encoding="utf-8")
        self.assertIn("_verify_executable", src)
        self.assertIn("--backend", src)

    def test_self_check_avoids_nonce_gated_endpoint(self):
        """desktop-ready 需要 WB_DESKTOP_BOOT nonce，裸探测永远 404；
        用它做探针会让完全正常的 EXE 被判为坏包。

        注意断言的是「**探针 URL**」而不是「文件里是否出现该字符串」——
        该字符串在解释原因的注释里是允许出现的。
        """
        src = (ROOT / "scripts" / "build_desktop.py").read_text(encoding="utf-8")
        urls = re.findall(r'urlopen\(\s*\n?\s*f?"([^"]+)"', src)
        self.assertTrue(urls, "未找到自检探针 URL")
        for u in urls:
            self.assertNotIn("desktop-ready", u,
                             f"自检用了 nonce 门槛端点 {u}，正常 EXE 会被误判")

    def test_self_check_does_not_deadlock_on_pipe(self):
        """onefile EXE 会派生子进程；用 PIPE + terminate() 会让
        `stdout.read()` 永久阻塞（构建卡死）。必须重定向到文件。"""
        src = (ROOT / "scripts" / "build_desktop.py").read_text(encoding="utf-8")
        idx = src.index("def _verify_executable")
        body = src[idx:]
        self.assertNotIn("subprocess.PIPE", body)
        self.assertIn("_kill_tree", body)

    def test_spec_excludes_pkg_resources(self):
        spec = (ROOT / "desktop" / "WorkBuddy2API.spec").read_text(encoding="utf-8")
        self.assertIn("pkg_resources", spec)
        self.assertIn("setuptools", spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
