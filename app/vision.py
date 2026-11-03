# -*- coding: utf-8 -*-
"""视觉识别：把一张衣服照片变成结构化字段。

设计原则 —— **模型只负责语义，不负责测量。**
  · 颜色：一律本地算（见 colorlab.py）。模型报的色值会被打光和环境带偏，不可复现。
  · 语义：品类 / 正式度 / 厚度 / 温度区间 / 领型 / 版型 / 材质 —— 这些本地小模型做不了，
    只能交给多模态模型。

另外两条不可让步的：
  1. **不信模型的自由输出。** 所有字段过一遍白名单和值域校验，不合法的丢掉并记下来，
     绝不把 "slot": "上衣" 这种值直接写进库。
  2. **任何失败都不阻塞录入。** 网络超时、没配 key、模型胡言乱语 —— 一律返回 None，
     前端退回「品类默认值」模式，用户还是能录。识别是加速器，不是必经环节。
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import catalog
from .engine import rules

# ⚠️ 配置文件放**项目根**（源码），**不放 `data/`**（应用状态）。
#    和 `gate.json` 是同一个道理（见 app/gate.py 顶部那段论证）：
#    `data/` 是**会被重写的那一半**，发布时整个被挪走
#    → 放里面的配置根本上不了云 → 手机上被要求"再配一次 Key"，
#      而电脑上明明已经配好了（**2026-10-01 用户报的就是这个**）。
#    放源码里就跟着每次上传走：电脑配一次，手机自动就有。
#    ⚠️ 代价：源码分享包**必须显式排除它**（里面有明文密钥）——
#       见 scripts/make_source_zip.py。
CONFIG_PATH = Path(__file__).resolve().parent.parent / "llm.json"

# 老路径（2026-10-01 之前）。**只读不写**：读得到就照用，下一次保存自动落到新路径，
# 等于顺手把配置迁过来。留着是为了老机器升级时不丢配置。
LEGACY_CONFIG_PATHS = (Path(__file__).resolve().parent.parent / "data" / "vision.json",)

# 预设：把最难的那几步（base_url / 模型名）替用户填好
#
# DeepSeek 放第一位是有原因的：它的 Flash 线现在是原生多模态，同一个 key 既能看图
# 又能做搭配排序，不用配两个服务商。而且便宜到可以忽略 ——
# 单张图 token 上限 1024，空闲时段输入 1 元/百万，识别一件衣服约 0.2 分钱。
# 注意模型名：`deepseek-flash`（模型版本 DeepSeek-V4.1-Flash）。
# 旧名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 仍能调，但已被官方下线、
# 请求转由最新 Flash 承接 —— 所以别写旧名，也别写带日期的临时版本名（那个已经过期）。
PRESETS = [
    {"id": "deepseek", "label": "DeepSeek（推荐）", "kind": "openai",
     "hint": "模型名填 deepseek-flash。V4.1-Flash 原生支持看图，识别一件约 0.2 分钱；"
             "空闲时段（含整个周末）价格减半。一个 key 同时管识别和搭配排序。"
             "已自动关闭「思考模式」—— 见下面说明。",
     "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
     "no_think": True},
    {"id": "zhipu", "label": "智谱 GLM-4V", "hint": "国内直连，glm-4v-flash 有免费额度",
     "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4v-flash"},
    {"id": "dashscope", "label": "通义千问 VL", "hint": "阿里云百炼，识别中文品类很准",
     "kind": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-vl-max"},
    {"id": "siliconflow", "label": "硅基流动", "hint": "聚合平台，Qwen2.5-VL 便宜",
     "kind": "openai", "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-VL-72B-Instruct"},
    {"id": "moonshot", "label": "月之暗面 Kimi", "hint": "长上下文，图片理解稳定",
     "kind": "openai", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k-vision-preview"},
    {"id": "ark", "label": "火山方舟（豆包）", "hint": "模型名要填你的接入点 ID",
     "kind": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": ""},
    {"id": "openai", "label": "OpenAI", "hint": "需要能访问外网",
     "kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"id": "ollama", "label": "本地 Ollama", "hint": "零成本、不出内网，但要自己装模型",
     "kind": "ollama", "base_url": "http://127.0.0.1:11434", "model": "qwen2.5vl:7b"},
]

DEFAULT_CONFIG = {
    "provider": "",      # 预设 id
    "kind": "openai",    # openai | ollama
    "base_url": "",
    "model": "",
    "api_key": "",
    "timeout": 90,
    "enabled": False,
    # 关掉「思考模式」。DeepSeek 的 thinking 默认打开且 effort=high，
    # 模型会把 token 先花在思维链上；结构化抽取这种任务不需要思考，
    # max_tokens 稍小就会被思维链吃光，content 直接返回空字符串，看起来像"模型没回应"。
    "no_think": False,
}

_FORMALITY_SCALE = "1=居家运动 2=休闲 3=通勤日常 4=商务 5=正装"
_THICKNESS_SCALE = ("1=极薄(吊带/真丝) 2=薄(普通T恤/Polo) 3=中(衬衫/针织/西裤) "
                    "4=厚(毛衣/卫衣/皮衣) 5=极厚(大衣/羽绒服)")

PROMPT = f"""你是服装数据录入助手。看这张单品照片（纯色背景、垂直俯拍），输出一个 JSON 对象。

