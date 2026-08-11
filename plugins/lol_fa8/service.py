import asyncio
import contextlib
import hashlib
import json
import os
import re
import time

from core.async_http import ManagedHttpClient
from core.constants import USER_AGENT, Msg
from core.logger_config import get_logger

logger = get_logger("FA8")

BASE_URL = "https://fa.3ui.cc"

def _default_config() -> dict:
    """从插件 config_spec 派生默认配置 —— 默认值的单一来源在 __init__.py。"""
    from plugins.lol_fa8 import LolFa8Plugin

    return {field.name: field.default for field in LolFa8Plugin().config_spec.fields}

SERVERS = {
    "1": "艾欧尼亚", "2": "比尔吉沃特", "3": "祖安", "4": "诺克萨斯",
    "5": "班德尔城", "6": "德玛西亚", "7": "皮尔特沃夫", "8": "战争学院",
    "9": "弗雷尔卓德", "10": "巨神峰", "11": "雷瑟守备", "12": "无畏先锋",
    "13": "裁决之地", "14": "黑色玫瑰", "15": "暗影岛", "16": "恕瑞玛",
    "17": "钢铁烈阳", "18": "水晶之痕", "19": "均衡教派", "20": "扭曲丛林",
    "21": "教育网专区", "22": "影流", "23": "守望之海", "24": "征服之海",
    "25": "卡拉曼达", "26": "巨龙之巢", "27": "皮城警备", "30": "男爵领域",
    "31": "峡谷之巅",
}

SERVER_NAME_TO_ID = {v: k for k, v in SERVERS.items()}

SERVER_GROUPS: dict[str, list[str]] = {
    "一区": ["3", "7", "10", "19", "21", "22", "23", "30"],
    "二区": ["4", "8", "11", "15", "24", "25"],
    "三区": ["5", "13", "17", "18", "27"],
    "四区": ["2", "9", "20"],
    "五区": ["6", "12", "16", "26"],
}

GROUP_ALIASES: dict[str, str] = {}
_NUM_MAP = {"一区": "1", "二区": "2", "三区": "3", "四区": "4", "五区": "5"}
for _g in SERVER_GROUPS:
    GROUP_ALIASES[_g] = _g
    GROUP_ALIASES[f"联盟{_g}"] = _g
    if _g in _NUM_MAP:
        GROUP_ALIASES[_NUM_MAP[_g]] = _g


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _ts() -> int:
    return int(time.time() * 1000)


_RE_MASTERY = re.compile(
    r'alt="([^"]+)".*?'
    r'class="font-medium text-white mr-2">([^<]+)</span>.*?'
    r'text-gray-400">(\d+级英雄成就)</span>.*?'
    r'熟练度：(\d+)',
    re.DOTALL,
)
_RE_CARD_SPLIT = re.compile(r'class="match-card\s+')
_RE_CHAMP = re.compile(r'champion/(\w+)\.png')
_RE_KDA = re.compile(r'font-bold text-white">(\d+/\d+/\d+)<')
_RE_SCORE = re.compile(r'评分\s*([\d.]+)')
_RE_MODE = re.compile(r'text-xs">([^<]+)</span>\s*<span[^>]*>时长([\d:]+)')
_RE_DATE = re.compile(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})')
_RE_HERO = re.compile(r"英雄:\s*(\d+)\s*个")
_RE_SKIN = re.compile(r"皮肤:\s*(\d+)\s*个")

_CHAMPION_NAMES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "champion_names.json")

with open(_CHAMPION_NAMES_PATH, encoding="utf-8") as _f:
    CHAMPION_EN_TO_CN: dict[str, str] = json.load(_f)


def _champion_cn(en_key: str) -> str:
    """将英雄英文 ID 转为中文名，未知则返回原样"""
    if not en_key:
        return en_key
    key = en_key.strip()
    if key in CHAMPION_EN_TO_CN:
        return CHAMPION_EN_TO_CN[key]
    # 兼容小写或首字母大写的 key（如 neeko -> Neeko）
    key_alt = key[0].upper() + key[1:].lower() if len(key) > 1 else key.upper()
    return CHAMPION_EN_TO_CN.get(key_alt, en_key)


def _parse_mastery(html: str) -> list[dict]:
    """从 mastery HTML 中解析英雄熟练度列表"""
    results = []
    for m in _RE_MASTERY.finditer(html):
        results.append({
            "name": m.group(2),
            "level": m.group(3),
            "points": int(m.group(4)),
        })
    return results


