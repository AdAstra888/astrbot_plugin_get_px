from __future__ import annotations

import asyncio

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .caption import build_caption
from .constants import AIOCQHTTP_PLATFORM, MAX_IMAGE_COUNT
from .downloader import cleanup
from .orientation import (
    ORIENTATION_ANY,
    filter_by_orientation,
    lolicon_aspect_ratio,
    normalize_orientation,
    orientation_label,
)
from .safety import ContentSafetyPolicy, STRICT_CONTENT_SAFETY_POLICY


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0

# 单次最多补拉几轮。Lolicon 每次返回的都是随机抽样，重复请求能拿到新结果；
# Pixiv 回退则靠 offset 翻页。三轮足够覆盖常见的去重/筛选损耗，
# 再多只会放大延迟与上游压力。
MAX_REFILL_ROUNDS = 3

# 首次取候选时按「请求张数 × 该倍数」超采，给去重、内容安全与方向筛选
# 留出损耗余量；上限由 Lolicon 的 num 参数（20）封顶。
CANDIDATE_OVERFETCH_FACTOR = 4


def _search_quality_label(value: object) -> str:
    return {
        "original": "原图",
        "large": "大图",
        "medium": "中图",
        "square_medium": "方形缩略图",
    }.get(str(value or ""), str(value or ""))


def _search_source_label(value: object) -> str:
    source = str(value or "")
    if source.startswith("lolicon"):
        return "Lolicon"
    if source.startswith("pixiv"):
        return "Pixiv"
    return source


