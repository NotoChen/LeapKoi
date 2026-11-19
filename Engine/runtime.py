from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Mapping

import listener_core as core
from avatars import SenderAvatars, lookup_sender_avatar
from configuration import configure_core
from lifecycle import startup_decision
from protocol import Events, EventConsole, EventNotifier


class PolicyGate(core.ScreenLockGuard):
    def __init__(self, console: EventConsole, settings: Callable[[], dict[str, Any]]):
        super().__init__(console)
        self.settings = settings
        self.paused = threading.Event()
        self.locked = False

    def refresh(self) -> bool:
        self.locked = super().is_locked_or_unknown()
        return self.locked

    def reason(self) -> str:
        if self.paused.is_set():
            return "paused"
        if self.locked and not self.settings()["process_while_locked"]:
            return "locked"
        return ""

    def disposition(self) -> str:
        reason = self.reason()
        return self.settings()[f"{reason}_message_policy"] if reason else "queue"

    def wait_until_unlocked(self, stop_event: threading.Event, action: str) -> bool:
        announced = False
        while not stop_event.is_set():
            self.refresh()
            reason = self.reason()
            if not reason:
                return True
            if not announced:
                self.console.info(f"{'已手动暂停' if reason == 'paused' else '电脑已锁屏'}，等待恢复后{action}")
                announced = True
            stop_event.wait(0.3)
        return False