def _parse_match_cards(html: str) -> list[dict]:
    """从战绩 HTML 中解析对局列表（分段解析避免回溯）"""
    results = []
    cards = _RE_CARD_SPLIT.split(html)
    for card in cards[1:]:
        try:
            win_lose = "胜利" if card.startswith("win") else "失败"
            champ_m = _RE_CHAMP.search(card)
            kda_m = _RE_KDA.search(card)
            if not (champ_m and kda_m):
                continue
            score_m = _RE_SCORE.search(card)
            mode_m = _RE_MODE.search(card)
            date_m = _RE_DATE.search(card)
            results.append({
                "result": win_lose,
                "champion": champ_m.group(1),
                "kda": kda_m.group(1),
                "score": score_m.group(1) if score_m else "",
                "mode": mode_m.group(1) if mode_m else "",
                "duration": mode_m.group(2) if mode_m else "",
                "date": date_m.group(1) if date_m else "",
            })
        except Exception:
            continue
    return results


class FA8Client:
    """FA8 API 异步客户端，自动管理登录态。"""

    _KEEPALIVE_INTERVAL = 5  # 秒

    def __init__(self, username: str = "", password: str = ""):
        self._user = (username or "").strip()
        self._pwd = (password or "").strip()
        self._http = ManagedHttpClient(
            headers={
                "User-Agent": USER_AGENT,
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            }
        )
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._start_keepalive()

    # ------------------------------------------------------------------
    # 登录保活
    # ------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        """启动可取消的异步保活任务。"""
        if not self._user or not self._pwd:
            return
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._stop_event.clear()
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(),
            name="fa8-keepalive",
        )
        logger.info("FA8 异步保活已启动 (间隔 %ds)", self._KEEPALIVE_INTERVAL)

    async def _keepalive_loop(self) -> None:
        """后台循环：检查登录状态，失效则自动重新登录"""
        while not self._stop_event.is_set():
            try:
                await self._check_and_login()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"FA8 保活检查异常: {e}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._KEEPALIVE_INTERVAL,
                )

    async def _check_and_login(self) -> None:
        """检查 Cookie 是否有效，无效则重新登录"""
        async with self._lock:
            session = await self._http.session()
            cookies = {cookie.key: cookie.value for cookie in session.cookie_jar}
            if cookies.get("name") and cookies.get("sign"):
                self._logged_in = True
                return
            self._logged_in = False
            await self._do_login()

    async def _do_login(self) -> bool:
        """执行登录请求（调用方需持有 _lock）"""
        try:
            ts = _ts()
            _status, data = await self._http.request_payload(
                "POST",
                f"{BASE_URL}/api/api.php?act=login",
                data={"user": self._user, "pwd": self._pwd, "time": ts},
                timeout=15,
            )
            if isinstance(data, dict) and data.get("code") == 0:
                self._logged_in = True
                logger.info("FA8 登录成功")
                return True
            message = data.get("msg", "未知错误") if isinstance(data, dict) else "响应格式错误"
            logger.warning(f"FA8 登录失败: {message}")
            return False
        except Exception as e:
            logger.error(f"FA8 登录异常: {e}")
            return False

    async def _ensure_login(self) -> bool:
        if self._logged_in:
            return True
        async with self._lock:
            if self._logged_in:
                return True
            if not self._user or not self._pwd:
                logger.error("FA8 账号或密码未配置")
                return False
            return await self._do_login()

    async def _post_api(self, endpoint: str, data: dict, retry: bool = True) -> dict:
        """发送 API 请求，自动处理登录"""
        if not await self._ensure_login():
            return {"code": -1, "msg": "登录失败，请检查 FA8 账号配置"}
        try:
            _status, result = await self._http.request_payload(
                "POST",
                f"{BASE_URL}/api/{endpoint}",
                data=data,
                timeout=10,
            )
            if not isinstance(result, dict):
                return {"code": -1, "msg": "响应格式错误"}
            msg = str(result.get("msg", ""))
            is_auth_error = result.get("code") != 0 and "登录" in msg
            if is_auth_error and retry:
                self._logged_in = False
                return await self._post_api(endpoint, data, retry=False)
            return result
        except Exception as e:
            logger.error(f"FA8 API 请求失败 [{endpoint}]: {e}")
            return {"code": -1, "msg": f"请求异常: {e}"}

    async def query_summoner(self, name: str, area: str) -> dict:
        """查询召唤师基本信息"""
        ts = _ts()
        sign = _md5(f"{name}{ts}{area}{ts}#6352")
        return await self._post_api("tyapi.php?act=cxinfo", {
            "name": name, "area": area, "sign": sign, "time": ts,
        })

    async def query_games(self, puuid: str, area: str, tag: str = "all", page: str = "0") -> dict:
        """查询历史战绩"""
        ts = _ts()
        sign = _md5(f"{puuid}{ts}{area}{ts}{tag}{page}#6662")
        return await self._post_api("tyapi.php?act=cxgame", {
            "puuid": puuid, "area": area, "page": page,
            "sign": sign, "tag": tag, "time": ts,
        })

    async def query_current_game(self, puuid: str, area: str) -> dict:
        """查询当前对局"""
        ts = _ts()
        sign = _md5(f"{puuid}{ts}{area}{ts}#6362")
        return await self._post_api("tyapi.php?act=nowcx", {
            "puuid": puuid, "area": area, "sign": sign, "time": ts,
        })

    async def stop(self) -> None:
        """停止保活任务并释放网络资源。"""
        self._stop_event.set()
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._http.close()


