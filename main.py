"""大肥鱼相册：她记住你发的照片，聊到相关话题时会主动发出来。

分两步：
1. 收图 —— 你给她发图片时，存进她的相册，并记下当时聊天的关键词做标签
2. 发图 —— 她回复时，如果你的话或她的话命中了相册里某张图的标签，
   就有一定概率把那张图一起发出来（有日限和重复冷却，宁可不发）

设计取向：话题不沾边就绝不动用相册，避免"乱发图"变成机械行为。
"""

import asyncio
import hashlib
import json
import os
import random
import re
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig

PLUGIN_NAME = "dafeiyu_photo"
DATA_DIR = f"/AstrBot/data/plugin_data/{PLUGIN_NAME}"
ALBUM_DIR = os.path.join(DATA_DIR, "album")
ALBUM_FILE = os.path.join(DATA_DIR, "album.json")

HORMONE_FILE = "/AstrBot/data/plugin_data/dafeiyu_hormone/hormones.json"


def _mood_gain() -> float:
    """心情影响发图意愿：烦躁压力高就不太想发，亲近高涨一点。"""
    try:
        with open(HORMONE_FILE, encoding="utf-8") as f:
            h = json.load(f)
        cort = float(h.get("cort", 0.2))
        oxy = float(h.get("oxy", 0.5))
        dopa = float(h.get("dopa", 0.4))
        gain = 1.0
        if cort >= 0.45:
            gain *= 0.25
        elif cort >= 0.32:
            gain *= 0.6
        if oxy >= 0.62:
            gain *= 1.35
        if dopa >= 0.55:
            gain *= 1.15
        return max(0.1, min(1.8, gain))
    except Exception:
        return 1.0


STOP = set("的了在是我你她他它们这那就也不都个有一和与或说会要可以吗呢吧啊哦嗯很太真")


def bigrams(text: str) -> set[str]:
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1) if t[i] not in STOP}