严格按下面的取值输出，不要自创取值：

slot        层位，只能取：L1(贴身穿的内搭) L2(中间层/毛衣卫衣) L3(外套) L4(下装) L5(鞋) ACC(配饰)
category    品类，必须属于 slot 对应的这一组：
            L1: {"/".join(catalog.CATEGORIES["L1"])}
            L2: {"/".join(catalog.CATEGORIES["L2"])}
            L3: {"/".join(catalog.CATEGORIES["L3"])}
            L4: {"/".join(catalog.CATEGORIES["L4"])}
            L5: {"/".join(catalog.CATEGORIES["L5"])}
            ACC: {"/".join(catalog.CATEGORIES["ACC"])}
name        给这件衣服起的中文短名，8 字以内，形如"米白棉质衬衫"，不要带品牌名
formality   正式度：{_FORMALITY_SCALE}
thickness   厚度：{_THICKNESS_SCALE}
temp_min    适合穿着的最低室外气温，整数摄氏度
temp_max    适合穿着的最高室外气温，整数摄氏度
collar      领型，只能取：{"/".join(catalog.COLLARS)}；下装/鞋/包袋类留空字符串
sleeve      袖型，只能取：{"/".join(catalog.SLEEVES)}；下装/鞋/包袋类留空字符串
length_band 衣长，只能取：{"/".join(catalog.LENGTHS)}；鞋/包袋类留空字符串
fit         版型，只能取：{"/".join(catalog.FITS)}
material    材质，只能取：{"/".join(catalog.MATERIALS)}
pattern     图案，只能取：{"/".join(catalog.PATTERNS)}；纯色就填"纯色"
confidence  你对自己判断的把握，0 到 1 之间的小数

要求：
- 不要判断颜色，颜色由系统另行测量，你不要输出颜色字段。
- 拿不准的字段直接省略，不要瞎猜 —— 省略比猜错有价值。
- 只输出 JSON，不要任何解释文字，不要 markdown 代码围栏。

