from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Mapping

import listener_core as core
from avatars import SenderAvatars, avatar_url, lookup_sender_avatar
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
        super().__init__(
            *args,
            max_concurrent_mrs=settings["max_concurrent_mrs"],
            merge_verify_timeout_seconds=settings["merge_timeout_seconds"],
            merge_verify_interval_seconds=settings["merge_poll_seconds"],
            retry_attempts=settings["max_attempts"],
            retry_delays=settings["retry_delays"],
            **kwargs,
        )
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
        # Keep worker threads alive across messages so their thread-local
        # HTTPS connections can be reused instead of paying a TLS handshake
        # for every MR.
        self._approval_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="approval-mr")
        self._approval_cv = threading.Condition()
        self._approval_active = 0
        self._approval_prefetch_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="approval-prefetch",
        )
        self._approval_prefetch_lock = threading.Lock()
        self._approval_prefetch: dict[str, Future[core.ApprovalInfo]] = {}
        self._metadata_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mr-metadata")
        # Commit count is display-only and may paginate. Keep it away from the
        # approver refresh so the attribution update cannot be delayed by it.
        self._commit_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mr-commit-count")
        self._metadata_lock = threading.Lock()
        self._metadata_pending: set[tuple[str, str]] = set()
        self._approval_refresh_pending: set[tuple[str, str]] = set()
        self._closed = False

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Apply the latest task snapshot without mutating listener globals."""
        self.settings = settings
        self.max_concurrent_mrs = settings["max_concurrent_mrs"]
        self.merge_verify_timeout_seconds = settings["merge_timeout_seconds"]
        self.merge_verify_interval_seconds = settings["merge_poll_seconds"]
        self.retry_attempts = settings["max_attempts"]
        self.retry_delays = tuple(settings["retry_delays"])
        for client in (getattr(self, "glab", None), getattr(self, "dws", None)):
            if client is not None:
                client.command_timeout = settings["command_timeout_seconds"]
                client.retry_attempts = settings["max_attempts"]
                client.retry_delays = tuple(settings["retry_delays"])

    def close(self) -> None:
        with self._metadata_lock:
            if self._closed:
                return
            self._closed = True
        with self._approval_cv:
            self._approval_cv.notify_all()
        self._approval_pool.shutdown(wait=False, cancel_futures=True)
        self._approval_prefetch_pool.shutdown(wait=False, cancel_futures=True)
        self._metadata_pool.shutdown(wait=False, cancel_futures=True)
        self._commit_pool.shutdown(wait=False, cancel_futures=True)

    def _run_limited(self, ref: core.MRRef, index: int, total: int):
        """Run one MR while allowing live concurrency changes to take effect."""
        with self._approval_cv:
            while (
                not self._closed
                and not self.stop_event.is_set()
                and self._approval_active >= max(1, self.max_concurrent_mrs)
            ):
                self._approval_cv.wait(timeout=0.5)
            if self._closed or self.stop_event.is_set():
                raise core.ProcessingStopped("监听正在停止，未继续执行 MR")
            self._approval_active += 1
        try:
            return self._process_one(ref, index, total)
        finally:
            with self._approval_cv:
                self._approval_active = max(0, self._approval_active - 1)
                self._approval_cv.notify()

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
        self._publish_details(plan.details, actor=plan.actor.name)
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

    def _details_loaded(self, details: core.MRDetails) -> None:
        # Publish the merge request as soon as its metadata is available. The
        # approval lookup may still be in flight, but the user can already see
        # the title, author, SHA and (for an existing merge) the actual merger.
        self._publish_details(details)

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

    def _preload_approvals(self, ref: core.MRRef) -> None:
        if not getattr(self.glab, "supports_parallel_approval_prefetch", False):
            return
        with self._approval_prefetch_lock:
            if self._closed or ref.key in self._approval_prefetch:
                return
        try:
            future = self._approval_prefetch_pool.submit(self.glab.approvals, ref, False)
        except RuntimeError:
            return
        with self._approval_prefetch_lock:
            if self._closed:
                future.cancel()
                return
            self._approval_prefetch[ref.key] = future

    def _load_preflight_approvals(self, ref: core.MRRef, use_leader: bool) -> core.ApprovalInfo:
        with self._approval_prefetch_lock:
            future = self._approval_prefetch.pop(ref.key, None)
        if future is not None:
            if use_leader:
                future.cancel()
            else:
                try:
                    return future.result()
                except core.ProcessingStopped:
                    raise
                except Exception as error:
                    self.console.debug(f"{ref.key} 并行审批状态读取失败，改为重新读取：{error!r}")
        return self.glab.approvals(ref, use_leader)

    def _discard_preloaded_approvals(self, ref: core.MRRef) -> None:
        with self._approval_prefetch_lock:
            future = self._approval_prefetch.pop(ref.key, None)
        if future is not None:
            future.cancel()

    def _on_merge_confirmed(self, details: core.MRDetails) -> None:
        super()._on_merge_confirmed(details)
        self._mark_merged(details.ref)
        self._publish_details(details)

    def _approve_and_verify(self, plan: core.MRPlan) -> core.ApprovalInfo:
        self._set_stage(plan.ref, "approval")
        self._mr(plan.ref, status="approving", stage="approval")
        result = super()._approve_and_verify(plan)
        if result.actor_wrote:
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
        pending_key = (task_id, ref.key)

        def load() -> None:
            self.console.local.task_id = task_id
            self.console.local.mr_key = ref.key
            try:
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
            finally:
                with self._metadata_lock:
                    self._metadata_pending.discard(pending_key)

        with self._metadata_lock:
            if pending_key in self._metadata_pending or self._closed:
                return
            self._metadata_pending.add(pending_key)
        try:
            self._commit_pool.submit(load)
        except RuntimeError:
            with self._metadata_lock:
                self._metadata_pending.discard(pending_key)

    def _finalize_approvals(
        self,
        ref: core.MRRef,
        plan: core.MRPlan,
        approvals: core.ApprovalInfo | None,
        approval_count: int,
        prefix: str,
    ) -> core.ApprovalInfo | None:
        # The merge result is already authoritative. Do not hold the visible
        # completion state behind one more GitLab read; refresh approvers in
        # the background and publish the delta when it arrives.
        self._schedule_approval_refresh(
            ref,
            plan,
            approvals,
            approval_count,
            prefix,
        )
        return approvals

    def _schedule_approval_refresh(
        self,
        ref: core.MRRef,
        plan: core.MRPlan,
        previous_approvals: core.ApprovalInfo | None,
        approval_count: int,
        prefix: str,
    ) -> None:
        task_id = self.task_id
        refresh_key = (task_id, ref.key)
        with self._metadata_lock:
            if self._closed or refresh_key in self._approval_refresh_pending:
                return
            self._approval_refresh_pending.add(refresh_key)

        def load() -> None:
            self.console.local.task_id = task_id
            self.console.local.mr_key = ref.key
            try:
                fresh = self._load_preflight_approvals(ref, plan.use_leader)
                if previous_approvals is not None:
                    for user_id, name in previous_approvals.approved_by.items():
                        fresh.approved_by.setdefault(user_id, name)
                if approval_count or fresh.user_has_approved:
                    fresh.approved_by.setdefault(plan.actor.user_id, plan.actor.name)
                self.events.send(
                    "mr",
                    task_id=task_id,
                    key=ref.key,
                    approvers=list(self._approver_names(fresh)),
                    approvals_known=True,
                    approved_by_own=self._approved_by_own(fresh),
                )
            except core.ProcessingStopped:
                pass
            except core.AutomationError as error:
                self.console.warning(
                    f"{prefix} 合并已完成，但读取最终审批人失败，"
                    f"保留处理中的审批记录：{error}"
                )
                self.console.debug(error.detail)
            except Exception as error:
                self.console.debug(f"{prefix} 后台刷新审批人异常：{error!r}")
            finally:
                with self._metadata_lock:
                    self._approval_refresh_pending.discard(refresh_key)

        try:
            self._metadata_pool.submit(load)
        except RuntimeError:
            with self._metadata_lock:
                self._approval_refresh_pending.discard(refresh_key)

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
            result.stage = self._stage_for(ref)
            # Commit count is display-only metadata. Fill it in after all
            # approval/merge writes, without delaying the completed state.
            if result.merged_count:
                self._ensure_commit_count(ref)
            self._mr(ref, status="failed" if result.error else "completed",
                     stage=self._stage_for(ref),
                     error=result.error or "", elapsed=time.monotonic() - started,
                     owned_merge=result.owned_merge,
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
                stage=stage,
            )
        finally:
            self._discard_preloaded_approvals(ref)
            with self._active_lock:
                self.active_mrs -= 1
            self.progress()
            self._clear_state(ref)

    def _publish_progress(
        self,
        task: core.MessageTask,
        results: Mapping[int, core.MRResult],
        total: int,
        started: float,
    ) -> None:
        """Show completed MRs immediately while the rest of the message runs."""
        if self.stop_event.is_set() or len(results) >= total:
            return
        rows = [
            self._mr_result_summary(task.refs[index - 1], results[index])
            for index in sorted(results)
        ]
        approvals = sum(result.approved_count for result in results.values())
        merges = sum(result.merged_count for result in results.values())
        owned = sum(
            result.owned_merge
            for result in results.values()
            if result.merged_count
        )
        unknown_owner = sum(
            not result.owned_merge and not result.merge_owner_known
            for result in results.values()
            if result.merged_count
        )
        other_owner = max(0, merges - owned - unknown_owner)
        summary = (
            f"{total} 个 MR：已完成 {len(results)} 个，"
            f"正在处理 {total - len(results)} 个"
        )
        if merges:
            summary += f"（本机合并 {owned} 个，其他成员合并 {other_owner} 个"
            if unknown_owner:
                summary += f"，合并人未知 {unknown_owner} 个"
            summary += "）"
        self.events.send(
            "task_update",
            id=task.event_key,
            status="running",
            approved_count=approvals,
            merged_count=merges,
            owned_merge_count=owned,
            other_merge_count=other_owner,
            unknown_merge_count=unknown_owner,
            received=False,
            elapsed=time.monotonic() - started,
            summary=summary,
            mr_summaries=rows,
            error="",
        )

    def process(self, task: core.MessageTask) -> None:
        self.apply_settings(self.settings)
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
            futures = {
                self._approval_pool.submit(self._run_limited, ref, i, total): i
                for i, ref in enumerate(task.refs, 1)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except core.ProcessingStopped:
                    stopped = True
                    stopped_count += 1
                    results[index] = core.MRResult()
                    self._mr(
                        task.refs[index - 1],
                        status="stopped",
                        stage="",
                        error="",
                        summary="监听已停止，未继续处理",
                    )
                except Exception as error:
                    message = f"任务处理异常：{error}"
                    self.console.error(message)
                    self.console.debug(repr(error))
                    results[index] = core.MRResult(error=message)
                self._publish_progress(task, results, total, started)
        approvals = sum(r.approved_count for r in results.values())
        merges = sum(r.merged_count for r in results.values())
        errors = [r.error for r in results.values() if r.error]
        mr_summaries = [
            self._mr_result_summary(task.refs[index - 1], results[index])
            for index in range(1, total + 1)
            if index in results
        ]
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
        status = "stopped" if stopped else "completed"
        if not stopped and errors:
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

        def emit_task_update() -> None:
            self.events.send(
                "task_update",
                id=task.event_key,
                status=status,
                approved_count=approvals,
                merged_count=merges,
                owned_merge_count=owned,
                other_merge_count=other_owner,
                unknown_merge_count=unknown_owner,
                received=received,
                elapsed=time.monotonic() - started,
                summary=summary,
                mr_summaries=mr_summaries,
                error="；".join(errors + ([receipt_error] if receipt_error else [])),
            )

        # Show the MR result as soon as GitLab is done. The DWS "收到"
        # reaction is an independent best-effort write and must not make the
        # approval result look slower than it is.
        emit_task_update()
        if should_receive:
            try:
                self._wait_until_unlocked("添加收到表态")
                self.dws.add_received(task.conversation_id, task.message_id)
                received = True
                self.console.success("已给原消息添加收到表态")
            except core.ProcessingStopped:
                # GitLab work is already complete. Stopping during the
                # best-effort DingTalk receipt must not turn the task back
                # into an unfinished processing job.
                receipt_error = "监听已停止，未添加收到"
            except core.AutomationError as error:
                receipt_error = str(error)
                self.console.error(f"MR 处理已结束，添加收到失败：{error}")
                if status == "completed":
                    status = "partial" if approvals or merges or owned else "failed"
            if received:
                summary += "，已添加收到"
            elif receipt_error:
                summary += f"，添加收到失败：{receipt_error}"
            emit_task_update()

        notification_summary = summary
        if mr_summaries:
            notification_summary += "；" + "；".join(mr_summaries)
        if stopped:
            self.notifier.send("代码审批已停止", notification_summary, task.sender_name, task_id=task.event_key)
        else:
            self.notifier.send(
                "代码审批处理完成" if status == "completed"
                else "代码审批处理失败" if status == "failed"
                else "代码审批部分完成",
                notification_summary,
                task.sender_name,
                task_id=task.event_key,
            )

    @staticmethod
    def _mr_result_summary(ref: core.MRRef, result: core.MRResult) -> str:
        parts: list[str] = []
        if result.approvals_known:
            names = "、".join(result.approvers) or "未返回"
            ownership = "本机参与" if result.approved_by_own else "其他成员"
            parts.append(f"审批人 {names}（{ownership}）")
        else:
            parts.append("审批人未获取")

        if result.merged_count:
            if result.merged_by:
                ownership = (
                    "本机"
                    if result.owned_merge
                    else "其他成员"
                    if result.merge_owner_known
                    else "归属未知"
                )
                parts.append(f"合并人 {result.merged_by}（{ownership}）")
            else:
                parts.append("合并人未返回")
        elif result.error:
            stage = {
                "preflight": "检查",
                "approval": "审批",
                "merge": "合并",
            }.get(result.stage, "处理")
            parts.append(f"{stage}失败")
        else:
            parts.append("未完成")
        return f"{ref.key}：" + "；".join(parts)


class ApplicationRuntime(core.ListenerRuntime):
    def __init__(self, *args: Any, events: Events, settings: Callable[[], dict[str, Any]], **kwargs: Any):
        snapshot = settings()
        super().__init__(
            *args,
            queue_size=snapshot["queue_capacity"],
            max_urls=snapshot["max_mrs_per_message"],
            reconnect_delays=snapshot["reconnect_delays"],
            **kwargs,
        )
        self.events = events
        self.settings = settings
        self.enqueued: dict[str, float] = {}
        self.connection = "starting"
        self.active_task_id = ""
        self._accept_lock = threading.Lock()
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

    def apply_settings(self, settings: dict[str, Any] | None = None) -> None:
        current = dict(self.settings() if settings is None else settings)
        with self._accept_lock:
            with self.tasks.mutex:
                self.tasks.maxsize = current["queue_capacity"]
            # The mark-read worker uses a separate bounded queue. Keep its
            # capacity aligned with the approval queue when settings change.
            with self._mark_read_tasks.mutex:
                self._mark_read_tasks.maxsize = max(1, current["queue_capacity"])
            self._trim_queue_to_capacity()
        self.max_urls = current["max_mrs_per_message"]
        self.reconnect_delays = tuple(current["reconnect_delays"])
        if hasattr(self.engine, "apply_settings"):
            self.engine.apply_settings(current)
        else:
            self.engine.settings = current

    def _trim_queue_to_capacity(self) -> None:
        """Discard waiting messages if a live setting reduced queue capacity."""
        while True:
            with self.tasks.mutex:
                if len(self.tasks.queue) <= self.tasks.maxsize:
                    return
            try:
                task = self.tasks.get_nowait()
            except queue.Empty:
                return
            if task is not None:
                self._discard(task, "队列容量已调整，移除超出上限的等待消息")
            self.tasks.task_done()

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
        event_avatar = avatar_url(core.first_avatar_value(event))
        if event_avatar and sender_id:
            self.avatars.remember(sender_id, event_avatar)
        self.events.send("task", task={
            "id": key, "sender": sender, "received_at": time.time(),
            "sender_avatar_url": event_avatar or self.avatars.cached_url(sender_id),
            "conversation_id": str(event.get("conversation_id", "")),
            "conversation_title": self.approval_group.title,
            "message_id": str(event.get("message_id", "")),
            "content": str(event.get("content", "")),
            "status": "queued", "summary": "等待处理", "error": "",
            "mrs": [{"key": r.key, "url": r.url, "title": "", "status": "waiting"} for r in refs],
            **values,
        })
        if not event_avatar and values.get("status") != "discarded" and sender_id in self.allowed_senders:
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
            # Prefer an embedded quote over a remote lookup. A remote lookup
            # is only needed when the message has no links and DWS did not
            # include the quoted message in the flattened event.
            embedded_quote = core.quoted_message(event)
            if settings["parse_quoted_messages"] and (
                direct_refs is None or embedded_quote is not None
            ):
                if embedded_quote is not None:
                    detail = event
                elif direct_refs is None:
                    try:
                        detail = self.dws.message_by_id(str(event["message_id"]))
                    except core.AutomationError as error:
                        self.console.warning(f"引用内容暂时不可用，继续检查正文：{error}")
            if detail is not None:
                refs, used_quote = core.extract_mr_refs_from_message(
                    str(event["content"]), detail, self.gitlab_host,
                    settings["max_mrs_per_message"])
            elif direct_refs is not None:
                refs, used_quote = direct_refs, False
            else:
                refs, used_quote = core.extract_mr_refs_from_message(
                    str(event["content"]), None, self.gitlab_host,
                    settings["max_mrs_per_message"])
        except core.AutomationError as error:
            self._task_record(event, key, sender, status="invalid", summary="消息无法处理", error=str(error))
            return
        task = core.MessageTask(key, str(event["message_id"]), str(event["conversation_id"]), sender, refs)
        with self._accept_lock:
            if self.recent_messages.contains(key):
                self.console.debug("忽略当前进程内重复投递的消息")
                return
            if not self._reserve(refs):
                self._task_record(event, key, sender, refs, status="discarded",
                                  summary="包含已在队列或正在处理的 MR")
                return
            # A message is only considered accepted after it is actually in
            # the queue. Pause/lock discards and failed queue writes must stay
            # retryable by the source instead of poisoning the dedup cache.
            self._task_record(
                event,
                key,
                sender,
                refs,
                quoted=used_quote,
                summary="等待手动恢复" if reason == "paused" else "等待解锁" if reason == "locked" else "等待处理",
            )
            while True:
                try:
                    self.tasks.put_nowait(task)
                    break
                except queue.Full:
                    if settings["queue_overflow_policy"] != "drop_oldest":
                        self._discard(task, "队列已满，按设置丢弃了新消息")
                        self.emit_state()
                        return
                    try:
                        old = self.tasks.get_nowait()
                    except queue.Empty:
                        self._discard(task, "队列已满，未加入队列")
                        self.emit_state()
                        return
                    if old is not None:
                        self._discard(old, "队列已满，按设置移除了最早的待处理消息")
                    self.tasks.task_done()
            self.enqueued[key] = time.monotonic()
            self.recent_messages.add_if_new(key)
        self.emit_state()

    def _discard(self, task: core.MessageTask, reason: str) -> None:
        self._release(task.refs)
        self.enqueued.pop(task.event_key, None)
        self.recent_messages.forget(task.event_key)
        self.events.send("task_update", id=task.event_key, status="discarded", summary=reason)

    def clear_queue(self) -> int:
        with self._accept_lock:
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
                # The queue is woken by a message or by the explicit shutdown
                # sentinel, so idle polling only adds latency to every click.
                task = self.tasks.get()
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
                self.apply_settings(settings)
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
            status = core.ANSI_ESCAPE_RE.sub("", line).strip()
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
        self._recover_stale_bus_before_listen()
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
                        except Exception as error:
                            # A malformed or unexpected event must not kill the
                            # only consumer while the DWS subscription remains
                            # healthy and the UI still reports "listening".
                            self.console.error(f"处理钉钉事件异常：{error}")
                            self.console.debug(repr(error))

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
                        self.console.warning(
                            f"钉钉连接组件（PID {process.pid}）仍在清理订阅，强制结束"
                        )
                        try:
                            process.kill()
                        except OSError:
                            pass
                        try:
                            returncode = process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.console.error("钉钉连接组件未能退出，已放弃等待")
                            returncode = -1
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
            worker.join(timeout=10)
            if worker.is_alive():
                self.console.error("审批工作线程未能及时结束，可能仍有外部命令未响应")
            if hasattr(self.engine, "close"):
                self.engine.close()
            try:
                self._mark_read_tasks.put_nowait(None)
            except queue.Full:
                pass
            if self._mark_read_thread is not None:
                self._mark_read_thread.join(timeout=2)
            self.monitor_stop.set()
            monitor.join(timeout=6)
            self._cleanup_idle_bus_after_listen()
        return 0

    def _cleanup_idle_bus_after_listen(self) -> None:
        """Stop the local bus once its last consumer has exited."""
        cleanup = getattr(self.dws, "recover_stale_event_bus", None)
        if not callable(cleanup):
            return
        try:
            recovered, message = cleanup(timeout=5)
        except Exception as error:
            self.console.debug(f"停止后清理 DWS 事件总线失败：{error!r}")
            return
        if recovered:
            self.console.success(message)
        else:
            self.console.debug(message)