class PhotoAlbum(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        os.makedirs(ALBUM_DIR, exist_ok=True)
        self.album = self._load()

    # ------------------------------------------------------------ 配置/存储

    def _c(self, key, default):
        try:
            v = self.cfg.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _load(self) -> dict:
        try:
            with open(ALBUM_FILE, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("items", [])
            d.setdefault("state", {"day": "", "count": 0})
            return d
        except Exception:
            return {"items": [], "state": {"day": "", "count": 0}}

    def _save(self) -> None:
        try:
            tmp = ALBUM_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.album, f, ensure_ascii=False, indent=1)
            os.replace(tmp, ALBUM_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 保存相册失败：{exc!r}")

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        raw = str(self._c("scopes", "") or "").strip()
        if not raw:
            return True
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return any(s.strip() and s.strip() in umo for s in raw.split(","))

    # ------------------------------------------------------------ 收图

    async def _fetch_bytes(self, url_or_path: str) -> bytes | None:
        try:
            if url_or_path.startswith(("http://", "https://")):
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url_or_path, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status != 200:
                            return None
                        return await r.read()
            if os.path.exists(url_or_path):
                with open(url_or_path, "rb") as f:
                    return f.read()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[{PLUGIN_NAME}] 取图失败：{exc!r}")
        return None

    def _remember(self, data: bytes, tags: set[str], source: str) -> str | None:
        if not data:
            return None
        h = hashlib.sha1(data).hexdigest()[:16]
        for it in self.album["items"]:
            if it.get("hash") == h:
                # 已经存过，只补标签
                it["tags"] = sorted(set(it.get("tags") or []) | tags)
                self._save()
                return it["file"]
        ext = ".jpg"
        head = data[:4]
        if head[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            ext = ".gif"
        elif head[:4] == b"RIFF":
            ext = ".webp"
        name = f"{h}{ext}"
        path = os.path.join(ALBUM_DIR, name)
        try:
            with open(path, "wb") as f:
                f.write(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 存图失败：{exc!r}")
            return None
        self.album["items"].append({
            "file": name, "hash": h, "tags": sorted(tags),
            "source": source[:120], "added": time.time(), "last_sent": 0, "sent_count": 0,
        })
        self._save()
        logger.info(f"[{PLUGIN_NAME}] 收进相册 {name}，标签 {sorted(tags)[:8]}")
        return name

    # ------------------------------------------------------------ 发图判断

    def _pick(self, texts: list[str]) -> dict | None:
        if not self.album["items"]:
            return None
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        st = self.album["state"]
        if st.get("day") != today:
            st["day"] = today
            st["count"] = 0
        if int(st.get("count") or 0) >= int(self._c("max_per_day", 2)):
            return None

        grams = set()
        for t in texts:
            grams |= bigrams(t)
        if not grams:
            return None

        min_score = int(self._c("min_score", 2))
        cd = float(self._c("cooldown_days", 7)) * 86400
        best, best_score = None, 0
        for it in self.album["items"]:
            if now - float(it.get("last_sent") or 0) < cd:
                continue
            score = len(grams & set(it.get("tags") or []))
            if score > best_score:
                best, best_score = it, score
        if best is None or best_score < min_score:
            return None
        prob = float(self._c("prob", 0.65)) * _mood_gain()
        if random.random() > max(0.02, min(0.95, prob)):
            return None
        return best

    def _mark_sent(self, item: dict) -> None:
        item["last_sent"] = time.time()
        item["sent_count"] = int(item.get("sent_count") or 0) + 1
        st = self.album["state"]
        st["day"] = time.strftime("%Y-%m-%d", time.localtime())
        st["count"] = int(st.get("count") or 0) + 1
        self._save()

    # ------------------------------------------------------------ 钩子

    @filter.on_llm_request(priority=85000)
    async def remember_photos(self, event: AstrMessageEvent, req) -> None:
        """你发来的图片存进相册，并把她看到的上下文当标签。"""
        try:
            if not self._c("enable", False) or not self._c("ingest", True):
                return
            msg = getattr(event, "message_obj", None)
            comps = list(getattr(msg, "message", []) or [])
            if not comps:
                return
            text = " ".join(c.text for c in comps
                            if isinstance(c, Plain) and getattr(c, "text", ""))
            tags = bigrams(text)
            for comp in comps:
                if not isinstance(comp, Image):
                    continue
                url = getattr(comp, "url", None) or getattr(comp, "file", None) or ""
                if not url:
                    continue
                data = await self._fetch_bytes(str(url))
                if data and len(data) > 1024:
                    await asyncio.to_thread(self._remember, data, tags, text or str(url))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 收图异常已忽略：{exc!r}")

    @filter.on_decorating_result(priority=85000)
    async def maybe_send_photo(self, event: AstrMessageEvent) -> None:
        """她回复时，如果话题命中相册，追加一张图。"""
        try:
            if not self._c("enable", False) or not self._scope_ok(event):
                return
            result = event.get_result()
            chain = getattr(result, "chain", None) if result else None
            if not chain:
                return
            her = " ".join(c.text for c in chain
                           if isinstance(c, Plain) and getattr(c, "text", ""))
            if her.strip().startswith(("/", "／")):
                return
            mine = ""
            msg = getattr(event, "message_obj", None)
            for c in (getattr(msg, "message", []) or []):
                if isinstance(c, Plain) and getattr(c, "text", ""):
                    mine += c.text
            if str(mine).strip().startswith(("/", "／")):
                return
            item = self._pick([her, mine])
            if not item:
                return
            path = os.path.join(ALBUM_DIR, item["file"])
            if not os.path.exists(path):
                return
            try:
                comp = Image.fromFileSystem(path)
            except Exception:
                comp = Image(file=path)
            chain.append(comp)
            result.chain = chain
            self._mark_sent(item)
            logger.info(f"[{PLUGIN_NAME}] 发了相册里的 {item['file']}（标签命中）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 发图异常已忽略：{exc!r}")

    # ------------------------------------------------------------ 指令

    @filter.command("相册")
    async def cmd_album(self, event: AstrMessageEvent):
        """看她的相册。用法：/相册"""
        items = self.album.get("items") or []
        if not items:
            yield event.plain_result("相册还是空的，你发几张图给我，我记下来。")
            return
        lines = [f"相册里 {len(items)} 张："]
        for i, it in enumerate(items[:20], 1):
            tags = "、".join((it.get("tags") or [])[:5]) or "（没标签）"
            lines.append(f"{i}. {tags} · 发过 {it.get('sent_count', 0)} 次")
        lines.append("用 /相册发 序号 让她当场发一张")
        yield event.plain_result("\n".join(lines))

    @filter.command("相册发")
    async def cmd_send(self, event: AstrMessageEvent, index: str = "1"):
        """当场发一张。用法：/相册发 2"""
        items = self.album.get("items") or []
        try:
            n = int(str(index).strip()) - 1
        except Exception:
            n = 0
        if not items:
            yield event.plain_result("相册是空的。")
            return
        if n < 0 or n >= len(items):
            yield event.plain_result(f"只有 {len(items)} 张。")
            return
        it = items[n]
        path = os.path.join(ALBUM_DIR, it["file"])
        if not os.path.exists(path):
            yield event.plain_result("这张图不见了。")
            return
        self._mark_sent(it)
        yield event.chain_result([Image.fromFileSystem(path)])

    @filter.command("相册重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        """清掉冷却和当日计数，方便测试。用法：/相册重置"""
        for it in self.album.get("items") or []:
            it["last_sent"] = 0
        self.album["state"] = {"day": "", "count": 0}
        self._save()
        yield event.plain_result("冷却和当日计数已清空。")

    @filter.command("相册忘了")
    async def cmd_forget(self, event: AstrMessageEvent, index: str = "1"):
        """从相册里删掉一张。用法：/相册忘了 2"""
        items = self.album.get("items") or []
        try:
            n = int(str(index).strip()) - 1
        except Exception:
            yield event.plain_result("用法：/相册忘了 2")
            return
        if n < 0 or n >= len(items):
            yield event.plain_result(f"只有 {len(items)} 张。")
            return
        it = items.pop(n)
        try:
            p = os.path.join(ALBUM_DIR, it["file"])
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        self._save()
        yield event.plain_result(f"忘了第 {n + 1} 张。")

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        # 预置照片
        try:
            raw = str(self._c("seed", "[]") or "").strip()
            seeds = json.loads(raw) if raw.startswith("[") else []
        except Exception:
            seeds = []
        for s in seeds:
            try:
                name = str(s.get("file") or "")
                tags = set(s.get("tags") or []) | bigrams(" ".join(s.get("tags") or []))
                if not name:
                    continue
                cands = [name, os.path.join("/AstrBot/data/attachments", name)]
                if not os.path.isabs(name):
                    cands.insert(1, os.path.join(ALBUM_DIR, name))
                for c in cands:
                    if os.path.exists(c):
                        with open(c, "rb") as f:
                            data = f.read()
                        self._remember(data, tags, "seed:" + name)
                        break
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 预置失败：{exc!r}")
        logger.info(f"[{PLUGIN_NAME}] 相册就绪，{len(self.album['items'])} 张，目录 {ALBUM_DIR}")

    async def terminate(self) -> None:
        self._save()
