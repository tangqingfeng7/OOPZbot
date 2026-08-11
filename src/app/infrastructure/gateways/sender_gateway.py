from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oopz.sdk_gateway import AsyncOopzGateway


class SenderGateway:
    """隔离应用层对 OopzSender 具体实现的直接依赖。

    显式暴露高频业务方法以保留类型信息，低频方法仍通过 __getattr__ 透传。
    """

    def __init__(self, sender: AsyncOopzGateway):
        self._sender = sender

    @property
    def raw(self) -> AsyncOopzGateway:
        return self._sender

    # -- 消息发送 --

    async def send_message(self, text: str, area: str | None = None,
                           channel: str | None = None, **kwargs):
        return await self._sender.send_message(text, area=area, channel=channel, **kwargs)

    async def send_private_message(self, target: str, text: str, **kwargs):
        return await self._sender.send_private_message(target, text, **kwargs)

    async def recall_message(self, message_id: str, **kwargs) -> dict:
        return await self._sender.recall_message(message_id, **kwargs)

    # -- 成员/域查询 --

    async def get_area_members(self, **kwargs) -> dict:
        return await self._sender.get_area_members(**kwargs)

    async def get_person_detail(self, **kwargs) -> dict:
        return await self._sender.get_person_detail(**kwargs)

    async def get_person_detail_full(self, uid: str, **kwargs) -> dict:
        return await self._sender.get_person_detail_full(uid, **kwargs)

    async def get_person_infos_batch(self, uids: list, **kwargs) -> dict:
        return await self._sender.get_person_infos_batch(uids, **kwargs)

    async def get_user_area_detail(self, uid: str, **kwargs) -> dict:
        return await self._sender.get_user_area_detail(uid, **kwargs)

    async def search_area_members(self, **kwargs):
        return await self._sender.search_area_members(**kwargs)

    # -- 角色管理 --

    async def get_assignable_roles(self, uid: str, **kwargs):
        return await self._sender.get_assignable_roles(uid, **kwargs)

    async def edit_user_role(self, uid: str, role_id, **kwargs) -> dict:
        return await self._sender.edit_user_role(uid, role_id, **kwargs)

    # -- 审核管理 --

    async def mute_user(self, uid: str, **kwargs) -> dict:
        return await self._sender.mute_user(uid, **kwargs)

    async def unmute_user(self, uid: str, **kwargs) -> dict:
        return await self._sender.unmute_user(uid, **kwargs)

    async def mute_mic(self, uid: str, **kwargs) -> dict:
        return await self._sender.mute_mic(uid, **kwargs)

    async def unmute_mic(self, uid: str, **kwargs) -> dict:
        return await self._sender.unmute_mic(uid, **kwargs)

    async def remove_from_area(self, uid: str, **kwargs) -> dict:
        return await self._sender.remove_from_area(uid, **kwargs)

    async def unblock_user_in_area(self, uid: str, **kwargs) -> dict:
        return await self._sender.unblock_user_in_area(uid, **kwargs)

    async def get_area_blocks(self, **kwargs) -> dict:
        return await self._sender.get_area_blocks(**kwargs)

    # -- 语音频道 --

    async def get_voice_channel_members(self, **kwargs):
        return await self._sender.get_voice_channel_members(**kwargs)

    async def get_voice_channel_for_user(self, user_uid: str, **kwargs):
        return await self._sender.get_voice_channel_for_user(user_uid, **kwargs)

    async def drag_member(self, target: str, to_channel: str, **kwargs) -> dict:
        return await self._sender.drag_member(target, to_channel, **kwargs)

    # -- 频道消息 --

    async def get_channel_messages(self, **kwargs):
        return await self._sender.get_channel_messages(**kwargs)

    async def find_message_timestamp(self, message_id: str, **kwargs):
        return await self._sender.find_message_timestamp(message_id, **kwargs)

    # -- 文件上传 --

    async def upload_file_from_url(self, url: str, **kwargs):
        return await self._sender.upload_file_from_url(url, **kwargs)

    # -- 其他 --

    async def get_daily_speech(self, **kwargs) -> dict:
        return await self._sender.get_daily_speech(**kwargs)

    async def get_joined_areas(self, **kwargs):
        return await self._sender.get_joined_areas(**kwargs)

    def __getattr__(self, name: str):
        return getattr(self._sender, name)