class ApplicationEngine(core.ApprovalEngine):
    def __init__(self, *args: Any, settings: dict[str, Any], events: Events, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.settings = settings
        self.events = events
        self.task_id = ""
        self.configured_leader = self.leader_identity
        self.progress: Callable[[], None] = lambda: None
        self.active_mrs = 0
        self._active_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stages: dict[str, str] = {}
        self._approved_refs: set[str] = set()
        self._merged_refs: set[str] = set()

    def _mr(self, ref: core.MRRef, **fields: Any) -> None:
        self.events.send("mr", task_id=self.task_id, key=ref.key, **fields)

    def _set_stage(self, ref: core.MRRef, stage: str) -> None:
        with self._state_lock:
            self._stages[ref.key] = stage

    def _stage_for(self, ref: core.MRRef) -> str:
        with self._state_lock:
            return self._stages.get(ref.key, "preflight")

    def _mark_approved(self, ref: core.MRRef) -> None:
        with self._state_lock:
            self._approved_refs.add(ref.key)

    def _mark_merged(self, ref: core.MRRef) -> None:
        with self._state_lock:
            self._merged_refs.add(ref.key)

    def _clear_state(self, ref: core.MRRef) -> None:
        with self._state_lock:
            self._stages.pop(ref.key, None)
            self._approved_refs.discard(ref.key)
            self._merged_refs.discard(ref.key)

    def _preflight_one(self, ref: core.MRRef, index: int, total: int) -> core.MRPlan:
        self._set_stage(ref, "preflight")
        self._mr(ref, status="preflight", stage="preflight")
        try:
            plan = super()._preflight_one(ref, index, total)
        except core.AutomationError:
            details = getattr(self.glab, "details_cache", {}).get(ref.key)
            if details is not None:
                self._publish_details(details)
            raise
        self._publish_details(plan.details)
        self._publish_approvals(ref, plan.approvals, plan.approvals_known)
        return plan

    def _publish_details(self, details: core.MRDetails, actor: str | None = None) -> None:
        ref = details.ref
        counts = getattr(self.glab, "commit_counts", {})
        self._mr(ref, url=details.web_url or ref.url, title=details.title, source_branch=details.source_branch,
                 target_branch=details.target_branch, changes_count=details.changes_count,
                 commit_count=counts.get(ref.key), author=details.author_name,
                 sha=details.sha, created_at=details.created_at, updated_at=details.updated_at,
                 merged_by=details.merged_by_name, **({"actor": actor} if actor is not None else {}))

    def _publish_approvals(
        self,
        ref: core.MRRef,
        approvals: core.ApprovalInfo | None,
        known: bool,
    ) -> None:
        self._mr(
            ref,
            approvers=list(self._approver_names(approvals)),
            approvals_known=known,
            approved_by_own=self._approved_by_own(approvals),
        )

    def _on_merge_confirmed(self, details: core.MRDetails) -> None:
        super()._on_merge_confirmed(details)
        self._mark_merged(details.ref)
        self._publish_details(details)

    def _approve_and_verify(self, plan: core.MRPlan) -> core.ApprovalInfo:
        self._set_stage(plan.ref, "approval")
        self._mr(plan.ref, status="approving", stage="approval")
        result = super()._approve_and_verify(plan)
        self._mark_approved(plan.ref)
        self._mr(plan.ref, approved=True, actor=plan.actor.name)
        self._publish_approvals(plan.ref, result, True)
        return result

    def _merge_attempt(
        self,
        plan: core.MRPlan,
        *,
        verify_before_write: bool = False,
    ) -> core.MRDetails:
        self._set_stage(plan.ref, "merge")
        self._mr(plan.ref, status="merging", stage="merge")
        return super()._merge_attempt(
            plan,
            verify_before_write=verify_before_write,
        )

    def _ensure_commit_count(self, ref: core.MRRef) -> None:
        counts = getattr(self.glab, "commit_counts", None)
        if isinstance(counts, dict) and ref.key in counts:
            return
        task_id = self.task_id

        def load() -> None:
            self.console.local.task_id = task_id
            self.console.local.mr_key = ref.key
            try:
                self.glab.commit_count(ref)
            except core.AutomationError as error:
                self.console.warning(f"{ref.key} 无法读取提交数，不影响审批结果：{error}")
                self.console.debug(error.detail)
                return
            if isinstance(counts, dict):
                self.events.send(
                    "mr",
                    task_id=task_id,
                    key=ref.key,
                    commit_count=counts.get(ref.key),
                )

        threading.Thread(
            target=load,
            name="commit-count",
            daemon=True,
        ).start()

    def _process_one(self, ref: core.MRRef, index: int, total: int):
        started = time.monotonic()
        self.console.local.task_id = self.task_id
        self.console.local.mr_key = ref.key
        with self._active_lock:
            self.active_mrs += 1
        self.progress()
        try:
            self._wait_until_unlocked("处理 MR")
            result = super()._process_one(ref, index, total)
            # Commit count is display-only metadata. Fill it in after all
            # approval/merge writes, without delaying the completed state.
            if result.merged_count:
                self._ensure_commit_count(ref)
            self._mr(ref, status="failed" if result.error else "completed",
                     stage=self._stage_for(ref),
                     error=result.error or "", elapsed=time.monotonic() - started,
                     owned_merge=result.owned_merge,
                     approvers=list(result.approvers),
                     approvals_known=result.approvals_known,
                     approved_by_own=result.approved_by_own,
                     merged_by=result.merged_by,
                     merge_owner_known=result.merge_owner_known)
            return result
        except core.ProcessingStopped:
            self._mr(ref, status="stopped", stage=self._stage_for(ref),
                     elapsed=time.monotonic() - started)
            raise
        except Exception as error:
            # Keep the failure attached to this MR instead of aborting the
            # whole message. Other MRs may already have changed GitLab state.
            stage = self._stage_for(ref)
            message = f"{ref.key} 处理异常：{error}"
            self.console.error(message)
            self.console.debug(repr(error))
            with self._state_lock:
                approved = ref.key in self._approved_refs
                merged = ref.key in self._merged_refs
            self._mr(ref, status="failed", stage=stage, error=message,
                     elapsed=time.monotonic() - started)
            return core.MRResult(
                approved_count=1 if approved else 0,
                merged_count=1 if merged else 0,
                error=message,
            )
        finally:
            with self._active_lock:
                self.active_mrs -= 1
            self.progress()
            self._clear_state(ref)

    def process(self, task: core.MessageTask) -> None:
        self.task_id = task.event_key
        self.own_user_ids = {self.primary_identity.user_id}
        configured_leader = getattr(self, "configured_leader", None)
        if self.settings["include_fallback_for_received"] and configured_leader:
            self.own_user_ids.add(configured_leader.user_id)
        total = len(task.refs)
        results: dict[int, Any] = {}
        started = time.monotonic()
        stopped = False
        stopped_count = 0
        if total == 1:
            try:
                results[1] = self._process_one(task.refs[0], 1, 1)
            except core.ProcessingStopped:
                stopped = True
                stopped_count = 1
                results[1] = core.MRResult()
            except Exception as error:
                message = f"任务处理异常：{error}"
                self.console.error(message)
                self.console.debug(repr(error))
                results[1] = core.MRResult(error=message)
        else:
            with ThreadPoolExecutor(max_workers=min(total, self.settings["max_concurrent_mrs"]), thread_name_prefix="approval-mr") as pool:
                futures = {pool.submit(self._process_one, ref, i, total): i for i, ref in enumerate(task.refs, 1)}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        results[index] = future.result()
                    except core.ProcessingStopped:
                        stopped = True
                        stopped_count += 1
                        results[index] = core.MRResult()
                    except Exception as error:
                        message = f"任务处理异常：{error}"
                        self.console.error(message)
                        self.console.debug(repr(error))
                        results[index] = core.MRResult(error=message)
        approvals = sum(r.approved_count for r in results.values())
        merges = sum(r.merged_count for r in results.values())
        errors = [r.error for r in results.values() if r.error]
        owned = sum(r.owned_merge for r in results.values() if r.merged_count)
        unknown_owner = sum(
            not r.owned_merge and not r.merge_owner_known
            for r in results.values()
            if r.merged_count
        )
        other_owner = max(0, merges - owned - unknown_owner)
        receipt_owned = sum(r.receipt_eligible for r in results.values())
        received = False
        receipt_error = ""
        should_receive = not stopped and self.settings["send_received"] and receipt_owned > 0 and (
            self.settings["received_condition"] == "any_owned_merge" or not errors
        )
        if should_receive:
            self._wait_until_unlocked("添加收到表态")
            try:
                self.dws.add_received(task.conversation_id, task.message_id)
                received = True
                self.console.success("已给原消息添加收到表态")
            except core.AutomationError as error:
                receipt_error = str(error)
                self.console.error(f"MR 处理已结束，添加收到失败：{error}")
        status = "stopped" if stopped else "completed"
        if not stopped and (errors or receipt_error):
            status = "partial" if approvals or merges or owned else "failed"
        summary = f"{total} 个 MR：本次本机审批 {approvals} 个，已合并 {merges} 个"
        if merges:
            summary += f"（本机合并 {owned} 个，其他成员合并 {other_owner} 个"
            if unknown_owner:
                summary += f"，合并人未知 {unknown_owner} 个"
            summary += "）"
        if stopped_count:
            summary += f"，{stopped_count} 个未完成"
        if errors:
            summary += f"，{len(errors)} 个处理失败"
        if stopped:
            summary = f"监听已停止：{summary}"
        if received:
            summary += "，已添加收到"
        self.events.send("task_update", id=task.event_key, status=status,
                         approved_count=approvals, merged_count=merges,
                         owned_merge_count=owned, other_merge_count=other_owner,
                         unknown_merge_count=unknown_owner, received=received,
                         elapsed=time.monotonic() - started, summary=summary,
                         error="；".join(errors + ([receipt_error] if receipt_error else [])))
        if stopped:
            self.notifier.send("代码审批已停止", summary, task.sender_name, task_id=task.event_key)
        else:
            self.notifier.send("代码审批处理完成" if status == "completed" else "代码审批处理失败" if status == "failed" else "代码审批部分完成", summary, task.sender_name, task_id=task.event_key)


class ApplicationRuntime(core.ListenerRuntime):
    def __init__(self, *args: Any, events: Events, settings: Callable[[], dict[str, Any]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.events = events
        self.settings = settings
        self.tasks = queue.Queue(maxsize=settings()["queue_capacity"])
        self.enqueued: dict[str, float] = {}
        self.connection = "starting"
        self.active_task_id = ""
        self.engine.progress = self.emit_state
        self.avatars = SenderAvatars(
            lambda contact: lookup_sender_avatar(self.dws, contact),
            lambda task_id, url: self.events.send("task_update", id=task_id, sender_avatar_url=url),
        )
        self.monitor_stop = threading.Event()
        self._mark_read_tasks: queue.Queue[core.MessageTask | None] = queue.Queue(
            maxsize=max(1, settings()["queue_capacity"])
        )
        self._mark_read_thread: threading.Thread | None = None

    def _ready(self) -> None:
        if self.stop_event.is_set():
            return
        self.connection = "listening"
        self.emit_state()

    def emit_state(self) -> None:
        status = self.connection
        if status == "listening" and self.screen_lock.reason():
            status = self.screen_lock.reason()
        self.events.send("state", status=status, connected=self.connection == "listening",
                         is_locked=self.screen_lock.locked, paused=self.screen_lock.paused.is_set(),
                         queue_count=self.tasks.qsize(), active_count=self.engine.active_mrs,
                         active_task_id=self.active_task_id)

    def set_paused(self, paused: bool) -> None:
        self.screen_lock.paused.set() if paused else self.screen_lock.paused.clear()
        self.emit_state()

    def _monitor(self) -> None:
        previous = None
        while not self.monitor_stop.is_set():
            self.screen_lock.refresh()
            state = (self.screen_lock.locked, self.screen_lock.reason(), self.tasks.qsize(), self.engine.active_mrs)
            if state != previous:
                self.emit_state()
                previous = state
            self.monitor_stop.wait(1)

    def _task_record(self, event: Mapping[str, Any], key: str, sender: str, refs=(), **values: Any) -> None:
        sender_id = str(event.get("sender_open_dingtalk_id", ""))
        self.events.send("task", task={
            "id": key, "sender": sender, "received_at": time.time(),
            "sender_avatar_url": self.avatars.cached_url(sender_id),
            "conversation_id": str(event.get("conversation_id", "")),
            "conversation_title": self.approval_group.title,
            "message_id": str(event.get("message_id", "")),
            "content": str(event.get("content", "")),
            "status": "queued", "summary": "等待处理", "error": "",
            "mrs": [{"key": r.key, "url": r.url, "title": "", "status": "waiting"} for r in refs],
            **values,
        })
        if values.get("status") != "discarded" and sender_id in self.allowed_senders:
            self.avatars.request(self.allowed_senders[sender_id], key)

    def _handle_event(self, event: Mapping[str, Any]) -> None:
        matched, _ = core.event_matches(event, self.allowed_senders, self.approval_group.open_conversation_id)
        if not matched:
            return
        try:
            key = core.event_key(event)
        except core.AutomationError as error:
            self.console.error(str(error))
            return
        if self.recent_messages.contains(key):
            self.console.debug("忽略当前进程内重复投递的消息")
            return
        sender = self.allowed_senders[str(event["sender_open_dingtalk_id"])].name
        self.screen_lock.refresh()
        reason = self.screen_lock.reason()
        if self.screen_lock.disposition() == "discard":
            self._task_record(event, key, sender, status="discarded",
                              summary="手动暂停期间已丢弃" if reason == "paused" else "锁屏期间已丢弃")
            return
        settings = self.settings()
        direct_refs = None
        try:
            direct_refs, _ = core.extract_mr_refs_from_message(
                str(event["content"]), None, self.gitlab_host, settings["max_mrs_per_message"])
        except core.AutomationError:
            pass
        try:
            detail = None
            # Only fetch the quote when the message itself cannot provide the
            # links. This keeps the serial event consumer off DWS for the
            # common case while still supporting quoted MR approvals.
            if settings["parse_quoted_messages"] and direct_refs is None:
                try:
                    detail = self.dws.message_by_id(str(event["message_id"]))
                except core.AutomationError as error:
                    self.console.warning(f"引用内容暂时不可用，继续检查正文：{error}")
            refs, used_quote = core.extract_mr_refs_from_message(
                str(event["content"]), detail, self.gitlab_host, settings["max_mrs_per_message"])
        except core.AutomationError as error:
            self._task_record(event, key, sender, status="invalid", summary="消息无法处理", error=str(error))
            return
        if not self._reserve(refs):
            self._task_record(event, key, sender, refs, status="discarded", summary="包含已在队列或正在处理的 MR")
            return
        if not self.recent_messages.add_if_new(key):
            self._release(refs)
            return
        task = core.MessageTask(key, str(event["message_id"]), str(event["conversation_id"]), sender, refs)
        self._task_record(event, key, sender, refs, quoted=used_quote,
                          summary="等待手动恢复" if reason == "paused" else "等待解锁" if reason == "locked" else "等待处理")
        self.enqueued[key] = time.monotonic()
        try:
            self.tasks.put_nowait(task)
        except queue.Full:
            if settings["queue_overflow_policy"] == "drop_oldest":
                try:
                    old = self.tasks.get_nowait()
                    if old is not None:
                        self._discard(old, "队列已满，按设置移除了最早的待处理消息")
                    self.tasks.task_done()
                except queue.Empty:
                    pass
                try:
                    self.tasks.put_nowait(task)
                except queue.Full:
                    self._discard(task, "队列已满，未加入队列")
            else:
                self._discard(task, "队列已满，按设置丢弃了新消息")
        self.emit_state()

    def _discard(self, task: core.MessageTask, reason: str) -> None:
        self._release(task.refs)
        self.enqueued.pop(task.event_key, None)
        self.recent_messages.forget(task.event_key)
        self.events.send("task_update", id=task.event_key, status="discarded", summary=reason)

    def clear_queue(self) -> int:
        count = 0
        while True:
            try:
                task = self.tasks.get_nowait()
            except queue.Empty:
                break
            if task is not None:
                self._discard(task, "已手动清空待处理队列")
                count += 1
            self.tasks.task_done()
        self.emit_state()
        return count

    def _start_mark_read_worker(self) -> None:
        if self._mark_read_thread is not None and self._mark_read_thread.is_alive():
            return
        self._mark_read_thread = threading.Thread(
            target=self._mark_read_worker,
            name="mark-read",
            daemon=True,
        )
        self._mark_read_thread.start()

    def _mark_read_worker(self) -> None:
        while True:
            task = self._mark_read_tasks.get()
            try:
                if task is None:
                    return
                if self.stop_event.is_set():
                    continue
                self._mark_read(task)
            except core.ProcessingStopped:
                continue
            except Exception as error:
                self.console.debug(f"后台标记已读异常：{error!r}")
            finally:
                self._mark_read_tasks.task_done()

    def _queue_mark_read(self, task: core.MessageTask) -> None:
        if self._mark_read_thread is None or not self._mark_read_thread.is_alive():
            self._mark_read(task)
            return
        try:
            self._mark_read_tasks.put_nowait(task)
        except queue.Full:
            self.console.debug("标记已读队列已满，跳过本次已读回执")

    def _worker(self) -> None:
        while True:
            if not self.stop_event.is_set() and self.screen_lock.reason():
                self.stop_event.wait(0.2)
                continue
            try:
                task = self.tasks.get(timeout=0.3)
            except queue.Empty:
                continue
            if task is None:
                self.tasks.task_done()
                return
            try:
                if self.stop_event.is_set():
                    self._discard(task, "监听已停止，未执行待处理消息")
                    continue
                if not self.screen_lock.wait_until_unlocked(self.stop_event, "处理审批消息"):
                    self._discard(task, "监听已停止")
                    continue
                settings = dict(self.settings())
                age = time.monotonic() - self.enqueued.get(task.event_key, time.monotonic())
                if settings["max_queue_age_seconds"] and age >= settings["max_queue_age_seconds"]:
                    self._discard(task, "等待时间超过设置上限，已跳过")
                    continue
                configure_core(settings)
                self.engine.settings = settings
                for name in ("commit_counts", "details_cache"):
                    cache = getattr(self.engine.glab, name, None)
                    if isinstance(cache, dict):
                        cache.clear()
                self.engine.leader_identity = self.engine.configured_leader if settings["use_fallback_for_own_mr"] else None
                self.active_task_id = task.event_key
                self.console.local.task_id = task.event_key
                self._worker_active.set()
                self.events.send("task_update", id=task.event_key, status="running", summary="正在处理")
                self.emit_state()
                if settings["mark_read"]:
                    self._queue_mark_read(task)
                self.notifier.send("收到代码审批请求", f"收到 {len(task.refs)} 个 MR，开始处理", task.sender_name, task_id=task.event_key)
                self.engine.process(task)
            except core.ProcessingStopped:
                self.events.send("task_update", id=task.event_key, status="stopped", summary="已停止后续操作")
            except Exception as error:
                self.console.error(f"任务处理失败：{error}")
                self.events.send("task_update", id=task.event_key, status="failed", summary="任务处理失败", error=str(error))
            finally:
                self._worker_active.clear()
                self.active_task_id = ""
                self._release(task.refs)
                self.enqueued.pop(task.event_key, None)
                self.tasks.task_done()
                self.emit_state()

    def _relay_dws_status(self, stream: Any, ready_event: threading.Event, status_lines: list[str]) -> None:
        for line in stream:
            status = line.strip()
            if not status:
                continue
            status_lines.append(status)
            del status_lines[:-core.MAX_DWS_STATUS_LINES]
            if status.startswith("[event] ready"):
                ready_event.set()
                self._ready_count += 1
                self._ready()
                self.console.success("钉钉真实订阅已就绪")
            elif "state=connected" in status:
                if ready_event.is_set():
                    self._ready()
            elif any(v in status.lower() for v in ("state=disconnected", "state=reconnecting", "[event] exited")) and not self.stop_event.is_set():
                self.connection = "reconnecting"
                self.emit_state()
            else:
                self.console.debug(status)

    def request_stop(self, signum: int | None = None) -> None:
        self.avatars.close()
        self.connection = "stopping"
        self.emit_state()
        super().request_stop(signum)

    def run(self) -> int:
        self.screen_lock.refresh()
        monitor = threading.Thread(target=self._monitor, name="screen-state", daemon=True)
        worker = threading.Thread(target=self._worker, name="approval-worker", daemon=False)
        self._start_mark_read_worker()
        monitor.start()
        worker.start()
        pre_ready_failures = 0
        reconnect_index = 0
        try:
            while not self.stop_event.is_set():
                self.connection = "starting" if self._ready_count == 0 and reconnect_index == 0 else "reconnecting"
                self.emit_state()
                ready = threading.Event()
                status_lines: list[str] = []
                try:
                    process = subprocess.Popen(self.dws.consume_command(), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, bufsize=1)
                except OSError as error:
                    raise core.AutomationError(f"无法启动钉钉监听：{error}") from error
                with self._process_lock:
                    self._dws_process = process
                assert process.stdout is not None and process.stderr is not None
                status_thread = threading.Thread(target=self._relay_dws_status,
                                                args=(process.stderr, ready, status_lines), daemon=True)
                incoming: queue.Queue[str] = queue.Queue(maxsize=1000)
                stdout_done = threading.Event()
                reader_stop = threading.Event()
                handler_stop = threading.Event()

                def read_events(stream=process.stdout, mailbox=incoming, done=stdout_done, cancel=reader_stop):
                    try:
                        for line in stream:
                            while not self.stop_event.is_set() and not cancel.is_set():
                                try:
                                    mailbox.put(line, timeout=0.2)
                                    break
                                except queue.Full:
                                    continue
                            if self.stop_event.is_set() or cancel.is_set():
                                break
                    finally:
                        done.set()

                def handle_events(mailbox=incoming, cancel=handler_stop):
                    # Keep DWS lookups and queue policy work off the thread
                    # that reads the long-lived subscription stream.
                    while not cancel.is_set() and not self.stop_event.is_set() and not ready.wait(0.1):
                        pass
                    while not cancel.is_set() and not self.stop_event.is_set():
                        try:
                            line = mailbox.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        try:
                            payload = json.loads(line)
                            if isinstance(payload, dict):
                                self._handle_event(payload)
                        except json.JSONDecodeError:
                            self.console.warning("忽略一条无法识别的钉钉事件")

                output_thread = threading.Thread(target=read_events, name="dws-events", daemon=True)
                status_thread.start()
                output_thread.start()
                handler_thread = threading.Thread(target=handle_events, name="approval-events", daemon=True)
                handler_thread.start()
                deadline = time.monotonic() + self.settings()["command_timeout_seconds"]
                try:
                    while not self.stop_event.is_set():
                        # Drain stdout only after the actual subscription ready marker.
                        if not ready.is_set():
                            if process.poll() is not None:
                                break
                            if time.monotonic() >= deadline:
                                status_lines.append('{"reason":"startup_timeout","retryable":true}')
                                self.console.warning("等待钉钉订阅就绪超时，正在结束本次连接")
                                break
                            self.stop_event.wait(0.05)
                            continue
                        if process.poll() is not None and stdout_done.is_set() and incoming.empty():
                            break
                        self.stop_event.wait(0.1)
                finally:
                    reader_stop.set()
                    handler_stop.set()
                    if process.poll() is None:
                        process.terminate()
                    try:
                        returncode = process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.connection = "stopping"
                        self.console.warning(f"钉钉连接组件（PID {process.pid}）仍在清理订阅，等待正常退出")
                        self.emit_state()
                        returncode = process.wait()
                    output_thread.join(timeout=2)
                    handler_thread.join(timeout=2)
                    status_thread.join(timeout=2)
                    process.stdout.close()
                    process.stderr.close()
                    with self._process_lock:
                        self._dws_process = None
                if self.stop_event.is_set():
                    break
                if not ready.is_set():
                    while not incoming.empty():
                        line = incoming.get_nowait()
                        try:
                            payload = json.loads(line)
                            if isinstance(payload, dict) and ("error" in payload or payload.get("success") is False):
                                status_lines.append(line)
                        except json.JSONDecodeError:
                            pass
                decision = startup_decision(status_lines, returncode)
                if ready.is_set():
                    pre_ready_failures = 0
                    reconnect_index = 0
                else:
                    pre_ready_failures += 1
                if decision.terminal or (not ready.is_set() and pre_ready_failures > decision.additional_attempts):
                    raise core.AutomationError(f"{decision.reason}；已停止自动重试")
                delays = self.settings()["reconnect_delays"]
                delay = max(delays[min(reconnect_index, len(delays) - 1)], decision.wait_seconds)
                reconnect_index += 1
                self.connection = "reconnecting"
                self.console.warning(f"{decision.reason}；{delay:g} 秒后重新连接")
                self.emit_state()
                self.stop_event.wait(delay)
        finally:
            self.avatars.close()
            self.stop_event.set()
            self.clear_queue()
            self.tasks.put(None)
            worker.join()
            try:
                self._mark_read_tasks.put_nowait(None)
            except queue.Full:
                pass
            if self._mark_read_thread is not None:
                self._mark_read_thread.join(timeout=2)
            self.monitor_stop.set()
            monitor.join(timeout=6)
        return 0
