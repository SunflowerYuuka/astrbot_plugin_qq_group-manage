import os
import json
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

@register("qq_group_manager", "SunflowerYuuka", "QQ群管家插件：自动处理入群申请、调用LLM迎新送辞、邀请进群审核", "1.0.0")
class GroupManager(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 用于持久化存储待审核的邀请
        self.pending_file = os.path.join(os.path.dirname(__file__), "pending_invites.json")
        self.pending_invites = self._load_pending()
        
        logger.info(f"[QQ群管家] 插件加载成功，当前待审核邀请数: {len(self.pending_invites)}")

    def _load_pending(self) -> dict:
        if os.path.exists(self.pending_file):
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

    def _is_group_managed(self, group_id: int) -> bool:
        list_mode = self.config.get("filter_list_mode", "黑名单")
        group_list = self.config.get("filter_group_list", [])
        
        str_group_id = str(group_id)
        str_group_list = [str(g) for g in group_list]

        if list_mode == "黑名单":
            return str_group_id not in str_group_list
        elif list_mode == "白名单":
            return str_group_id in str_group_list
        
        return True

    @filter.command("bot_join")
    async def handle_bot_join(self, event: AstrMessageEvent, invite_id: str, decision: str):
        """
        管理员审核邀请进群指令
        用法: /bot_join <审核ID> <同意/拒绝>
        """
        sender_id = str(event.get_sender_id())
        admin_qqs = [q.strip() for q in self.config.get("admin_qq", "").split(",") if q.strip()]
        
        if sender_id not in admin_qqs:
            yield event.plain_result(" 您没有权限执行此操作，请在 WebUI 中配置管理员 QQ。")
            return

        self.pending_invites = self._load_pending()
        if invite_id not in self.pending_invites:
            yield event.plain_result(f" 找不到 ID 为 {invite_id} 的待审核记录，可能已处理或已超时。")
            return

        invite = self.pending_invites[invite_id]
        flag = invite["flag"]
        user_id = invite["user_id"]
        group_id = invite["group_id"]

        if decision not in ["同意", "拒绝"]:
            yield event.plain_result(" 指令格式错误。正确用法: /bot_join <审核ID> <同意/拒绝>")
            return

        approve = (decision == "同意")

        try:
            # 1. 调用 API 处理请求
            await event.bot.api.call_action(
                "set_group_add_request",
                flag=flag,
                sub_type="invite",
                approve=approve
            )
            
            # 2. 通知邀请者
            status_str = "同意" if approve else "拒绝"
            await event.bot.api.call_action(
                "send_private_msg",
                user_id=user_id,
                message=f" 您邀请我加入群聊 {group_id} 的请求已被管理员【{status_str}】。"
            )
            
            # 3. 清理记录
            del self.pending_invites[invite_id]
            self._save_pending()
            
            yield event.plain_result(f" 已【{status_str}】该邀请，并已私聊通知邀请人。")
            
        except Exception as e:
            yield event.plain_result(f" 操作失败，可能是请求已过期: {e}")

    async def _timeout_task(self, invite_id: str, bot):
        await asyncio.sleep(24 * 3600) # 等待 24 小时
        
        self.pending_invites = self._load_pending()
        if invite_id in self.pending_invites:
            invite = self.pending_invites[invite_id]
            try:
                await bot.api.call_action(
                    "set_group_add_request",
                    flag=invite["flag"],
                    sub_type="invite",
                    approve=False
                )
                await bot.api.call_action(
                    "send_private_msg",
                    user_id=invite["user_id"],
                    message=f" 您邀请我加入群聊 {invite['group_id']} 的请求已超时（24小时未审核），系统已自动拒绝。"
                )
                del self.pending_invites[invite_id]
                self._save_pending()
                logger.info(f"[QQ群管家] 邀请 {invite_id} 已超时，自动拒绝成功。")
            except Exception as e:
                logger.error(f"[QQ群管家] 超时自动拒绝失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_group_events(self, event: AstrMessageEvent):
        if not hasattr(event, "message_obj"):
            return
        raw_msg = getattr(event.message_obj, "raw_message", None)
        if not raw_msg or not isinstance(raw_msg, dict):
            return

        post_type = raw_msg.get("post_type")
        
        if post_type == "request" and raw_msg.get("request_type") == "group":
            sub_type = raw_msg.get("sub_type")
            flag = raw_msg.get("flag")
            user_id = raw_msg.get("user_id")
            group_id = raw_msg.get("group_id")

            if sub_type == "add":
                if not self._is_group_managed(group_id):
                    return
                if self.config.get("auto_approve", True):
                    try:
                        await event.bot.api.call_action(
                            "set_group_add_request",
                            flag=flag,
                            sub_type="add",
                            approve=True
                        )
                        logger.info(f"[QQ群管家] 已自动同意用户 {user_id} 加入群 {group_id} 的申请。")
                    except Exception as e:
                        logger.error(f"[QQ群管家] 自动处理入群申请失败: {e}")

            elif sub_type == "invite":
                invite_id = str(int(time.time()))[-6:] # 使用时间戳后6位作为简短ID
                self.pending_invites[invite_id] = {
                    "flag": flag,
                    "user_id": user_id,
                    "group_id": group_id,
                    "time": time.time()
                }
                self._save_pending()

                prompt_template = self.config.get("invite_prompt", "我已收到您邀请我加入群聊（群号：{group_id}）的请求，我已经通知我的管理员啦，请耐心等待管理员审核哦~")
                prompt = prompt_template.format(user_id=user_id, group_id=group_id)
                llm_msg = await self._generate_llm_response(prompt, group_id)
                if llm_msg:
                    try:
                        await event.bot.api.call_action("send_private_msg", user_id=user_id, message=llm_msg)
                    except Exception as e:
                        logger.error(f"[QQ群管家] 发送等待审核提示失败: {e}")

                admin_qqs = [q.strip() for q in self.config.get("admin_qq", "").split(",") if q.strip()]
                if admin_qqs:
                    admin_msg = (
                        f" 收到新的进群邀请！\n"
                        f"群号：{group_id}\n"
                        f"邀请人：{user_id}\n"
                        f"审核ID：{invite_id}\n"
                        f" 请回复指令进行审核：\n"
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

                asyncio.create_task(self._timeout_task(invite_id, event.bot))

        elif post_type == "notice":
            notice_type = raw_msg.get("notice_type")
            group_id = raw_msg.get("group_id")
            user_id = raw_msg.get("user_id")
            
            if not self._is_group_managed(group_id):
                return

            if notice_type == "group_increase" and self.config.get("welcome_enable", True):
                prompt_template = self.config.get("welcome_prompt", "")
                prompt = prompt_template.format(user_id=user_id, group_id=group_id)
                
                welcome_msg = await self._generate_llm_response(prompt, group_id)
                if welcome_msg:
                    try:
                        await event.bot.api.call_action(
                            "send_group_msg",
                            group_id=group_id,
                            message=f"[CQ:at,qq={user_id}] {welcome_msg}"
                        )
                    except Exception as e:
                        logger.error(f"[QQ群管家] 发送迎新消息失败: {e}")
                        
            elif notice_type == "group_decrease" and self.config.get("farewell_enable", True):
                prompt_template = self.config.get("farewell_prompt", "")
                prompt = prompt_template.format(user_id=user_id, group_id=group_id)
                
                farewell_msg = await self._generate_llm_response(prompt, group_id)
                if farewell_msg:
                    try:
                        await event.bot.api.call_action(
                            "send_group_msg",
                            group_id=group_id,
                            message=farewell_msg
                        )
                    except Exception as e:
                        logger.error(f"[QQ群管家] 发送送辞消息失败: {e}")

    async def _generate_llm_response(self, prompt: str, group_id: int) -> str:
        try:
            provider_id = self.config.get("llm_provider_id", "")
            persona_id = self.config.get("llm_persona_id", "").strip()

            provider = None
            if provider_id and hasattr(self.context, "providers") and provider_id in self.context.providers:
                provider = self.context.providers[provider_id]
            else:
                provider = self.context.get_using_provider()

            if not provider:
                return ""

            system_prompt = ""
            if persona_id:
                try:
                    if hasattr(self.context, "persona_manager") and hasattr(self.context.persona_manager, "personas"):
                        personas_collection = self.context.persona_manager.personas
                        actual_persona = None

                        if isinstance(personas_collection, dict):
                            actual_persona = personas_collection.get(persona_id)
                        elif isinstance(personas_collection, list):
                            for p in personas_collection:
                                pid = ""
                                if isinstance(p, dict):
                                    pid = p.get("persona_id", p.get("name", p.get("id", "")))
                                else:
                                    pid = getattr(p, "persona_id", getattr(p, "name", getattr(p, "id", "")))
                                
                                if pid == persona_id:
                                    actual_persona = p
                                    break

                        if actual_persona:
                            if isinstance(actual_persona, dict):
                                system_prompt = actual_persona.get("bot_info", "") or actual_persona.get("system_prompt", "")
                            else:
                                system_prompt = getattr(actual_persona, "bot_info", "")
                                if not system_prompt:
                                    system_prompt = getattr(actual_persona, "system_prompt", "")
                                if not system_prompt and hasattr(actual_persona, "config") and isinstance(actual_persona.config, dict):
                                    system_prompt = actual_persona.config.get("bot_info", "") or actual_persona.config.get("system_prompt", "")
                except Exception as e:
                    logger.warning(f"[QQ群管家] 获取人格 [{persona_id}] 失败: {e}")

            final_prompt = prompt
            if system_prompt:
                final_prompt = (
                    f"【系统设定/人格要求】\n{system_prompt}\n\n"
                    f"【当前任务】\n{prompt}\n\n"
                    f"(注：请严格遵循上述【系统设定】中的人格、语气和角色扮演要求来完成【当前任务】。)"
                )

            session_id = f"group_manager_{group_id}"
            
            response = await provider.text_chat(
                prompt=final_prompt, 
                session_id=session_id,
                system_prompt=system_prompt 
            )
            
            if hasattr(response, "completion_text"):
                return response.completion_text
            elif isinstance(response, dict) and "text" in response:
                return response["text"]
            else:
                return str(response)
                
        except Exception as e:
            logger.error(f"[QQ群管家] LLM 生成消息失败: {e}")
            return ""