class FA8Handler:
    """FA8 战绩查询命令处理器"""

    def __init__(self, config: dict | None = None):
        self._config = _default_config()
        if config:
            self._config.update(config)
        self._client = FA8Client(
            username=self._config.get("username", ""),
            password=self._config.get("password", ""),
        )

    async def close(self):
        """释放内部客户端资源（插件卸载时调用）。"""
        with contextlib.suppress(Exception):
            await self._client.stop()

    def _resolve_area(self, text: str) -> tuple[str, list[str]]:
        """
        从输入文本中解析大区和召唤师名。
        返回 (召唤师名, 大区ID列表)。
        支持格式:
          - "召唤师名#编号"              → 使用默认大区
          - "大区名 召唤师名#编号"       → 指定大区
          - "一区 召唤师名#编号"         → 搜索整个区组
          - "联盟一区 召唤师名#编号"     → 搜索整个区组
        """
        text = text.strip()
        default_area = self._config.get("default_area", "1")

        parts = text.split(None, 1)
        if len(parts) == 2:
            prefix = parts[0]
            if prefix in GROUP_ALIASES:
                return parts[1], SERVER_GROUPS[GROUP_ALIASES[prefix]]
            if prefix in SERVERS:
                return parts[1], [prefix]
            if prefix in SERVER_NAME_TO_ID:
                return parts[1], [SERVER_NAME_TO_ID[prefix]]

        for server_name, server_id in SERVER_NAME_TO_ID.items():
            if text.startswith(server_name):
                name = text[len(server_name):].strip()
                if name:
                    return name, [server_id]

        return text, [default_area]

    async def _search_summoner(self, name: str, areas: list[str]) -> tuple[str, dict] | None:
        """在多个大区中并行搜索召唤师，返回 (area_id, info) 或 None。

        全部大区并发发起，但**按 areas 顺序**逐个等待结果：

        - 结果确定：同名召唤师存在于多个大区时，总是返回配置顺序靠前的那个，
          不像「谁先响应谁赢」那样随机。
        - 尽早返回：命中即返回并取消其余请求，不必像 ``gather`` 那样等最慢的大区
          （单次查询超时 10s，区组搜索会同时查好几个区）。

        既然要保证区序，就不可能在高优先大区出结果之前返回低优先大区的命中，
        因此「按序等待」已经是确定性前提下最早的返回时机。
        """
        if len(areas) == 1:
            info = await self._client.query_summoner(name, areas[0])
            if info.get("code") == 0:
                return areas[0], info
            return None

        tasks = [
            asyncio.create_task(self._client.query_summoner(name, area))
            for area in areas
        ]
        try:
            for area, task in zip(areas, tasks, strict=False):
                try:
                    info = await task
                except Exception as exc:
                    logger.debug("FA8 大区 %s 查询失败: %s", area, exc)
                    continue
                if isinstance(info, dict) and info.get("code") == 0:
                    return area, info
            return None
        finally:
            # 命中后剩下的请求不再有意义；不取消会留下悬挂任务。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def query_and_format(self, raw_input: str) -> str:
        """查询召唤师战绩并格式化为消息文本"""
        if not self._config.get("enabled", False):
            return "战绩查询功能未启用，请在 config/plugins/lol_fa8/config.json 中配置"

        name, areas = self._resolve_area(raw_input)
        if not name:
            return (
                "请输入召唤师名称\n"
                "格式: @bot 战绩 召唤师名#编号\n"
                "示例: @bot 战绩 召唤师名#编号\n"
                "指定大区: @bot 战绩 班德尔城 召唤师名#编号\n"
                "按区搜索: @bot 战绩 3 召唤师名#编号 (1-5对应联盟一~五区)"
            )

        is_group = len(areas) > 1
        group_label = ""
        if is_group:
            for alias, g in GROUP_ALIASES.items():
                if SERVER_GROUPS[g] == areas and not alias.startswith("联盟"):
                    group_label = f"联盟{alias}"
                    break

        result = await self._search_summoner(name, areas)
        if result is None:
            if is_group:
                return f"{Msg.ERR} 在{group_label}所有服务器中均未找到该玩家"
            msg_area = SERVERS.get(areas[0], f"大区{areas[0]}")
            return f"{Msg.ERR} 在{msg_area}未找到该玩家"

        area, info = result
        msg = info.get("msg", "")
        if "登录" in str(msg):
            return f"{Msg.ERR} 查询失败: FA8 登录态异常，请联系管理员检查配置"

        puuid = info.get("puuid", "")
        server_name = SERVERS.get(area, f"大区{area}")

        lines = [
            f"LOL 战绩查询 - {server_name}",
            "═══════════════════",
            f"  召唤师: {name}",
            f"  等级: {info.get('level', '?')}",
            f"  最近游戏: {info.get('lastGameDate', '未知')}",
        ]

        hero_count = ""
        skin_count = ""
        skin_html = info.get("skin", "")
        hero_m = _RE_HERO.search(skin_html)
        skin_m = _RE_SKIN.search(skin_html)
        if hero_m:
            hero_count = hero_m.group(1)
        if skin_m:
            skin_count = skin_m.group(1)
        if hero_count or skin_count:
            lines.append(f"  英雄: {hero_count} 个 | 皮肤: {skin_count} 个")

        lines.append("───────────────────")
        lines.append("  段位信息:")

        # 将 FA8 返回的段位字段做一次“清洗”，把 "无"、"?"、"未定级" 等情况统一成“未定级 / 无段位”
        def _normalize_rank_text(text: str) -> str | None:
            if not text:
                return None
            t = str(text).strip()
            if t in ("无", "?", "-", "未定级", "未排位"):
                return None
            return t

        ds_dj_raw = info.get("dsdj", "")
        ds_dj = _normalize_rank_text(ds_dj_raw)
        if ds_dj:
            lines.append(f"    单双排位: {ds_dj} ({info.get('dssf', '')}) 胜点{info.get('dssd', 0)}")
        else:
            lines.append("    单双排位: 未定级 / 无段位")

        lh_dj_raw = info.get("lhdj", "")
        lh_dj = _normalize_rank_text(lh_dj_raw)
        if lh_dj:
            lines.append(f"    灵活排位: {lh_dj} ({info.get('lhsf', '')}) 胜点{info.get('lhsd', 0)}")
        else:
            lines.append("    灵活排位: 未定级 / 无段位")

        rank = info.get("rank", {})
        ds_best_raw = rank.get("dszgdw", "")
        ds_best = _normalize_rank_text(ds_best_raw)
        if ds_best:
            lines.append(f"    单双排位最高: {ds_best}")

        lh_best_raw = rank.get("lhzgdw", "")
        lh_best = _normalize_rank_text(lh_best_raw)
        if lh_best:
            lines.append(f"    灵活排位最高: {lh_best}")

        mastery_html = info.get("mastery", "")
        champions = _parse_mastery(mastery_html)
        if champions:
            lines.append("───────────────────")
            lines.append("  英雄熟练度 TOP5:")
            for i, c in enumerate(champions[:5], 1):
                lines.append(f"    {i}. {c['name']} - {c['level']} ({c['points']:,})")

        if puuid:
            games, current = await asyncio.gather(
                self._client.query_games(puuid, area),
                self._client.query_current_game(puuid, area),
            )

            if games.get("code") == 0:
                win = games.get("win", 0)
                lose = games.get("lose", 0)
                sl = games.get("sl", 0)
                lines.append("───────────────────")
                lines.append(f"  近期战绩: {win}胜 {lose}负 (胜率{sl}%)")

                zj_html = games.get("zj", "")
                matches = _parse_match_cards(zj_html)
                if matches:
                    lines.append("  最近对局:")
                    for m in matches[:5]:
                        icon = "赢" if m["result"] == "胜利" else "输"
                        champ_cn = _champion_cn(m["champion"])

                        mode = m.get("mode") or ""
                        if mode:
                            mode = mode.replace("单排/双排", "单双排位")
                        score = m.get("score") or ""
                        duration = m.get("duration") or ""
                        date = m.get("date") or ""

                        mode_part = f"{mode} " if mode else ""
                        score_part = f" 评分{score}" if score else ""
                        duration_part = f" [{duration}]" if duration else ""
                        date_part = f" {date}" if date else ""

                        lines.append(
                            f"    {icon} {mode_part}{champ_cn} "
                            f"{m['kda']}{score_part}"
                            f"{duration_part}{date_part}"
                        )

            if current.get("code") == 0:
                lines.append("───────────────────")
                lines.append("  ★ 当前正在游戏中!")
                if current.get("mode"):
                    lines.append(f"    模式: {current['mode']}")

        lines.append("═══════════════════")
        return "\n".join(lines)
