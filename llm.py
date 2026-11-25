# -*- coding: utf-8 -*-
"""
QuantDesk · LLM 层

提供两处能力，共用同一套端点配置与密钥（data/llm.json）：
1. 选股重排：只做「候选重排 + 用因子说话的解读」，不做买卖决策、不读账户。
2. 复盘 AI：由 emtai 调用，生成复盘日记 / 行为体检；发送账本聚合数据，见 PRIVACY_REVIEW。

边界：
- 默认关闭：未配置 / 未启用时用本地规则模板，明确标注 local，绝不伪装成 LLM
- 启用后走用户自己填写的 OpenAI 兼容 /chat/completions 端点
- 选股重排只发送候选代码、名称、模型分与因子数值，不含账户、持仓、成交
- 复盘 AI 仅在用户把对应开关切到 AI 时发送账本聚合数据，不含账号、密码、流水号
- 选股返回结果做白名单校验：模型编造的代码一律丢弃，理由截断，分数钳制
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "data" / "llm.json"

DEFAULT = {"enabled": False, "base_url": "", "api_key": "", "model": "",
           "temperature": 0.2, "timeout": 25, "max_tokens": 0,
           # 空头辩护：默认关。开启前应先用历史 picks 做 A/B，确认否决组确实更差。
           "bear_enabled": False, "bear_model": "", "judge_model": "", "bear_min": 3}
# max_tokens=0 表示「按候选数量自动算预算」：
# 推理型模型（deepseek-flash / reasoner 等）会先把预算烧在 reasoning_tokens 上，
# 给固定 900 实测会导致 content 为空（finish_reason=length），所以默认自动放大。

PRIVACY = ("仅在「已启用且点重排」时才向下方填写的端点发送候选代码/名称/模型分/因子数值；"
           "不发送账户、持仓、成交记录或自选股来源。密钥只保存在本机 data/llm.json。")

PRIVACY_REVIEW = ("把「复盘日记 / 行为体检」切到 AI 并生成时，会向同一端点发送账本聚合数据："
                  "总资产、当日与累计盈亏、仓位占比、成交统计、持仓与已平仓股票的名称/代码。"
                  "不发送资金账号、密码、委托编号、成交编号、自选股来源与完整的资金流水明细；"
                  "密钥只保存在本机 data/llm.json。若不想出网，保持「本地规则」模式即可。")


def _cfg() -> dict:
    c = dict(DEFAULT)
    if CONFIG_PATH.exists():
        try:
            c.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    return c


def _save(c: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(k: str) -> str:
    if not k:
        return ""
    return k[:3] + "*" * max(4, min(len(k) - 5, 12)) + k[-2:]


def configured(c: dict | None = None) -> bool:
    c = c or _cfg()
    return bool(c.get("base_url") and c.get("api_key") and c.get("model"))


def status() -> dict:
    c = _cfg()
    return {"ok": True, "enabled": bool(c.get("enabled")),
            "configured": configured(c), "base_url": c.get("base_url", ""),
            "model": c.get("model", ""), "api_key_masked": _mask(c.get("api_key", "")),
            "temperature": c.get("temperature", 0.2), "timeout": c.get("timeout", 25),
            "max_tokens": c.get("max_tokens", 0),
            "bear_enabled": bool(c.get("bear_enabled")),
            "bear_model": c.get("bear_model", ""),
            "judge_model": c.get("judge_model", ""),
            "bear_min": int(c.get("bear_min") or 3),
            "privacy": PRIVACY,
            "mode_default": "local（本地规则解读，未调用大模型）",
            "note": "支持任何 OpenAI 兼容端点（自建 / 云厂商 / 本地 Ollama 的兼容层），由用户自行填写。"}


def configure(body: dict) -> dict:
    c = _cfg()
    if body.get("clear_key"):
        c["api_key"] = ""
    for k in ("enabled", "base_url", "model", "temperature", "timeout", "max_tokens"):
        if k in body and body[k] is not None:
            c[k] = body[k]
    if "bear_enabled" in body:
        c["bear_enabled"] = bool(body["bear_enabled"])
    for k in ("bear_model", "judge_model"):
        if k in body:
            c[k] = str(body[k] or "").strip()
    if "bear_min" in body:
        try:
            c["bear_min"] = max(1, min(int(body["bear_min"] or 3), 6))
        except Exception:
            c["bear_min"] = 3
    if "api_key" in body and not body.get("clear_key"):
        v = str(body["api_key"] or "").strip()
        if v and v != c["api_key"]:  # 前端回显的是打码值，不做覆盖
            c["api_key"] = v
    c["enabled"] = bool(c["enabled"])
    c["base_url"] = str(c["base_url"] or "").strip().rstrip("/")
    c["model"] = str(c["model"] or "").strip()
    c["temperature"] = max(0.0, min(float(c["temperature"] or 0.2), 1.0))
    c["timeout"] = max(5, min(int(c["timeout"] or 25), 180))
    try:
        c["max_tokens"] = max(0, min(int(float(c.get("max_tokens") or 0)), 8000))
    except Exception:
        c["max_tokens"] = 0
    _save(c)
    return {**status(), "ok": True, "message": "配置已保存到本机"}


def _budget(n_items: int, cfg_tokens: int = 0) -> int:
    """输出预算：用户显式填了就用用户的，否则按条目数估算。

    推理型模型的 reasoning_tokens 与 content 共用同一个 max_tokens 池，
    实测 10 只候选在 deepseek-flash 上要 ~1200 推理 + ~350 输出，
    留足余量：每条候选 180 tokens，另加 1200 的推理底座。
    """
    if cfg_tokens and cfg_tokens >= 200:
        return int(min(cfg_tokens, 8000))
    n = max(1, int(n_items))
    return int(min(1200 + n * 180, 6000))


def _chat(messages: list[dict], c: dict, max_tokens: int = 900,
          timeout: int | None = None) -> dict:
    """底层调用。timeout 是**读**超时（秒），连接超时单独给 8 秒。

    配置里的 timeout=25 是给「重排」这种小任务用的；空头辩护要为一整批候选
    写理由，推理型模型光思考就可能 40 秒以上，必须由调用方给更长的预算，
    否则 requests 会先抛 ReadTimeout，看起来像「接口挂了」。
    """
    url = c["base_url"].rstrip("/") + "/chat/completions"
    payload = {"model": c["model"], "messages": messages,
               "temperature": float(c["temperature"]), "max_tokens": max_tokens,
               "stream": False}
    read_to = max(5, int(timeout or c["timeout"] or 25))
    t0 = time.time()
    r = requests.post(url, json=payload, timeout=(8, read_to),
                      headers={"Authorization": "Bearer " + c["api_key"],
                               "Content-Type": "application/json"})
    r.raise_for_status()
    obj = r.json()
    ch = (obj.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):     # 少数兼容层返回分段内容
        content = "".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    usage = obj.get("usage") or {}
    return {"content": content, "usage": usage,
            "finish_reason": ch.get("finish_reason") or "",
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "max_tokens": max_tokens,
            "latency_ms": int((time.time() - t0) * 1000),
            "model": obj.get("model") or c["model"]}


def _chat_retry(messages: list[dict], c: dict, max_tokens: int = 900,
                timeout: int | None = None, tries: int = 2) -> dict:
    """超时 / 连不上就重试；4xx 直接抛（配置错了，重试也没用）。"""
    last: Exception | None = None
    for i in range(max(1, tries)):
        try:
            return _chat(messages, c, max_tokens=max_tokens, timeout=timeout)
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", 0) or 0
            if 400 <= int(status) < 500:
                raise
            last = e
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last = e
        if i + 1 < tries:
            time.sleep(2.0 * (i + 1))
    if last is not None:
        raise last
    raise RuntimeError("LLM 调用失败")


def test(body: dict | None = None) -> dict:
    c = _cfg()
    if body and any(k in body for k in ("base_url", "api_key", "model", "enabled", "max_tokens")):
        configure(body)
        c = _cfg()
    if not configured(c):
        return {"ok": False, "error": "未填完整：需要 base_url、api_key、model"}
    try:
        # 推理型模型会先烧 reasoning tokens，20 这种小预算会拿到空 content，所以给足 900
        r = _chat([{"role": "user", "content": "只回复两个字：可用"}], c, max_tokens=900)
        reply = str(r["content"]).strip()
        if not reply:
            return {"ok": False, "error": "端点连通，但返回内容为空（finish_reason=%s，reasoning_tokens=%s）。"
                                          "推理型模型的思考过程会占用 max_tokens，请把「最大输出 tokens」调到 2000 以上。"
                                          % (r.get("finish_reason") or "—", r.get("reasoning_tokens") or 0)}
        return {"ok": True, "model": r["model"], "latency_ms": r["latency_ms"],
                "reply": reply[:80], "usage": r["usage"],
                "finish_reason": r.get("finish_reason", "")}
    except Exception as e:
        return {"ok": False, "error": "连接失败：" + str(e)[:200]}


def ready(c: dict | None = None) -> bool:
    """复盘 AI 能否调用：既要配置完整，也要用户打开了 LLM 总开关。"""
    c = c or _cfg()
    return bool(c.get("enabled") and configured(c))


def chat(messages: list[dict], max_tokens: int = 3000,
         c: dict | None = None) -> dict:
    """通用对话入口，供 emtai 使用。失败不抛异常，统一返回 ok=False。

    与 rerank 的区别：这里不做候选白名单，由调用方负责解析与校验。
    """
    c = c or _cfg()
    if not configured(c):
        return {"ok": False, "error": "LLM 未配置：需要 base_url、api_key、model"}
    if not c.get("enabled"):
        return {"ok": False, "error": "LLM 总开关未启用（选股设置 → LLM 重排 → 启用）"}
    try:
        # 复盘日记/体检的输出比「重排」长得多，配置里的 25 秒读超时不够用
        r = _chat(messages, c, max_tokens=int(max_tokens or 3000),
                  timeout=max(60, int(c.get("timeout") or 25) * 3))
        return dict(r, ok=True)
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "LLM 调用失败：" + str(e)[:240]}


# --------------------------------------------------------------------------
# 本地规则解读（默认模式）
# --------------------------------------------------------------------------

def _fmt(v, nd=2, suffix="") -> str:
    try:
        if v is None:
            return "—"
        return ("%." + str(nd) + "f") % float(v) + suffix
    except Exception:
        return "—"


def _local_reason(r: dict) -> str:
    bits = []
    if r.get("mom20") is not None:
        bits.append("20日动量 " + _fmt(r["mom20"], 1, "%"))
    if r.get("mom60") is not None:
        bits.append("60日动量 " + _fmt(r["mom60"], 1, "%"))
    if r.get("vol20") is not None:
        bits.append("20日波动 " + _fmt(r["vol20"], 1, "%"))
    if r.get("rsi14") is not None:
        bits.append("RSI " + _fmt(r["rsi14"], 0))
    if r.get("vol_ratio") is not None:
        bits.append("量比 " + _fmt(r["vol_ratio"], 2))
    head = "、".join(bits[:4]) if bits else "因子数据不足"
    tail = "；横截面打分 " + _fmt(r.get("score"), 3)
    return head + tail + "。仅为因子描述，不含买卖判断。"


def _local(candidates: list[dict], warning: str = "", diag: dict | None = None) -> dict:
    rows = [dict(r) for r in candidates]
    for i, r in enumerate(rows, 1):
        r["llm_score"] = None
        r["reason"] = _local_reason(r)
        r["rank_llm"] = i
    out = {"ok": True, "mode": "local", "model": "", "rows": rows,
           "warning": warning or "当前为本地规则解读（未调用大模型）；在下方配置并启用 LLM 后可切换为模型重排。",
           "usage": {}, "latency_ms": 0, "privacy": PRIVACY}
    if diag:
        out["diag"] = diag            # 失败时把 finish_reason / 原始内容片段带回去，界面上可查
    return out


# --------------------------------------------------------------------------
# LLM 重排
# --------------------------------------------------------------------------

SYSTEM = ("你是 A 股量化研究助手。只依据用户给出的横截面因子与分数，对候选做相对排序并给出简短解释。"
          "禁止编造数据、禁止给买卖建议或目标价、禁止输出候选之外的代码。"
          "只输出严格 JSON：{\"items\":[{\"code\":\"6位代码\",\"score\":0到100的数字,\"reason\":\"不超过60字的中文说明\"}]}")


def _slim(r: dict) -> dict:
    keys = ("code", "name", "score", "base_score", "mom5", "mom20", "mom60",
            "vol20", "vol_ratio", "rsi14", "ma_dev", "macd_hist", "dd20", "industry")
    return {k: (round(float(r[k]), 3) if isinstance(r.get(k), (int, float)) else r.get(k))
            for k in keys if r.get(k) is not None}


def _parse_items(text: str) -> tuple[list[dict], str]:
    """把模型回复解析成 items。返回 (items, 失败原因)。

    容错顺序：整段 JSON → 剥 ```json 包裹 → 全角标点纠正 → 截断修复（补齐括号）→ 逐对象正则捞取。
    真实端点常出问题的地方：推理模型把预算烧光导致空内容、JSON 被截断、
    JSON 包在解释文字里、分数写成字符串、字段名换成 reason/comment。
    """
    raw = str(text or "").strip()
    if not raw:
        return [], "empty"

    def norm(obj) -> list[dict]:
        items = obj.get("items") if isinstance(obj, dict) else obj
        if isinstance(items, dict):
            items = [items]
        out = []
        for x in (items or []):
            if not isinstance(x, dict):
                continue
            code = x.get("code") or x.get("symbol") or x.get("股票代码")
            if code is None:
                continue
            reason = x.get("reason") or x.get("comment") or x.get("说明") or x.get("rationale") or ""
            out.append({"code": str(code), "score": x.get("score", x.get("分数")),
                        "reason": reason})
        return out

    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    fixed = (stripped.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'")
             .replace("，", ",").replace("：", ":"))
    i, j = stripped.find("{"), stripped.rfind("}")
    variants = [raw, stripped, fixed]
    if i >= 0 and j > i:
        variants += [stripped[i:j + 1], fixed[i:j + 1]]
    for t in variants:
        try:
            got = norm(json.loads(t))
            if got:
                return got, ""
        except Exception:
            pass

    # 截断修复：从第一个 { 开始补足未闭合的引号与括号
    if i >= 0:
        frag = stripped[i:]
        depth, in_str, esc = 0, False, False
        buf = []
        for ch in frag:
            buf.append(ch)
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
        if in_str:
            buf.append('"')
        repair = "".join(buf).rstrip().rstrip(",")
        # 截断点可能落在对象内或数组内，闭合顺序不确定 → 用 6 个字符以内的组合逐一试
        import itertools
        for n in range(1, 7):
            for extra in itertools.product("}]", repeat=n):
                try:
                    got = norm(json.loads(repair + "".join(extra)))
                except Exception:
                    continue
                if got:
                    return got, "truncated"

    # 兜底：逐个对象正则捞，外层结构坏了也能救回部分候选
    salvaged = []
    for o in re.findall(r"\{[^{}]*\}", stripped):
        c = re.search(r'"(?:code|symbol)"\s*:\s*"?(\d{6})"?', o)
        s2 = re.search(r'"(?:score|\u5206\u6570)"\s*:\s*([\d.]+)', o)
        r2 = re.search(r'"(?:reason|comment)"\s*:\s*"([^"]{0,200})"', o)
        if c and s2:
            salvaged.append({"code": c.group(1), "score": s2.group(1),
                             "reason": r2.group(1) if r2 else ""})
    if salvaged:
        return salvaged, "salvaged"
    return [], "unparsable"


SYSTEM_STRICT = (SYSTEM + " 注意：不要输出任何解释性文字或思考过程，第一个字符就必须是 {，最后一个字符必须是 }。")


def _ask(user: str, c: dict, budget: int, strict: bool = False,
         timeout: int | None = None) -> dict:
    return _chat([{"role": "system", "content": SYSTEM_STRICT if strict else SYSTEM},
                  {"role": "user", "content": user}], c, max_tokens=budget,
                 timeout=timeout)


def rerank(candidates: list[dict], as_of: str = "", horizon: int | None = None,
           force: bool = False) -> dict:
    """候选重排。未启用 / 未配置 / 调用失败 → 自动回退本地模式并明确说明。"""
    c = _cfg()
    if not candidates:
        return {"ok": False, "error": "没有可重排的候选，请先运行一键选股"}
    if not force and (not c.get("enabled") or not configured(c)):
        return _local(candidates)

    head = ("截止 %s，共 %d 只候选；模型口径：%s。请按相对强弱给 score 并逐条说明主要依据"
            "（每条 reason 不超过 40 字）。"
            % (as_of or "最近交易日", len(candidates),
               ("未来 %s 日预测超额收益" % horizon) if horizon else "横截面打分"))
    user = head + "\n候选 JSON：\n" + json.dumps([_slim(r) for r in candidates],
                                              ensure_ascii=False, separators=(",", ":"))
    budget = _budget(len(candidates), int(c.get("max_tokens") or 0))
    # 重排要逐条写理由，10 只也常在 30 秒上下；给配置超时的 3 倍，下限 60 秒
    to = max(60, int(c.get("timeout") or 25) * 3)
    try:
        r = _ask(user, c, budget, timeout=to)
        # 推理型模型会把 max_tokens 先花在 reasoning 上：内容为空/被截断时放大预算重试一次
        if not str(r.get("content") or "").strip() or r.get("finish_reason") == "length":
            bigger = min(max(budget * 2, 2400), 6000)
            if bigger > budget:
                r2 = _ask(user, c, bigger, strict=True, timeout=to)
                if str(r2.get("content") or "").strip():
                    r = r2
        content = str(r.get("content") or "")
        items, why = _parse_items(content)
        diag = {"finish_reason": r.get("finish_reason", ""),
                "reasoning_tokens": r.get("reasoning_tokens", 0),
                "max_tokens": r.get("max_tokens", budget),
                "content_len": len(content),
                "raw_preview": content[:400]}
        if not items:
            if why == "empty":
                msg = ("模型返回内容为空（finish_reason=%s，reasoning_tokens=%s / 预算 %s）。"
                       "推理型模型会把预算先花在思考上：把下方「最大输出 tokens」调到 2000~4000 再试。"
                       % (diag["finish_reason"] or "—", diag["reasoning_tokens"] or 0, diag["max_tokens"]))
            else:
                msg = "模型返回的内容不是可解析的 JSON（%d 字符），已回退本地规则解读（不伪造模型结论）。" % len(content)
            return _local(candidates, msg, diag)
        allowed = {str(x.get("code")).zfill(6) for x in candidates}
        picked = {}
        for x in items:
            code = str(x.get("code")).zfill(6)
            if code not in allowed or code in picked:
                continue
            try:
                sc = float(x.get("score"))
            except Exception:
                continue
            picked[code] = {"llm_score": round(max(0.0, min(100.0, sc)), 2),
                            "reason": re.sub(r"\s+", " ", str(x.get("reason") or ""))[:120]}
        if not picked:
            return _local(candidates, "LLM 未返回任何有效候选代码（可能编造了代码，已过滤），已回退本地规则解读。", diag)
        rows = [dict(x) for x in candidates]
        for x in rows:
            x.update(picked.get(x["code"], {"llm_score": None,
                                            "reason": "模型未覆盖该候选，保留原始排序。"}))
        rows.sort(key=lambda x: (x["llm_score"] is None, -(x["llm_score"] or 0),
                                 x.get("rank", 999)))
        for i, x in enumerate(rows, 1):
            x["rank_llm"] = i
        kept = len(picked)
        warn = ""
        if why in ("truncated", "salvaged"):
            warn = "模型输出被截断，已修复并救回 %d 只候选；建议把「最大输出 tokens」调大。" % kept
        if kept < len(candidates):
            warn = (warn + " " if warn else "") + ("模型只覆盖了 %d/%d 只候选，其余保留原排序（已过滤编造代码）。"
                                                   % (kept, len(candidates)))
        return {"ok": True, "mode": "llm", "model": r["model"], "rows": rows,
                "usage": r["usage"], "latency_ms": r["latency_ms"], "privacy": PRIVACY,
                "parse": why or "ok", "diag": diag, "warning": warn}
    except Exception as e:
        return _local(candidates, "LLM 调用失败（%s），已回退本地规则解读。" % str(e)[:160])


# ---- 重排结果持久化：刷新页面 / 重启服务后仍可恢复，不必重烧 token ----
LAST_PATH = BASE / "data" / "screen" / "llm_last.json"


def save_last(result: dict, as_of: str = "") -> None:
    """只存恢复所需的最小集合（分数/理由/名次），价格等行情字段恢复时用最新候选。"""
    if not result.get("ok") or not result.get("rows"):
        return
    items = [{"code": str(x.get("code") or ""), "llm_score": x.get("llm_score"),
              "reason": x.get("reason"), "rank_llm": x.get("rank_llm")}
             for x in result["rows"]]
    data = {"as_of": str(as_of or ""), "mode": result.get("mode", "local"),
            "model": result.get("model", ""),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": items}
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LAST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LAST_PATH)


def load_last() -> dict:
    if not LAST_PATH.exists():
        return {"ok": True, "saved": None}
    try:
        d = json.loads(LAST_PATH.read_text(encoding="utf-8"))
        if not d.get("items") or not d.get("as_of"):
            return {"ok": True, "saved": None}
        return {"ok": True, "saved": d}
    except Exception:
        return {"ok": True, "saved": None}


# --------------------------------------------------------------------------
# 空头辩护（bear case）：先让模型尽力否决，再让另一个模型裁决
# --------------------------------------------------------------------------
# 为什么不是「重排」：现在的用法是「模型已选中 Top20 → 让 LLM 找理由」，
# 那是典型的确认偏误 —— 它拿到的是已经被选中的票，只会论证「为什么好」。
# 改成对抗式：明确要求列否决理由 + 触发条件 + 观察指标，才可能真的降风险。
#
# 两段式（借 TradingAgents 的 tier 设计）：
#   辩护用 quick 模型（便宜、快，只看因子与行业）
#   裁决用 deep 模型（贵、慢，只看辩护文本做取舍）
# 显式分开的好处：辩护阶段要「尽量刁钻」，裁决阶段要「克制」，同一个模型很难同时做好。

BEAR_SYSTEM = (
    "你是 A 股量化研究的风控审查员。你的任务不是论证这些候选为什么好，"
    "而是尽力找出它们接下来为什么会亏。"
    "对每只股票给出至少 3 条否决理由，每条必须包含三个字段："
    "point（否决理由，不超过 40 字）、trigger（出现什么情况就说明这条理由成立）、"
    "watch（需要盯的具体指标，如「5 日换手率」「20 日波动」「行业指数 20 日涨幅」）。"
    "理由要具体到这只票的因子状态，不要写「市场有风险」这类空话。"
    "禁止编造财务数据、禁止给目标价或买卖建议、禁止输出候选之外的代码。"
    "只输出严格 JSON：{\"items\":[{\"code\":\"6位代码\",\"bears\":[{\"point\":\"\",\"trigger\":\"\",\"watch\":\"\"}]}]}"
)

JUDGE_SYSTEM = (
    "你是组合经理。庭上已经对每只候选提出了否决理由，你只做取舍，不补充新理由。"
    "判断标准：若某条否决理由的触发条件在当前数据下已经接近成立，或理由指向的是"
    "「可能造成永久性损失」的风险（退市、财务造假、流动性枯竭），则 drop；"
    "若只是短期波动或行业逆风，则 keep。"
    "约束：drop 的比例不要超过三分之一，除非理由确实严重。"
    "只输出严格 JSON：{\"items\":[{\"code\":\"6位代码\",\"verdict\":\"keep或drop\",\"reason\":\"不超过50字\"}],"
    "\"summary\":\"不超过60字的整体判断\"}"
)


def _loads_any(text: str) -> dict | list | None:
    """容错解析：剥 ```json、全角标点、截取首尾大括号、补未闭合括号。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S)
    fixed = (stripped.replace("“", '"').replace("”", '"')
             .replace("‘", "'").replace("’", "'")
             .replace("，", ",").replace("：", ":"))
    variants = [raw, stripped, fixed]
    i, j = stripped.find("{"), stripped.rfind("}")
    if i >= 0 and j > i:
        variants += [stripped[i:j + 1], fixed[i:j + 1]]
    for t in variants:
        try:
            return json.loads(t)
        except Exception:
            pass
    if i >= 0:
        frag = fixed[i:]
        depth, in_str, esc, buf = 0, False, False, []
        for ch in frag:
            buf.append(ch)
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
        if in_str:
            buf.append('"')
        repair = "".join(buf).rstrip().rstrip(",")
        import itertools
        for n in range(1, 7):
            for extra in itertools.product("}]", repeat=n):
                try:
                    return json.loads(repair + "".join(extra))
                except Exception:
                    continue
    return None