示例：
{{"slot":"L1","category":"衬衫","name":"白色棉质衬衫","formality":3,"thickness":2,
"temp_min":10,"temp_max":30,"collar":"翻领","sleeve":"长袖","length_band":"常规",
"fit":"合身","material":"棉","pattern":"纯色","confidence":0.92}}"""


# ══════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════

def _config_source() -> Path | None:
    """真正要读的那个文件：**新路径优先**，其次才是老路径。都没有就返回 None。

    ⚠️ 顺序不能反 —— 反了的话，一台机器上只要老文件还在，
    用户在新路径改的配置就永远读不到，而且是静默的（页面上看着一切正常）。
    """
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    for p in LEGACY_CONFIG_PATHS:
        if p.exists():
            return p
    return None


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    src = _config_source()
    if src is not None:
        try:
            cfg.update(json.loads(src.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    # 没填必填项就等于没开
    if not (cfg.get("base_url") and cfg.get("model")):
        cfg["enabled"] = False
    return cfg


def save_config(patch: dict) -> dict:
    """保存配置。

    顺序很重要：**先套预设、再应用 patch**。反过来的话，预设里的值会把用户在界面上
    显式填的内容盖掉（比如手动改过 no_think 又被预设重置回默认）。
    """
    cfg = load_config()

    # 1) 选了预设就补全 base_url / kind / 思考模式开关，省得用户手填
    pid = patch.get("provider", cfg.get("provider"))
    p = next((x for x in PRESETS if x["id"] == pid), None) if pid else None
    if p:
        if not patch.get("base_url"):
            cfg["base_url"] = p["base_url"]
        cfg["kind"] = p["kind"]
        if not (patch.get("model") or cfg.get("model")):
            cfg["model"] = p["model"]
        if "no_think" not in patch:
            cfg["no_think"] = p.get("no_think", False)

    # 2) patch 覆盖一切
    for k in ("provider", "kind", "base_url", "model", "api_key",
              "timeout", "enabled", "no_think"):
        if k in patch and patch[k] is not None:
            cfg[k] = patch[k]
    cfg["timeout"] = int(cfg.get("timeout") or 90)

    cfg["base_url"] = str(cfg.get("base_url") or "").rstrip("/")
    if not (cfg.get("base_url") and cfg.get("model")):
        cfg["enabled"] = False

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def public_config() -> dict:
    """给前端的版本：不回传 api_key 明文，只说有没有。"""
    cfg = load_config()
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured": bool(cfg.get("base_url") and cfg.get("model")),
        "provider": cfg.get("provider", ""),
        "kind": cfg.get("kind", "openai"),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "has_key": bool(cfg.get("api_key")),
        "timeout": cfg.get("timeout", 90),
        "no_think": bool(cfg.get("no_think")),
        "presets": PRESETS,
    }


# ══════════════════════════════════════════════
# 调用
# ══════════════════════════════════════════════

def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _extract_json(text: str) -> dict:
    """从模型回复里抠出 JSON。会处理 ```json 围栏和前后废话。"""
    if not text:
        raise ValueError("模型返回为空")
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError("回复里找不到 JSON")


def _call_model(image_path: Path, cfg: dict) -> dict:
    img_b64 = _b64(image_path)
    timeout = int(cfg.get("timeout") or 90)

    if cfg.get("kind") == "ollama":
        url = cfg["base_url"] + "/api/chat"
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": PROMPT, "images": [img_b64]}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        data = _post_json(url, payload, {}, timeout)
        return _extract_json((data.get("message") or {}).get("content", ""))

    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        # 给足余量：即使某个服务商默认开了思考模式，也别让它把回答挤没
        "max_tokens": 1600,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + img_b64}},
            ]},
        ],
    }
    # 只对声明了 no_think 的预设加这个字段 —— 它是 DeepSeek 专有的顶层参数，
    # 别的服务商收到未知字段可能直接 400。
    if cfg.get("no_think"):
        payload["thinking"] = {"type": "disabled"}

    headers = {}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    data = _post_json(url, payload, headers, timeout)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("模型没返回 choices：" + json.dumps(data, ensure_ascii=False)[:200])

    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if text:
        return _extract_json(text)

    # content 是空的。最常见的原因是思考模式把 token 花光了（思维链在
    # reasoning_content 里，content 为空串）。先试着从思维链里抢救，
    # 抢救不到再报一个能看懂的错误。
    reasoning = msg.get("reasoning_content") or ""
    if reasoning:
        try:
            return _extract_json(reasoning)
        except ValueError:
            pass
    finish = choices[0].get("finish_reason")
    raise ValueError(
        "模型返回内容为空"
        + (f"（finish_reason={finish}，多半是思考模式吃光了 max_tokens）" if finish else "")
        + "。如果是 DeepSeek，确认模型名是 deepseek-flash 且已关闭思考模式。"
    )


# ══════════════════════════════════════════════
# 校验（这里是重点：宁缺勿滥）
# ══════════════════════════════════════════════

def _clean_enum(value, allowed: list[str], allow_empty: bool = True) -> str | None:
    if value is None:
        return "" if allow_empty else None
    v = str(value).strip()
    if v in allowed:
        return v
    if allow_empty and v in ("", "-", "无", "不适用", "None", "null"):
        return ""
    return None


def _clean_int(value, lo: int, hi: int) -> int | None:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if lo <= n <= hi:
        return n
    return None


def validate(raw: dict, fallback_slot: str | None = None) -> dict:
    """把模型输出规整成可以入库的字段。不合法的字段直接丢掉并记在 _dropped 里。"""
    out: dict = {}
    dropped: list[str] = []

    slot = str(raw.get("slot") or "").strip().upper()
    if slot not in catalog.SLOT_NAMES:
        # 模型有时给"上衣""外套"这种词，先从品类反推槽位
        cat_hint = catalog.nearest_category(raw.get("category"))
        if cat_hint:
            slot = next(s for s, cats in catalog.CATEGORIES.items() if cat_hint in cats)
        elif fallback_slot:
            slot = fallback_slot
        else:
            dropped.append("slot")
            slot = "L1"
    out["slot"] = slot

    category = catalog.nearest_category(raw.get("category"))
    out["category"] = catalog.category_of(slot, category)
    if category is None:
        dropped.append("category")

    base = catalog.defaults_for(slot, out["category"])

    n = str(raw.get("name") or "").strip()
    n = re.sub(r"\s+", " ", n)[:20]
    out["name"] = n

    for field, lo, hi in (("formality", 1, 5), ("thickness", 1, 5)):
        v = _clean_int(raw.get(field), lo, hi)
        if v is None:
            if field in raw:
                dropped.append(field)
            v = base[field]
        out[field] = v

    tmin = _clean_int(raw.get("temp_min"), -30, 45)
    tmax = _clean_int(raw.get("temp_max"), -30, 45)
    if tmin is None or tmax is None:
        if "temp_min" in raw or "temp_max" in raw:
            dropped.append("temp")
        tmin, tmax = base["temp_min"], base["temp_max"]
    if tmin > tmax:
        tmin, tmax = tmax, tmin
    if tmax - tmin < 8:          # 区间太窄会让这件衣服几乎进不了任何搭配
        mid = (tmin + tmax) / 2
        tmin, tmax = int(mid - 6), int(mid + 6)
    out["temp_min"], out["temp_max"] = tmin, tmax

    for field, allowed in (("collar", catalog.COLLARS), ("sleeve", catalog.SLEEVES),
                           ("length_band", catalog.LENGTHS), ("fit", catalog.FITS),
                           ("material", catalog.MATERIALS), ("pattern", catalog.PATTERNS)):
        v = _clean_enum(raw.get(field), allowed)
        if v is None:
            if field in raw:
                dropped.append(field)
            v = base.get(field, "")
        out[field] = v

    # 槽位决定哪些字段根本没意义。模型经常给裤子编一个"翻领"，
    # 而 D2/D3/D6 这些叠穿规则会拿它去比对 —— 必须清掉，否则规则被噪声触发。
    if slot in ("L4", "L5", "ACC"):
        out["collar"] = ""
        out["sleeve"] = ""
    if slot in ("L5", "ACC"):
        out["length_band"] = ""

    conf = raw.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = 0.6
    if dropped:
        conf = min(conf, 0.55)

    out["_confidence"] = round(conf, 2)
    out["_dropped"] = dropped
    return out


# ══════════════════════════════════════════════
# 对外
# ══════════════════════════════════════════════

def available() -> tuple[bool, str]:
    cfg = load_config()
    if not cfg.get("enabled"):
        return False, "未配置视觉模型"
    return True, ""


def analyze(image_path: Path, fallback_slot: str | None = None) -> dict:
    """识别一张图。永远不抛异常 —— 失败就返回 {"ok": False, "error": ...}。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "未配置视觉模型"}

    if not image_path.exists():
        return {"ok": False, "error": "图片不存在"}

    try:
        raw = _call_model(image_path, cfg)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        hint = ""
        if e.code in (401, 403):
            hint = "（API Key 不对或没权限）"
        elif e.code == 404:
            hint = "（模型名或接口地址不对）"
        elif e.code == 429:
            hint = "（调用太频繁或额度用完了）"
        elif "does not support image" in detail.lower():
            hint = "（这个模型不支持图片输入 —— DeepSeek 请把模型名填 deepseek-flash）"
        return {"ok": False, "error": f"HTTP {e.code}{hint} {detail}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"连不上：{e.reason}（本地 Ollama 没启动？）"}
    except (ValueError, KeyError) as e:
        msg = str(e)
        if msg.startswith(("模型返回内容为空", "模型没返回 choices")):
            return {"ok": False, "error": msg}
        return {"ok": False, "error": f"返回内容解析失败：{msg}"}
    except TimeoutError:
        return {"ok": False, "error": "超时"}
    except Exception as e:  # 任何意外都不该让录入中断
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not isinstance(raw, dict):
        return {"ok": False, "error": "模型返回的不是 JSON 对象"}

    fields = validate(raw, fallback_slot)
    fields.update({"ok": True, "error": "", "model": cfg.get("model", "")})
    return fields


def test_connection() -> dict:
    """用一张现造的图测连通性，不依赖任何用户数据。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "还没填完（base_url / 模型名必填）"}

    tmp = CONFIG_PATH.parent / "_vision_test.jpg"
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (256, 256), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([80, 60, 176, 220], fill=(240, 240, 235), outline=(200, 200, 195))
        img.save(tmp, "JPEG", quality=85)
        res = analyze(tmp)
        if res.get("ok"):
            return {"ok": True, "error": "", "sample": res}
        return {"ok": False, "error": res.get("error", "未知错误")}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ══════════════════════════════════════════════
# 搭配审美排序（纯文字，不需要图片）
# ══════════════════════════════════════════════

# 这条 prompt 是「审美」真正起作用的地方。
#
# 2026-09-21 按用户反馈（"大模型审美有点没跟上"）重写过一次，两处改动：
#   ① 旧版只说"考虑色系统一、比例轮廓、正式度"—— 那是**形容词**，模型拿到手
#      只会回一句"搭配协调"这种谁都能说的话。现在换成**可以照着比的具体条目**，
#      而且排了先后（轮廓 → 明度 → 色彩 → 材质 → 正式度 → 呼应）：前一条分出了
#      胜负就不用看后面的，避免模型在"两个小优点"和"一个大缺点"之间瞎权衡。
#   ② 补了一份**反面清单**。只告诉模型"什么好"不够，它需要一个否定的把手 ——
#      "上下都宽松""厚薄断层""正式度割裂"这些是真人一眼能看出来的毛病。
#
# 顺序不是随便排的：轮廓（显不显肿）排在颜色前面，因为隔着三米看，
# 衣服的**形状**比颜色先被看见。
RANK_PROMPT = """你是资深造型师。下面是从这个人**自己的衣橱**里，用硬规则算出来的几套合法搭配
（物理上一定穿得出去：温度合适、场合不错位、颜色不打架）。你唯一的任务是**排序**：
哪一套最该穿出去。

铁律（比审美更优先）：
- 只能在这些候选里挑，**绝对不要提到任何不在候选里的单品**。
- 不要因为「缺某件」就否定一套 —— 他不会去买衣服，他就是要从现有的里面穿。
- 不要复述衣服名字，不要客套话，不要 markdown。

按下面前后顺序比，前面分出了胜负就不用再看后面：
1. **轮廓**：上下不要同时宽松（整个人是方的、显肿），也不要同时紧身。
   有外套时，外套敞开露出的内搭要自成一套 —— 领口有层次、下摆不堆、腰线别丢。
2. **明度**：全身都是中性色时，必须要有明暗差（深—中—浅）。
   一个深浅穿到底最平庸。上深下浅、上浅下深都成立。
3. **色彩**：彩色（非中性色）最多两个，最好一个当主角、一个做点缀。
   同色系深浅比撞色安全；真要撞，撞得干净也算好。
4. **材质与厚薄**：上软下硬、上硬下软都成立；**上下一样厚薄会显得钝**。
   厚重外套压轻薄内搭是加分；薄外套压厚毛衣是减分。
5. **正式度**：和场合贴不贴。宁可整体略高一档，不要上下割裂。
6. **呼应**：内搭颜色和鞋呼应、鞋和包同色系，都算小加分。

反面清单（命中任意一条就往后排）：
- 上下都宽松，整个人是方的
- 三个以上彩色堆在身上
- 图案打架（格纹配条纹、印花配印花）
- 外套盖着时好看、一敞开就散架
- 厚薄断层：上半身过冬、下半身过夏
- 正式度割裂：正装上衣配运动鞋、正装裤配休闲上衣

严格输出 JSON，不要任何解释文字：
{"order": [最好的序号, 次好的序号, ...], "reason": "第一套好在哪，40 字以内"}

`reason` 必须说具体的 —— 轮廓、明度、材质、呼应里的哪一条。**不许写「搭配协调」
「很合适」「很有感觉」这种谁都能说的空话**，那是没看的意思。
序号从 1 开始，每套都要出现一次。示例：
{"order":[2,1,3],"reason":"外套压住浅色内搭，上深下浅有层次，裤脚利落，通勤最稳。"}"""


def _lightness(hex_str) -> str:
    """把像素色折成一个"看着多亮"的档位。

    判「明度差」这件事，文字模型看 #1C1C1A / #FEFEFE 还行，看 #6B7280 就悬了。
    本地先算好档位再喂过去，比让它自己解析十六进制可靠 —— 颜色本来就是本地的活
    （见模块顶部：模型判语义，本地测颜色）。
    """
    hsv = rules.hsv_from_hex(hex_str)
    if not hsv:
        return ""
    v = hsv[2]                      # 取 V（最大通道）而不是明度公式：
    if v >= 0.85:                   # 中性灰的 V 就是它的灰阶，正是判明暗差要的东西
        return "浅亮"
    if v >= 0.55:
        return "中等"
    if v >= 0.30:
        return "偏深"
    return "深暗"


# 喂给模型的字段。**多给几个真的会影响审美的**，不能只给名字和颜色 ——
# 「轮廓 / 厚薄 / 图案」这几条判断全靠它们，而衣服名字里通常读不出来。
_DESC_KEYS = (("material", "材质"), ("pattern", "图案"), ("thickness", "厚薄"),
              ("collar", "领"), ("sleeve", "袖"), ("length_band", "衣长"),
              ("fit", "版型"), ("formality", "正式度"))


def _describe(outfit: dict, idx: int, temp: float, occasion: str | None) -> str:
    items = outfit.get("items") or []
    lines = []
    for it in items:
        bits = [it.get("name") or "?"]
        if it.get("color_main"):
            bits.append(it["color_main"] + ("（中性色）" if it.get("is_neutral") else ""))
        lv = _lightness(it.get("color_hex"))
        if lv:
            bits.append("明度" + lv)
        for k, label in _DESC_KEYS:
            v = it.get(k)
            if v not in (None, "", 0):
                bits.append(f"{label}{v}")
        if it.get("is_signature"):
            bits.append("本命件")
        slot = it.get("slot")
        name = catalog.SLOT_NAMES.get(slot, slot)
        lines.append(f"  {name}（{slot}）：{' · '.join(bits)}")
    head = f"第 {idx} 套（{int(temp)}℃"
    if occasion:
        head += f" · {occasion}"
    head += f"，共 {len(items)} 件）"
    return head + "\n" + "\n".join(lines)


def rank_outfits(outfits: list[dict], temp: float,
                 occasion: str | None = None) -> dict:
    """让模型对候选搭配排序，**就地**调整 outfits 的顺序和理由。

    返回 `{"used": bool, "error": str}`。失败时保留本地排序 —— 排序是增强，
    不是必经环节；但**失败必须看得见**：`error` 会一路传到页面上，
    而不是假装"本地排序也挺好"。

    ⚠️ 这里踩过一个坑，正是这个项目第七次同类问题（默认值在背后生效 → 静默失败）：
    **请求体里没关 DeepSeek 的思考模式。** 它的 thinking 默认开启，模型先把 token
    花在思维链上，`content` 直接返回空串，然后被下面那个 `except` 吞掉 ——
    于是"模型排过序"这个标签**一直是假的**，页面看起来一切正常，用户只觉得
    "大模型审美没跟上"。隔壁 `summarize_plan` 关掉了，这里漏了。
    **凡是往 DeepSeek 发请求的地方，都得带 no_think。**
    """
    cfg = load_config()
    if not cfg.get("enabled") or len(outfits) < 2:
        return {"used": False, "error": ""}

    body = "\n\n".join(_describe(o, i + 1, temp, occasion)
                       for i, o in enumerate(outfits))
    prompt = f"{RANK_PROMPT}\n\n候选（共 {len(outfits)} 套）：\n\n{body}"

    try:
        if cfg.get("kind") == "ollama":
            data = _post_json(cfg["base_url"] + "/api/chat", {
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0},
            }, {}, int(cfg.get("timeout") or 90))
            text = str((data.get("message") or {}).get("content") or "").strip()
        else:
            headers = {}
            if cfg.get("api_key"):
                headers["Authorization"] = "Bearer " + cfg["api_key"]
            payload = {
                "model": cfg["model"], "temperature": 0, "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            }
            # 不关思考模式 → 思维链吃光 max_tokens → 正文是空串。
            # 只对声明了 no_think 的预设发这个字段（别的服务商收到未知字段可能 400）。
            if cfg.get("no_think"):
                payload["thinking"] = {"type": "disabled"}
            data = _post_json(cfg["base_url"] + "/chat/completions",
                              payload, headers, int(cfg.get("timeout") or 90))
            text = str(((data.get("choices") or [{}])[0].get("message") or {})
                       .get("content") or "").strip()

        if not text:
            # 这条报错是刻意写清楚的：以后谁再忘关思考模式，一眼就能认出原因，
            # 不用再去翻请求体。
            return {"used": False,
                    "error": "模型返回了空正文（思考模式没关时，思维链会把 "
                             "max_tokens 吃光，正文就是空的）"}
        raw = _extract_json(text)
    except Exception as e:
        return {"used": False, "error": f"{type(e).__name__}: {e}"[:180]}

    if not isinstance(raw, dict) or not isinstance(raw.get("order"), list):
        return {"used": False, "error": "模型没按格式返回 order"}
    seq = []
    for x in raw["order"]:
        try:
            i = int(x) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(outfits) and i not in seq:
            seq.append(i)
    if not seq:
        return {"used": False, "error": "模型给的序号都不在范围内"}
    # 模型漏掉的补在最后，保证一套都不丢
    seq += [i for i in range(len(outfits)) if i not in seq]

    reason = str(raw.get("reason") or "").strip()[:60]
    reordered = []
    for rank, i in enumerate(seq):
        o = outfits[i]
        if rank == 0 and reason:
            o = dict(o)
            o["llm_reason"] = reason
        reordered.append(o)
    outfits[:] = reordered
    return {"used": True, "error": ""}


# ══════════════════════════════════════════════
# 多天计划的总结（一周 / 旅行）
# ══════════════════════════════════════════════

PLAN_PROMPT = """你是打包顾问。下面是从这个人**自己的衣橱**排出来的多天穿搭表，
里面每一件都是他真实拥有的。你不需要、也不许建议他买任何东西。

写一段话（120 字以内），告诉他这趟怎么带最省地方、最该注意什么。
要求：
- 只讲穿搭与打包，不要复述表格，不要客套话，不要 markdown，不要换行。
- **只许提下面列出的这些衣服。** 这是硬规矩：不许凭常识补一件表里没有的
  （比如"记得带件外套"）—— 打包清单里没有，就是他这趟不会带，
  你一提，用户就会觉得推荐和清单对不上。表里没有的层，宁可不说。
- 有具体依据再说（温差、下雨、哪几件在复用），没有就直说"照表带就行"。
- 直接说结论，不要"首先/其次"。"""


def summarize_plan(day_lines: list[str], rows: list[dict], mode: str) -> str:
    """给多天计划写一段人话总结。**失败一律返回空串** —— 它是锦上添花，不是必经环节。"""
    cfg = load_config()
    if not cfg.get("enabled") or not day_lines:
        return ""

    packing = "\n".join(
        f"- {r.get('slot') or ''} {r.get('name') or '?'}"
        f"（{r.get('wear_count') or 0} 天）" for r in rows) or "-（空）"
    kind = "一趟旅行" if mode == "trip" else "一周"
    prompt = (f"{PLAN_PROMPT}\n\n这是{kind}的安排：\n\n"
              + "\n".join(day_lines)
              + f"\n\n打包清单：\n{packing}")

    try:
        if cfg.get("kind") == "ollama":
            data = _post_json(cfg["base_url"] + "/api/chat", {
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                "stream": False, "options": {"temperature": 0.3},
            }, {}, int(cfg.get("timeout") or 90))
            text = (data.get("message") or {}).get("content", "")
        else:
            headers = {}
            if cfg.get("api_key"):
                headers["Authorization"] = "Bearer " + cfg["api_key"]
            payload = {
                "model": cfg["model"], "temperature": 0.3, "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }
            # 和识别一样：DeepSeek 不关思考模式的话，正文可能被思维链挤成空串
            if cfg.get("no_think"):
                payload["thinking"] = {"type": "disabled"}
            data = _post_json(cfg["base_url"] + "/chat/completions",
                              payload, headers, int(cfg.get("timeout") or 90))
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            text = msg.get("content") or msg.get("reasoning_content") or ""
    except Exception:
        return ""

    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:180]
