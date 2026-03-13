import json
import asyncio
import uuid
import inspect
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

@register("QQ_Group_Manager", "SunflowerYuuka", "QQ群管家插件：自动处理入群申请、调用LLM迎新送辞、邀请进群审核", "v1.2.0")
class GroupManager(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        data_dir = StarTools.get_data_dir()
        self.pending_file = data_dir / "pending_invites.json"
        self.pending_invites = self._load_pending()
        
        self._background_tasks = set()
        
        logger.info(f"[QQ群管家] 插件加载成功，当前待审核邀请数: {len(self.pending_invites)}")

    def _load_pending(self) -> dict:
        if self.pending_file.exists():
            try:
                with open(self.pending_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[QQ群管家] 读取待审核记录失败: {e}")
        return {}

    def _save_pending(self):
        try:
            with open(self.pending_file, "w", encoding="utf-8") as f:
                json.dump(self.pending_invites, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[QQ群管家] 保存待审核记录失败: {e}")

    def _is_group_managed(self, group_id: int, config: dict) -> bool:
        list_mode = config.get("filter_list_mode", "黑名单")
        group_list = config.get("filter_group_list", [])
        
        str_group_id = str(group_id)
        str_group_list = [str(g) for g in group_list]

        if list_mode == "黑名单":
            return str_group_id not in str_group_list
        elif list_mode == "白名单":
            return str_group_id in str_group_list
        
        return True

    @filter.command("bot_join")
    async def handle_bot_join(self, event: AstrMessageEvent, invite_id: str, decision: str):
        config = self.get_config() if hasattr(self, "get_config") else self.config
        if not config:
            config = self.config

        sender_id = str(event.get_sender_id())
        admin_qqs = [q.strip() for q in config.get("admin_qq", "").split(",") if q.strip()]
        
        if sender_id not in admin_qqs:
            yield event.plain_result("您没有权限执行此操作，请在 WebUI 中配置管理员 QQ。")
            return

        self.pending_invites = self._load_pending()
        if invite_id not in self.pending_invites:
            yield event.plain_result(f"找不到 ID 为 {invite_id} 的待审核记录，可能已处理或已超时。")
            return

        invite = self.pending_invites[invite_id]
        flag = invite["flag"]
        user_id = invite["user_id"]
        group_id = invite["group_id"]

        if decision not in ["同意", "拒绝"]:
            yield event.plain_result("指令格式错误。正确用法: /bot_join <审核ID> <同意/拒绝>")
            return

        approve = (decision == "同意")

        try:
            await event.bot.api.call_action("set_group_add_request", flag=flag, sub_type="invite", approve=approve)
            
            status_str = "同意" if approve else "拒绝"
            await event.bot.api.call_action(
                "send_private_msg",
                user_id=user_id,
                message=f"您邀请我加入群聊 {group_id} 的请求已被管理员【{status_str}】。"
            )
            
            del self.pending_invites[invite_id]
            self._save_pending()
            
            yield event.plain_result(f"已【{status_str}】该邀请，并已私聊通知邀请人。")
            
        except Exception as e:
            yield event.plain_result(f"操作失败，可能是请求已过期: {e}")

    async def _timeout_task(self, invite_id: str, bot):
        await asyncio.sleep(24 * 3600)
        
        self.pending_invites = self._load_pending()
        if invite_id in self.pending_invites:
            invite = self.pending_invites[invite_id]
            try:
                await bot.api.call_action("set_group_add_request", flag=invite["flag"], sub_type="invite", approve=False)
                await bot.api.call_action(
                    "send_private_msg",
                    user_id=invite["user_id"],
                    message=f"您邀请我加入群聊 {invite['group_id']} 的请求已超时（24小时未审核），系统已自动拒绝。"
                )
                del self.pending_invites[invite_id]
                self._save_pending()
                logger.info(f"[QQ群管家] 邀请 {invite_id} 已超时，自动拒绝成功。")
            except Exception as e:
                logger.error(f"[QQ群管家] 超时自动拒绝失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_group_events(self, event: AstrMessageEvent):
        config = self.get_config() if hasattr(self, "get_config") else self.config
        if not config:
            config = self.config
            
        if not hasattr(event, "message_obj"):
            return
        raw_msg = getattr(event.message_obj, "raw_message", None)
        if not raw_msg or not isinstance(raw_msg, dict):
            return

        post_type = raw_msg.get("post_type")
        
        if post_type == "request" and raw_msg.get("request_type") == "group":
            sub_type = raw_msg.get("sub_type")
            if sub_type == "add":
                await self._handle_group_add(event, raw_msg, config)
            elif sub_type == "invite":
                await self._handle_group_invite(event, raw_msg, config)
                
        elif post_type == "notice":
            notice_type = raw_msg.get("notice_type")
            if notice_type == "group_increase":
                await self._handle_group_increase(event, raw_msg, config)
            elif notice_type == "group_decrease":
                await self._handle_group_decrease(event, raw_msg, config)

    async def _handle_group_add(self, event: AstrMessageEvent, raw_msg: dict, config: dict):
        group_id = raw_msg.get("group_id")
        if not self._is_group_managed(group_id, config):
            return

        if config.get("auto_approve", True):
            try:
                await event.bot.api.call_action(
                    "set_group_add_request",
                    flag=raw_msg.get("flag"),
                    sub_type="add",
                    approve=True
                )
                logger.info(f"[QQ群管家] 已自动同意用户 {raw_msg.get('user_id')} 加入群 {group_id} 的申请。")
            except Exception as e:
                logger.error(f"[QQ群管家] 自动处理入群申请失败: {e}")

    async def _handle_group_invite(self, event: AstrMessageEvent, raw_msg: dict, config: dict):
        flag = raw_msg.get("flag")
        user_id = raw_msg.get("user_id")
        group_id = raw_msg.get("group_id")

        invite_id = uuid.uuid4().hex[:6]
        self.pending_invites[invite_id] = {
            "flag": flag,
            "user_id": user_id,
            "group_id": group_id
        }
        self._save_pending()

        prompt_template = config.get("invite_prompt", "我已收到您邀请我加入群聊（群号：{group_id}）的请求，我已经通知我的管理员啦，请耐心等待管理员审核哦~")
        prompt = prompt_template.format(user_id=user_id, group_id=group_id)
        
        llm_msg = await self._generate_llm_response(prompt, group_id, config)
        if not llm_msg:
            fallback_template = config.get("invite_fallback", "已通知管理员，请等待审核。")
            llm_msg = fallback_template.format(user_id=user_id, group_id=group_id)
            
        try:
            await event.bot.api.call_action("send_private_msg", user_id=user_id, message=llm_msg)
        except Exception as e:
            logger.error(f"[QQ群管家] 发送等待审核提示失败: {e}")

        admin_qqs = [q.strip() for q in config.get("admin_qq", "").split(",") if q.strip()]
        if admin_qqs:
            admin_msg = (
                f"收到新的进群邀请！\n"
                f"群号：{group_id}\n"
                f"邀请人：{user_id}\n"
                f"审核ID：{invite_id}\n"
                f"请回复指令进行审核：\n"
                f"/bot_join {invite_id} 同意\n"
                f"/bot_join {invite_id} 拒绝"
            )
            for admin in admin_qqs:
                try:
                    await event.bot.api.call_action("send_private_msg", user_id=int(admin), message=admin_msg)
                except Exception as e:
                    logger.error(f"[QQ群管家] 通知管理员 {admin} 失败: {e}")
        else:
            logger.warning("[QQ群管家] 收到进群邀请，但未配置管理员 QQ，将无法审核！")

        task = asyncio.create_task(self._timeout_task(invite_id, event.bot))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _handle_group_increase(self, event: AstrMessageEvent, raw_msg: dict, config: dict):
        group_id = raw_msg.get("group_id")
        user_id = raw_msg.get("user_id")
        
        if not self._is_group_managed(group_id, config):
            return

        if config.get("welcome_enable", True):
            prompt_template = config.get("welcome_prompt", "")
            prompt = prompt_template.format(user_id=user_id, group_id=group_id)
            
            welcome_msg = await self._generate_llm_response(prompt, group_id, config)
            if not welcome_msg:
                fallback_template = config.get("welcome_fallback", "欢迎加入群聊！")
                welcome_msg = fallback_template.format(user_id=user_id, group_id=group_id)
                
            try:
                await event.bot.api.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=f"[CQ:at,qq={user_id}] {welcome_msg}"
                )
            except Exception as e:
                logger.error(f"[QQ群管家] 发送迎新消息失败: {e}")

    async def _handle_group_decrease(self, event: AstrMessageEvent, raw_msg: dict, config: dict):
        group_id = raw_msg.get("group_id")
        user_id = raw_msg.get("user_id")
        
        if not self._is_group_managed(group_id, config):
            return

        if config.get("farewell_enable", True):
            prompt_template = config.get("farewell_prompt", "")
            prompt = prompt_template.format(user_id=user_id, group_id=group_id)
            
            farewell_msg = await self._generate_llm_response(prompt, group_id, config)
            if not farewell_msg:
                fallback_template = config.get("farewell_fallback", "有缘再见。")
                farewell_msg = fallback_template.format(user_id=user_id, group_id=group_id)
                
            try:
                await event.bot.api.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=farewell_msg
                )
            except Exception as e:
                logger.error(f"[QQ群管家] 发送送辞消息失败: {e}")

    async def _generate_llm_response(self, prompt: str, group_id: int, config: dict) -> str:
        try:
            primary_provider_id = config.get("llm_provider_id", "").strip()
            fallback_1 = config.get("fallback_provider_1", "").strip()
            fallback_2 = config.get("fallback_provider_2", "").strip()
            persona_id = config.get("llm_persona_id", "").strip()

            system_prompt = ""
            if persona_id and persona_id != "系统默认":
                try:
                    if hasattr(self.context, "persona_manager"):
                        persona = self.context.persona_manager.get_persona(persona_id)
                        if inspect.iscoroutine(persona):
                            persona = await persona
                            
                        if persona:
                            possible_keys = ["prompt", "bot_info", "system_prompt", "system", "content"]
                            if isinstance(persona, dict):
                                for key in possible_keys:
                                    if persona.get(key):
                                        system_prompt = persona.get(key)
                                        break
                            else:
                                for key in possible_keys:
                                    if hasattr(persona, key) and getattr(persona, key):
                                        system_prompt = getattr(persona, key)
                                        break
                                if not system_prompt and hasattr(persona, "config") and isinstance(persona.config, dict):
                                    for key in possible_keys:
                                        if persona.config.get(key):
                                            system_prompt = persona.config.get(key)
                                            break
                except Exception as e:
                    logger.warning(f"[QQ群管家] 获取人格 [{persona_id}] 失败: {e}")

            final_prompt = prompt
            if system_prompt:
                final_prompt = (
                    f"【系统设定/人格要求】\n{system_prompt}\n\n"
                    f"【当前任务】\n{prompt}\n\n"
                    f"(注：请严格遵循上述【系统设定】中的人格、语气和角色扮演要求来完成【当前任务】。)"
                )

            ids_to_try = []
            if primary_provider_id:
                ids_to_try.append(primary_provider_id)
                
            for f_id in [fallback_1, fallback_2]:
                if f_id and f_id not in ids_to_try:
                    ids_to_try.append(f_id)
                    
            if not ids_to_try:
                ids_to_try.append("")

            for prov_id in ids_to_try:
                try:
                    logger.info(f"[QQ群管家] 尝试使用大模型 [{prov_id or '系统默认'}] 生成回复...")
                    
                    if hasattr(self.context, "llm_generate"):
                        response = await self.context.llm_generate(
                            chat_provider_id=prov_id if prov_id else None,
                            prompt=final_prompt
                        )
                        if response and hasattr(response, "completion_text") and response.completion_text.strip():
                            return response.completion_text.strip()
                    else:
                        provider_key = prov_id.split('/')[0] if prov_id else ""
                        provider = self.context.providers.get(provider_key) or self.context.get_using_provider()
                        if provider:
                            response = await provider.text_chat(prompt=final_prompt, session_id=f"group_manager_{group_id}")
                            if hasattr(response, "completion_text") and response.completion_text.strip():
                                return response.completion_text.strip()
                                
                    logger.warning(f"[QQ群管家] 模型 [{prov_id or '系统默认'}] 返回了空文本，尝试下一个...")
                except Exception as e:
                    logger.warning(f"[QQ群管家] 模型 [{prov_id or '系统默认'}] 生成失败: {e}，尝试下一个...")
                    continue

            logger.error("[QQ群管家] 所有配置的大模型均生成失败，将使用静态兜底文本！")
            return ""
                
        except Exception as e:
            logger.error(f"[QQ群管家] LLM 生成流程发生异常: {e}")
            return ""