def _clamp_text(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _with_model(c: dict, model: str | None) -> dict:
    if not model:
        return c
    return {**c, "model": str(model)}


# 空头辩护一次要为一整批候选写理由，推理型模型光思考就可能几十秒。
# 实测 10 只一次请求在 deepseek-flash 上要 60~90 秒，而配置里的读超时是 25 秒，
# 必然 ReadTimeout。改成小批 + 独立长超时：单批失败只影响 4 只，还能自动对半拆。
BEAR_CHUNK = 4
BEAR_TIMEOUT = 90
JUDGE_CHUNK = 6
JUDGE_TIMEOUT = 60


def _bear_chunk(chunk: list[dict], as_of: str, quick: dict,
                min_bears: int) -> tuple[dict, dict]:
    """一批候选 → ({code: [bears]}, 原始响应)。解析失败抛异常，由上层拆半重试。"""
    head = ("截止 %s，本批 %d 只候选。请逐只列出至少 %d 条否决理由。\n候选 JSON：\n"
            % (as_of or "最近交易日", len(chunk), min_bears))
    payload = json.dumps([_slim(x) for x in chunk], ensure_ascii=False,
                         separators=(",", ":"))
    budget = max(1200, int(_budget(len(chunk)) * 1.6))
    r = _chat_retry([{"role": "system", "content": BEAR_SYSTEM},
                     {"role": "user", "content": head + payload}],
                    quick, max_tokens=budget, timeout=BEAR_TIMEOUT)
    obj = _loads_any(r.get("content") or "")
    items = (obj or {}).get("items") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise ValueError("输出无法解析为 JSON（finish_reason=%s，长度 %d）"
                         % (r.get("finish_reason") or "—", len(str(r.get("content") or ""))))
    allowed = {str(x.get("code")).zfill(6) for x in chunk}
    out: dict[str, list[dict]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").zfill(6)
        if code not in allowed:
            continue          # 编造的代码一律丢弃
        arr = it.get("bears") or it.get("bear_case") or it.get("points") or []
        if isinstance(arr, str):
            arr = [{"point": arr, "trigger": "", "watch": ""}]
        rows = []
        for b in (arr or []):
            if isinstance(b, str):
                b = {"point": b}
            if not isinstance(b, dict):
                continue
            p = _clamp_text(b.get("point") or b.get("reason") or b.get("text"), 120)
            if not p:
                continue
            rows.append({"point": p,
                         "trigger": _clamp_text(b.get("trigger") or b.get("when"), 80),
                         "watch": _clamp_text(b.get("watch") or b.get("indicator"), 60)})
        if rows:
            out[code] = rows[:6]
    return out, r


def _bear_collect(candidates: list[dict], as_of: str, quick: dict,
                  min_bears: int) -> tuple[dict, list, list]:
    """分批跑辩护；某批失败就对半拆开再试（最多拆 3 层）。"""
    bears: dict[str, list[dict]] = {}
    fails: list[str] = []
    batches: list[dict] = []

    def take(chunk: list[dict], depth: int = 0) -> None:
        if not chunk:
            return
        try:
            got, r = _bear_chunk(chunk, as_of, quick, min_bears)
            bears.update(got)
            batches.append({"n": len(chunk), "ms": r.get("latency_ms"),
                            "codes": [str(x.get("code")).zfill(6) for x in chunk]})
        except Exception as e:                         # noqa: BLE001
            if len(chunk) > 1 and depth < 3:
                mid = len(chunk) // 2
                take(chunk[:mid], depth + 1)
                take(chunk[mid:], depth + 1)
            else:
                fails.append("/".join(str(x.get("code")).zfill(6) for x in chunk)
                             + "：" + str(e)[:90])

    for i in range(0, len(candidates), BEAR_CHUNK):
        take(candidates[i:i + BEAR_CHUNK])
    return bears, fails, batches


def bear_case(candidates: list[dict], as_of: str = "", force: bool = False,
              judge: bool = True) -> dict:
    """对候选做空头辩护。未启用 / 未配置 / 失败 → 明确返回 off，不伪造结论。

    返回 rows 里每只票带 bears（≥3 条）与 verdict（keep/drop），drop 的不进最终名单。
    """
    c = _cfg()
    if not candidates:
        return {"ok": False, "error": "没有可辩护的候选，请先运行一键选股"}
    enabled = bool(c.get("bear_enabled"))
    if not (enabled or force) or not configured(c):
        return {"ok": True, "mode": "off", "rows": [], "kept": len(candidates),
                "dropped": 0, "enabled": enabled, "configured": configured(c),
                "note": "空头辩护未启用（或未配置端点）。要开启请在「数据中枢 → AI 服务」里打开开关；"
                        "开启后建议先跑一次离线 A/B，确认否决组确实更差再用于实盘。"}

    quick = _with_model(c, c.get("bear_model"))
    deep = _with_model(c, c.get("judge_model") or c.get("bear_model"))
    min_bears = max(1, min(int(c.get("bear_min") or 3), 6))

    bears, fails, batches = _bear_collect(candidates, as_of, quick, min_bears)
    if not bears:
        return {"ok": True, "mode": "local", "rows": [], "kept": len(candidates),
                "dropped": 0,
                "warning": "空头辩护调用失败（%s），本批不做否决。"
                           % (fails[0] if fails else "模型没有返回有效理由"),
                "diag": {"failed": fails, "batches": batches,
                         "timeout": BEAR_TIMEOUT, "chunk": BEAR_CHUNK}}

    verdicts = {code: ("keep", "") for code in bears}
    summary = ""
    judge_fails: list[str] = []
    if judge and bears:
        pairs = list(bears.items())
        for i in range(0, len(pairs), JUDGE_CHUNK):
            part = dict(pairs[i:i + JUDGE_CHUNK])
            jhead = ("截止 %s。下面是候选与各自的否决理由，请逐只裁决 keep 或 drop。\n"
                     % (as_of or "最近交易日"))
            jbody = json.dumps([{"code": k, "bears": v} for k, v in part.items()],
                               ensure_ascii=False, separators=(",", ":"))
            try:
                r2 = _chat_retry([{"role": "system", "content": JUDGE_SYSTEM},
                                  {"role": "user", "content": jhead + jbody}], deep,
                                 max_tokens=max(900, int(_budget(len(part)) * 0.7)),
                                 timeout=JUDGE_TIMEOUT)
                obj2 = _loads_any(r2.get("content") or "")
                if isinstance(obj2, dict) and not summary:
                    summary = _clamp_text(obj2.get("summary"), 120)
                its2 = (obj2 or {}).get("items") if isinstance(obj2, dict) else None
                for it in (its2 or []):
                    if not isinstance(it, dict):
                        continue
                    code = str(it.get("code") or "").zfill(6)
                    if code not in part:
                        continue
                    v = str(it.get("verdict") or it.get("decision") or "keep").lower()
                    verdicts[code] = ("drop" if "drop" in v or "否" in v or "拒" in v
                                      else "keep", _clamp_text(it.get("reason"), 120))
            except Exception as e:                     # noqa: BLE001
                judge_fails.append(str(e)[:60])
        if judge_fails and not summary:
            summary = "裁决模型调用失败（%s），失败批次全部保留。" % judge_fails[0]

    rows = []
    for x in candidates:
        code = str(x.get("code")).zfill(6)
        v, why = verdicts.get(code, ("keep", ""))
        rows.append({"code": code, "name": x.get("name") or code,
                     "bears": bears.get(code) or [], "verdict": v,
                     "judge_reason": why,
                     "enough": len(bears.get(code) or []) >= min_bears})
    dropped = [x for x in rows if x["verdict"] == "drop"]
    kept = [x for x in rows if x["verdict"] != "drop"]
    out = {"ok": True, "mode": "llm", "model": quick.get("model"),
           "judge_model": deep.get("model"), "rows": rows,
           "kept": len(kept), "dropped": len(dropped),
           "drop_codes": [x["code"] for x in dropped],
           "summary": summary,
           "latency_ms": sum(int(b.get("ms") or 0) for b in batches),
           "note": "辩护与裁决都只依据候选的因子数值，不发送账户信息；"
                   "drop 的票不进最终名单，但仍会写进决策档案，供事后 A/B 检验。",
           "privacy": PRIVACY,
           "diag": {"batches": batches, "chunk": BEAR_CHUNK,
                    "timeout": BEAR_TIMEOUT, "judge_fail": judge_fails}}
    if fails:
        # 部分成功也要说清楚：否则用户以为「10 只都辩护过了」
        out["warning"] = ("有 %d 批候选辩护失败（%s），这些票按「无否决理由」保留。"
                          % (len(fails), fails[0]))
        out["failed"] = fails
    return out