class SearchMixin:
    """Search and source-fallback flows."""

    @staticmethod
    def _should_use_forward(
        *, downloaded_count: int, threshold: int, platform_name: str
    ) -> bool:
        return platform_name == AIOCQHTTP_PLATFORM and downloaded_count > threshold

    def _p_unit_cost(self) -> int:
        return self._cfg_int("p_coin_cost", 20, 0, 200)

    def _p_charging_active(self) -> bool:
        return (
            self._p_unit_cost() > 0
            and self._cfg_bool("checkin_enabled", True)
            and getattr(self, "checkin_store", None) is not None
        )

    def _caption_mode(self) -> str:
        return self._cfg_str("caption_mode", "full")

    def _default_orientation(self) -> str:
        return normalize_orientation(self._cfg_str("image_orientation", "any"))

    async def _p_balance_error(self, user_id: str, count: int) -> str:
        """返回余额不足提示；余额充足或钱包不可用时返回空串。"""
        cost = self._p_unit_cost()
        if not user_id or cost <= 0 or count <= 0:
            return ""
        try:
            profile = await self.checkin_store.get_profile(user_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} /p 金币预检失败，本次免检放行: "
                f"error_type={type(exc).__name__}"
            )
            return ""
        total = cost * count
        if profile.coins < total:
            return f"金币不足，需要 {total}，当前只有 {profile.coins}。"
        return ""

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            return True
        if getattr(self, "client", None) and self.client.api:
            return True
        self._init_client()
        return bool(
            (
                getattr(self, "lolicon_client", None)
                and self.lolicon_client.available
            )
            or getattr(self, "client", None)
        )

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id() or ''}"

    @staticmethod
    def _source_key(tag: str, source: str) -> str:
        prefix = "search" if tag.strip() else "random"
        return f"{source}:{prefix}:{tag.strip().casefold()}" if tag.strip() else f"{source}:random"

    async def _fetch_source_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int = 20,
        offset: int = 0,
        aspect_ratio: str = "",
        use_page_cursor: bool = True,
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> tuple[list[dict], int, str]:
        """优先请求 Lolicon，失败后按有无标签回退 Pixiv。"""
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            try:
                if tag:
                    illusts = await lolicon_client.search(
                        tag,
                        count=count,
                        aspect_ratio=aspect_ratio,
                        r18=0 if policy.general_only_enabled else 2,
                    )
                    source_key = self._source_key(tag, "lolicon")
                else:
                    illusts = await lolicon_client.random(
                        count=count,
                        aspect_ratio=aspect_ratio,
                        r18=0 if policy.general_only_enabled else 2,
                    )
                    source_key = "lolicon:random"
                if illusts:
                    return illusts, len(illusts), source_key
            except Exception as exc:
                logger.info(
                    f"{LOG_PREFIX} Lolicon 请求失败，尝试 Pixiv 回退: "
                    f"tag_configured={'yes' if tag else 'no'} "
                    f"error_type={type(exc).__name__}"
                )

        pixiv_source_key = self._source_key(tag, "pixiv") if tag else "pixiv:recommended"
        page_offset = offset
        if use_page_cursor and page_offset == 0 and self.image_index is not None:
            try:
                page_offset = await self.image_index.get_page_offset(
                    self._event_scope(event), pixiv_source_key
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 读取 Pixiv 回退分页游标失败: "
                    f"error_type={type(exc).__name__}"
                )

        if self.client is None:
            self._init_client()
        if self.client is None:
            return [], 0, pixiv_source_key
        try:
            if tag:
                illusts = await self.client.search(tag, offset=page_offset)
                source_key = pixiv_source_key
            else:
                illusts = await self.client.recommended(offset=page_offset)
                source_key = pixiv_source_key
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} Pixiv 回退请求失败: "
                f"tag_configured={'yes' if tag else 'no'} "
                f"error_type={type(exc).__name__}"
            )
            return [], 0, pixiv_source_key
        return illusts, len(illusts), source_key

    async def _fetch_illust_by_id(
        self, event: AstrMessageEvent, illust_id: str
    ) -> dict | None:
        """按作品 ID 取详情，用于「点名要这张图」的发送路径。"""
        raw_id = str(illust_id or "").strip()
        if not raw_id.isdigit():
            return None
        if getattr(self, "client", None) is None:
            self._init_client()
        client = getattr(self, "client", None)
        if client is None:
            logger.warning(
                f"{LOG_PREFIX} 按 ID 发图失败，未配置 Pixiv 客户端: "
                f"illust_id={raw_id}"
            )
            return None
        try:
            illust = await client.illust_detail(int(raw_id))
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 按 ID 获取作品详情失败: "
                f"illust_id={raw_id} error_type={type(exc).__name__}"
            )
            return None
        if not illust:
            return None
        normalized = dict(illust)
        normalized.setdefault("id", raw_id)
        normalized["_source"] = "pixiv"
        return normalized

    async def _collect_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        wanted: int,
        orientation: str,
        policy: ContentSafetyPolicy,
        filter_manga: bool,
    ) -> tuple[list[dict], str, int]:
        """超采 + 补拉，直到候选足够或轮数用尽。

        返回 ``(候选列表, source_key, 累计原始条数)``。候选已去重、
        已过滤漫画/黑名单/内容安全/方向，可直接交给 ``_pick_illusts``。
        """
        aspect_ratio = lolicon_aspect_ratio(orientation)
        # 首次按 wanted 的倍数超采，但不超过 Lolicon 单次上限。
        batch_size = min(max(wanted * CANDIDATE_OVERFETCH_FACTOR, wanted), 20)

        collected: list[dict] = []
        seen_ids: set[str] = set()
        source_key = ""
        total_raw = 0

        for round_index in range(MAX_REFILL_ROUNDS):
            batch, raw_count, batch_source_key = await self._fetch_source_candidates(
                event,
                tag,
                count=batch_size,
                offset=total_raw,
                aspect_ratio=aspect_ratio,
                use_page_cursor=round_index == 0,
                policy=policy,
            )
            if not source_key:
                source_key = batch_source_key
            total_raw += raw_count
            if not batch:
                break

            if filter_manga:
                batch = self._filter_manga(batch)
            # 内容安全服务不可用时这里会抛 RuntimeError，由调用方转成用户提示。
            batch = await self._filter_blacklisted_illusts(batch, policy)
            batch = filter_by_orientation(batch, orientation)

            added = 0
            for illust in batch:
                illust_id = str(illust.get("id") or "")
                if not illust_id or illust_id in seen_ids:
                    continue
                seen_ids.add(illust_id)
                collected.append(illust)
                added += 1

            logger.info(
                f"{LOG_PREFIX} 候选补拉: round={round_index + 1}/"
                f"{MAX_REFILL_ROUNDS} source={_search_source_label(batch_source_key)} "
                f"raw_count={raw_count} added={added} "
                f"collected={len(collected)} wanted={wanted}"
            )
            if len(collected) >= wanted:
                break

        return collected, source_key, total_raw

    async def _record_image_usage(
        self,
        event: AstrMessageEvent,
        source_key: str,
        illust: dict,
        *,
        feature: str,
        user_id: str = "",
    ) -> None:
        if self.image_index is None or not source_key:
            return
        illust_id = str(illust.get("id") or "")
        if not illust_id:
            return
        try:
            await self.image_index.record_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature=feature,
                user_id=user_id,
            )
        except Exception as e:
            # 去重索引写入失败会让同一作品被重复发送，必须留下可追踪的日志。
            logger.warning(
                f"{LOG_PREFIX} 记录图片使用失败，去重索引可能失效: "
                f"illust_id={illust_id} feature={feature} "
                f"error_type={type(e).__name__} error={e}"
            )

    def _remember_last_sent(self, event: AstrMessageEvent, illust: dict) -> None:
        """记住用户最近收到的一张图，供「/p 收藏」无参调用使用。"""
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return
        cache = getattr(self, "_last_sent_illust", None)
        if cache is None:
            cache = {}
            self._last_sent_illust = cache
        if len(cache) > 1024:
            cache.clear()
        cache[user_id] = illust

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
        *,
        orientation: str = "",
        illust_id: str = "",
    ):
        """搜索并发送图片；支持按作品 ID 直发与方向筛选。"""
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.debug(
                f"{LOG_PREFIX} 搜索请求触发频率限制: retry_after_seconds={wait}"
            )
            yield event.plain_result(f"⏳ 请求太频繁，请 {wait} 秒后再试")
            return

        by_id_mode = bool(str(illust_id or "").strip())

        # 参数解析
        max_count = self._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT)
        if by_id_mode:
            # 点名要的作品只发这一张，不受 max_count 影响。
            count = 1
        else:
            try:
                count = max(1, min(int(count_str), max_count)) if count_str else 1
            except (TypeError, ValueError):
                count = 1

        resolved_orientation = normalize_orientation(
            orientation or self._default_orientation()
        )

        # 金币预检：请求张数 × 单价，余额不足直接提醒并终止。
        if self._p_charging_active():
            sender_id = str(event.get_sender_id() or "")
            balance_error = await self._p_balance_error(sender_id, count)
            if balance_error:
                yield event.plain_result(balance_error)
                return

        policy = await self._content_safety_policy(event)
        try:
            if tag and await self._blocked_query_term(tag, policy):
                yield event.plain_result("🚫 搜索词不符合内容安全要求")
                return
        except RuntimeError:
            yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
            return
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "original")
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            25.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)
        filter_manga = self._cfg_bool("filter_manga", True)

        if by_id_mode:
            detail = await self._fetch_illust_by_id(event, illust_id)
            if detail is None:
                yield event.plain_result(
                    f"❌ 没有找到作品 {illust_id}，请确认 ID 是否正确"
                    "（按 ID 发图需要配置 pixiv_refresh_token）"
                )
                return
            illusts = [detail]
            source_key = "pixiv:detail"
            raw_count = 1
            logger.info(
                f"{LOG_PREFIX} 按作品 ID 发图: illust_id={illust_id} "
                f"orientation={orientation_label(resolved_orientation)}"
            )
        else:
            try:
                illusts, source_key, raw_count = await self._collect_candidates(
                    event,
                    tag,
                    wanted=count,
                    orientation=resolved_orientation,
                    policy=policy,
                    filter_manga=filter_manga,
                )
            except RuntimeError:
                yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
                return
            logger.info(
                f"{LOG_PREFIX} 搜索候选获取完成: "
                f"tag_configured={'yes' if tag else 'no'} "
                f"source={_search_source_label(source_key)} "
                f"requested_count={count} quality={_search_quality_label(quality)} "
                f"orientation={orientation_label(resolved_orientation)} "
                f"candidate_count={len(illusts)} raw_count={raw_count}"
            )

        if not illusts:
            if resolved_orientation != ORIENTATION_ANY:
                yield event.plain_result(
                    f"❌ 图片源请求失败，或没有符合「{orientation_label(resolved_orientation)}」"
                    "的作品，换个标签或去掉方向筛选试试"
                )
            else:
                yield event.plain_result("❌ 图片源请求失败或无结果，换个标签试试")
            return

        if by_id_mode:
            # 点名要的作品仍要过内容安全与黑名单，但不参与去重池。
            try:
                illusts = await self._filter_blacklisted_illusts(illusts, policy)
            except RuntimeError:
                yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
                return
            if not illusts:
                yield event.plain_result(
                    f"🚫 作品 {illust_id} 未通过内容安全策略，无法发送"
                )
                return
            pick_count = 1
            dedupe_days = 0
        else:
            # _collect_candidates 已经过滤过漫画、黑名单、内容安全与方向，
            # 这里只需按实际候选数收敛张数。
            pick_count = min(count, len(illusts))
            dedupe_days = self._cfg_int("dedupe_days", 1, 0, 7)
            if (
                self.image_index is not None
                and self.image_index.retention_days != dedupe_days
            ):
                try:
                    await self.image_index.set_retention_days(dedupe_days)
                except Exception:
                    yield event.plain_result("图片去重索引更新失败，请稍后重试")
                    return

        chosen = await self._pick_illusts(
            event,
            illusts,
            pick_count,
            source_key=source_key,
            dedupe_enabled=dedupe_days > 0,
            raw_count=raw_count,
        )
        if not chosen:
            yield event.plain_result(
                "当前去重范围内没有未发送过的图片了，换个标签或稍后再试"
            )
            return
        pick_count = len(chosen)
        pending_illust_ids: set[str] = set()
        sent_illust_ids: set[str] = set()
        if dedupe_days > 0 and self.image_index is not None:
            pending_illust_ids = {
                str(illust.get("id") or "") for illust in chosen if illust.get("id")
            }

        # 下载所有图片
        downloaded: list[tuple[dict, str, str, int]] = []
        temp_paths: list[str] = []
        try:
            for idx, illust in enumerate(chosen, 1):
                illust_id = illust.get("id", "?")
                title = illust.get("title", "无标题")

                try:
                    path, actual_q, file_size = await self.downloader.download_for_send(
                        illust,
                        quality,
                        timeout=timeout_sec,
                        downgrade_limit_bytes=downgrade_limit_bytes,
                        log_context=f"[{idx}/{pick_count}] 作品 {illust_id}",
                    )
                    logger.debug(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} "
                        f"下载完成（大小={file_size / 1024:.2f}KB，画质={_search_quality_label(actual_q)}）"
                    )
                    temp_paths.append(path)
                    downloaded.append((illust, path, actual_q, file_size))
                except asyncio.TimeoutError:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"candidate={idx}/{pick_count} reason=timeout "
                        f"timeout_seconds={timeout_sec}"
                    )
                except Exception as e:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"candidate={idx}/{pick_count} reason=download_error "
                        f"error_type={type(e).__name__}"
                    )

            # 统一发送（避免 yield 和 send 混用导致消息拆分）
            if not downloaded:
                yield event.plain_result("😢 所有图片均下载失败，请稍后再试")
                return

            # 非 OneBot 平台不支持合并转发，自动降级。
            forward_threshold = self._forward_threshold()
            use_forward = self._should_use_forward(
                downloaded_count=len(downloaded),
                threshold=forward_threshold,
                platform_name=event.get_platform_name(),
            )
            caption_mode = self._caption_mode()

            if use_forward:
                # 合并转发模式：所有图片打包成一条聊天记录
                raw_self_id = event.get_self_id()
                # AstrBot 的 Node.uin 是字符串，且部分平台的 self_id 不是数字。
                self_id = "0" if raw_self_id is None else str(raw_self_id)
                nodes = Nodes([])
                for illust, path, _actual_q, _file_size in downloaded:
                    content = [
                        Plain(build_caption(illust, caption_mode)),
                        Image.fromFileSystem(path),
                    ]
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=content,
                        )
                    )
                # 如果有下载失败的图片，在合并消息末尾提示
                failed_count = pick_count - len(downloaded)
                if failed_count > 0:
                    failed_ids = [
                        str(il.get("id", "?"))
                        for il in chosen
                        if not any(d[0].get("id") == il.get("id") for d in downloaded)
                    ]
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=[
                                Plain(
                                    f"⚠️ {failed_count} 张图片下载失败（ID: {', '.join(failed_ids)}），已跳过"
                                )
                            ],
                        )
                    )
                # 合并转发（带重试机制）
                max_retries = 3
                forward_success = False
                for attempt in range(1, max_retries + 1):
                    try:
                        await event.send(event.chain_result([nodes]))
                        sent_illust_ids.update(
                            str(illust.get("id") or "")
                            for illust, *_rest in downloaded
                            if illust.get("id")
                        )
                        logger.info(
                            f"{LOG_PREFIX} 合并转发 {len(nodes.nodes)} 条作品"
                            + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                        )
                        forward_success = True
                        break
                    except Exception as e:
                        if attempt < max_retries:
                            wait_sec = attempt * 2
                            logger.info(
                                f"{LOG_PREFIX} 合并转发失败，准备重试: "
                                f"attempt={attempt}/{max_retries} "
                                f"retry_after_seconds={wait_sec} "
                                f"error_type={type(e).__name__}"
                            )
                            await asyncio.sleep(wait_sec)
                        else:
                            friendly_err = self._friendly_send_error(e)
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败，降级为逐条发送: "
                                f"attempts={max_retries} reason={friendly_err} "
                                f"error_type={type(e).__name__}"
                            )

                # 合并转发失败，降级为逐条发送
                if not forward_success:
                    await event.send(
                        event.plain_result("⚠️ 合并转发失败，正在逐条发送...")
                    )
                    for illust, path, actual_q, file_size in downloaded:
                        illust_id = illust.get("id", "?")
                        title = illust.get("title", "无标题")
                        content = [
                            Plain(build_caption(illust, caption_mode)),
                            Image.fromFileSystem(path),
                        ]
                        # 逐条发送（带重试机制）
                        for attempt in range(1, max_retries + 1):
                            try:
                                await event.send(event.chain_result(content))
                                if illust.get("id"):
                                    sent_illust_ids.add(str(illust.get("id")))
                                logger.info(
                                    f"{LOG_PREFIX} [降级] 作品 {illust_id} 已发送"
                                )
                                self._remember_last_sent(event, illust)
                                await self._record_image_usage(
                                    event,
                                    source_key,
                                    illust,
                                    feature="normal",
                                    user_id=str(event.get_sender_id() or ""),
                                )
                                break
                            except Exception as e:
                                if attempt < max_retries:
                                    await asyncio.sleep(attempt * 2)
                                else:
                                    friendly_err = self._friendly_send_error(e)
                                    logger.error(
                                        f"{LOG_PREFIX} 降级发送失败: "
                                        f"illust_id={illust_id} attempts={max_retries} "
                                        f"reason={friendly_err} "
                                        f"error_type={type(e).__name__}"
                                    )
                                    try:
                                        await event.send(
                                            event.plain_result(
                                                f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过"
                                            )
                                        )
                                    except Exception as notify_error:
                                        logger.warning(
                                            f"{LOG_PREFIX} 发送失败提示未能送出: "
                                            f"illust_id={illust_id} "
                                            f"error_type={type(notify_error).__name__}"
                                        )
                else:
                    for illust, path, actual_q, file_size in downloaded:
                        self._remember_last_sent(event, illust)
                        await self._record_image_usage(
                            event,
                            source_key,
                            illust,
                            feature="normal",
                            user_id=str(event.get_sender_id() or ""),
                        )
            else:
                # 逐条发送模式
                for illust, path, actual_q, file_size in downloaded:
                    illust_id = illust.get("id", "?")
                    title = illust.get("title", "无标题")
                    content = [
                        Plain(build_caption(illust, caption_mode)),
                        Image.fromFileSystem(path),
                    ]
                    # 逐条发送（带重试机制）
                    max_retries = 3
                    for attempt in range(1, max_retries + 1):
                        try:
                            await event.send(event.chain_result(content))
                            if illust.get("id"):
                                sent_illust_ids.add(str(illust.get("id")))
                            logger.info(
                                f"{LOG_PREFIX} 作品 {illust_id} 已发送"
                                + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                            )
                            self._remember_last_sent(event, illust)
                            await self._record_image_usage(
                                event,
                                source_key,
                                illust,
                                feature="normal",
                                user_id=str(event.get_sender_id() or ""),
                            )
                            break
                        except Exception as e:
                            if attempt < max_retries:
                                wait_sec = attempt * 2
                                logger.info(
                                    f"{LOG_PREFIX} 作品发送失败，准备重试: "
                                    f"illust_id={illust_id} "
                                    f"attempt={attempt}/{max_retries} "
                                    f"retry_after_seconds={wait_sec} "
                                    f"error_type={type(e).__name__}"
                                )
                                await asyncio.sleep(wait_sec)
                            else:
                                friendly_err = self._friendly_send_error(e)
                                logger.error(
                                    f"{LOG_PREFIX} 作品发送失败: "
                                    f"illust_id={illust_id} attempts={max_retries} "
                                    f"reason={friendly_err} "
                                    f"error_type={type(e).__name__}"
                                )
                                try:
                                    await event.send(
                                        event.plain_result(
                                            f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过"
                                        )
                                    )
                                except Exception as notify_error:
                                    logger.warning(
                                        f"{LOG_PREFIX} 发送失败提示未能送出: "
                                        f"illust_id={illust_id} "
                                        f"error_type={type(notify_error).__name__}"
                                    )

            # 张数不足时明确告知，避免用户以为请求被吞了。
            if not by_id_mode and len(downloaded) < count:
                try:
                    await event.send(
                        event.plain_result(
                            f"ℹ️ 请求 {count} 张，实际发送 {len(downloaded)} 张"
                            "（去重、内容安全或下载失败导致减少）"
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX} 发送张数不足提示失败: "
                        f"requested={count} sent={len(downloaded)} "
                        f"error_type={type(exc).__name__}"
                    )
        finally:
            if self._p_charging_active() and sent_illust_ids:
                total_cost = self._p_unit_cost() * len(sent_illust_ids)
                try:
                    result = await self.checkin_store.spend_coins(
                        user_id=str(event.get_sender_id() or ""),
                        cost=total_cost,
                    )
                    if not result.success:
                        logger.warning(
                            f"{LOG_PREFIX} /p 金币结算未完成: "
                            f"sent_count={len(sent_illust_ids)} cost={total_cost} "
                            f"result_message={result.message}"
                        )
                        try:
                            await event.send(
                                event.plain_result(
                                    f"⚠️ 金币结算未完成：{result.message}"
                                )
                            )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算警告失败: "
                                f"error_type={type(exc).__name__}"
                            )
                    else:
                        logger.info(
                            f"{LOG_PREFIX} /p 金币结算: "
                            f"sent_count={len(sent_illust_ids)} cost={total_cost}"
                        )
                        try:
                            await event.send(
                                event.plain_result(
                                    f"💰 本次发图消耗 {total_cost} 金币"
                                    f"（{len(sent_illust_ids)} 张），余额 {result.profile.coins}。"
                                )
                            )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算提醒失败: "
                                f"error_type={type(exc).__name__}"
                            )
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX} /p 金币结算失败，本次不扣费: "
                        f"error_type={type(exc).__name__}"
                    )
            for p in temp_paths:
                cleanup(p)
            if pending_illust_ids and self.image_index is not None:
                for illust_id in pending_illust_ids - sent_illust_ids:
                    try:
                        await self.image_index.release_usage(
                            scope=self._event_scope(event),
                            source_key=source_key,
                            illust_id=illust_id,
                            feature="normal_pending",
                        )
                    except Exception as e:
                        logger.warning(
                            f"{LOG_PREFIX} 释放未发送作品占用失败，去重记录可能残留: "
                            f"illust_id={illust_id} feature=normal_pending "
                            f"error_type={type(e).__name__} error={e}"
                        )
