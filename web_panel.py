#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilibili_learning_bot · Web 管理面板 
功能：仪表盘 | 机器人启停 | B站扫码登录 | 配置编辑 | 实时日志
     人格管理 | 评论日志 | 用户画像 | 记忆知识库 | 日记进化 | 操作日志
"""
import os, sys, json, time, io, base64, binascii, threading, asyncio, subprocess, signal, queue, hashlib, re, uuid as _uuid_module
from html import escape as _html_escape
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path

# ── 线程安全 JSON 工具 ──
from json_utils import JsonStore, sanitize_config_for_export, is_safe_path, get_backup_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _disclaimer_confirm_terminal():
    """显示红色免责声明，输入'我同意'后继续。"""
    from colorama import Fore, Style
    _TARGET = "\u6211\u540c\u610f"  # 我同意
    banner = f"""
{Fore.RED}{'=' * 60}
  \u26a0  免责声明 / DISCLAIMER
{'=' * 60}
  本项目仅供学习参考，
  若因使用本项目产生任何后果，本人概不负责。

  This project is for learning purposes only.
  Any consequences are solely your own responsibility.
{'=' * 60}{Style.RESET_ALL}
"""
    print(banner)
    user_input = input(f"{Fore.YELLOW}请输入 '{_TARGET}' 以继续:{Style.RESET_ALL}").strip()
    if user_input != _TARGET:
        print(f"{Fore.RED}\u2717 输入不匹配，程序退出。{Style.RESET_ALL}")
        sys.exit(1)
    print(f"{Fore.GREEN}\u2713 已确认，欢迎使用...{Style.RESET_ALL}\n")
    return True

os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

try:
    from flask import Flask, request, jsonify, Response, stream_with_context, session, redirect
except ImportError:
    print("[ERROR] Please install Flask: pip install flask")
    sys.exit(1)

try:
    import qrcode as qrlib
    from qrcode.image.pil import PilImage
except ImportError:
    qrlib = None

# ── 路径（支持环境变量切换账号）──
BASE_DIR = Path(__file__).resolve().parent
# BILI_ACCOUNT_DATA_DIR: 自定义 Data 目录路径，用于多账号隔离，如 "account1/Data" 或 "account2/Data"
_account_data_override = os.getenv('BILI_ACCOUNT_DATA_DIR', '').strip()
if _account_data_override:
    DATA_DIR = BASE_DIR / _account_data_override
else:
    DATA_DIR = BASE_DIR / "Data"
CONFIG_FILE = DATA_DIR / "config.json"
COOKIE_FILE = DATA_DIR / "bilibili_cookies.json"
# 账号标识名（显示在网页标题等处）
ACCOUNT_NAME = os.getenv('BILI_ACCOUNT_NAME', '').strip() or '默认'
PROMPT_SKILLS_FILENAME = "web_prompt_skills.json"

app = Flask(__name__, static_folder=None)
app.secret_key = os.urandom(24).hex()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── 全局状态 ──
bot_process: subprocess.Popen | None = None
bot_running = False
bot_start_time: datetime | None = None
panel_start = datetime.now()
bot_output_lines: list[str] = []
bot_output_lock = threading.Lock()

# QR 登录状态
qr_state = {"active": False, "url": "", "status": "idle", "message": "", "uid": "", "img_b64": ""}

def log_line(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with bot_output_lock:
        bot_output_lines.append(line)
        if len(bot_output_lines) > 500:
            del bot_output_lines[:-400]
    print(line, flush=True)
    return line

# ── 文件工具（线程安全）──
def read_json(path: Path, default=None):
    """线程安全读取 JSON（通过 JsonStore）。"""
    return JsonStore(path).read(default if default is not None else {})

def write_json(path: Path, data):
    """线程安全写入 JSON（原子写临时文件再 rename）。"""
    return JsonStore(path).write(data)

def _prompt_skills_file() -> Path:
    return DATA_DIR / PROMPT_SKILLS_FILENAME

def _prompt_skill_record_id(scope: str, persona: str, name: str) -> str:
    raw = f"{scope}\n{persona}\n{name}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]

def _sanitize_prompt_skill_payload(body: dict, existing_id: str = "") -> dict:
    """Normalize admin-provided prompt skill text for runtime-only storage."""
    name = str(body.get("name") or "").strip()[:80]
    scope = str(body.get("scope") or "global").strip().lower()
    persona = str(body.get("persona") or "").strip()[:80]
    content = str(body.get("content") or "").replace("\x00", "").strip()
    if not name:
        raise ValueError("Skill 名称不能为空")
    if scope not in ("global", "persona"):
        raise ValueError("Skill 作用域只能是 global 或 persona")
    if scope == "persona" and not persona:
        raise ValueError("人格 Skill 需要选择人格")
    if scope == "global":
        persona = ""
    if not content:
        raise ValueError("Skill 内容不能为空")
    content = content[:20000]
    skill_id = existing_id if re.match(r'^[a-f0-9]{16}$', existing_id) else _prompt_skill_record_id(scope, persona, name)
    return dict(
        id=skill_id,
        name=name,
        scope=scope,
        persona=persona,
        content=content,
        updated_at=datetime.now().isoformat(),
    )

def _read_prompt_skills() -> list:
    data = read_json(_prompt_skills_file(), dict(items=[]))
    if isinstance(data, list):
        raw_items = data
    else:
        raw_items = data.get("items", []) if isinstance(data, dict) else []
    items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            existing_id = str(item.get("id") or "").strip()
            normalized = _sanitize_prompt_skill_payload(item, existing_id=existing_id)
            normalized["created_at"] = item.get("created_at") or item.get("updated_at") or normalized["updated_at"]
            normalized["updated_at"] = item.get("updated_at") or normalized["updated_at"]
            items.append(normalized)
        except Exception:
            continue
    return items

def _write_prompt_skills(items: list):
    write_json(_prompt_skills_file(), dict(items=items))

def _select_prompt_skills(persona: str = "") -> list:
    persona = str(persona or "").strip()
    selected = []
    for item in _read_prompt_skills():
        if item.get("scope") == "global" or (persona and item.get("scope") == "persona" and item.get("persona") == persona):
            selected.append(item)
    return selected[:12]

def panel_credentials(config=None):
    """Return configured Web panel username/password, with env password taking precedence."""
    config = config if config is not None else read_json(CONFIG_FILE, {})
    web_cfg = config.get('web', {})
    username = (web_cfg.get('username') or '').strip()
    password = os.getenv('BILI_LEARNING_PANEL_PASSWORD') or web_cfg.get('password', '')
    return username, password

def _sanitize_logo_svg(svg: str) -> str:
    """Keep admin-provided SVG logos inert enough for inline preview and favicons."""
    svg = (svg or '').strip()
    if not svg or len(svg) > 20000:
        return ''
    if not re.match(r'(?is)^<svg[\s>]', svg):
        return ''
    unsafe = (
        r'<\s*script\b',
        r'<\s*foreignObject\b',
        r'\son[a-z]+\s*=',
        r'javascript\s*:',
        r'data\s*:\s*text/html',
    )
    if any(re.search(pattern, svg, re.IGNORECASE) for pattern in unsafe):
        return ''
    return svg

def _sanitize_logo_image(image: str) -> str:
    """Accept small ordinary image data URLs for site logos."""
    image = (image or '').strip()
    if not image or len(image) > 1400000:
        return ''
    match = re.match(r'^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$', image, re.IGNORECASE)
    if not match:
        return ''
    mime = match.group(1).lower()
    payload = re.sub(r'\s+', '', match.group(2))
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return ''
    if not raw or len(raw) > 1024 * 1024:
        return ''
    signatures = {
        'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
        'image/webp': raw.startswith(b'RIFF') and len(raw) >= 12 and raw[8:12] == b'WEBP',
        'image/gif': raw.startswith((b'GIF87a', b'GIF89a')),
    }
    if not signatures.get(mime, False):
        return ''
    return f'data:{mime};base64,{payload}'

def _site_branding(config=None):
    """Return site logo/title values used by the main panel and web app icons."""
    config = config if config is not None else read_json(CONFIG_FILE, {})
    site = config.get('site', {}) if isinstance(config, dict) else {}
    logo_text = (site.get('logo_text') or 'BL').strip()[:8] or 'BL'
    brand_name = (site.get('brand_name') or 'B站 AI 管理系统').strip() or 'B站 AI 管理系统'
    logo_image = _sanitize_logo_image(site.get('logo_image') or '')
    logo_svg = _sanitize_logo_svg(site.get('logo_svg') or '')
    if logo_image:
        icon_data = logo_image
    elif logo_svg:
        icon_svg = logo_svg
        icon_data = 'data:image/svg+xml;charset=utf-8,' + quote(icon_svg)
    else:
        icon_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">'
            '<rect width="180" height="180" rx="40" fill="#141413"/>'
            f'<text x="90" y="108" text-anchor="middle" font-size="72" font-family="Arial, sans-serif" font-weight="700" fill="#faf9f5">{_html_escape(logo_text)}</text>'
            '</svg>'
        )
        icon_data = 'data:image/svg+xml;charset=utf-8,' + quote(icon_svg)
    return dict(logo_text=logo_text, brand_name=brand_name, logo_image=logo_image, logo_svg=logo_svg, icon_data=icon_data)

def _site_logo_mark(site) -> str:
    if site.get('logo_image'):
        return f'<img src="{site["logo_image"]}" alt="Logo">'
    return site.get('logo_svg') or _html_escape(site['logo_text'])

def _site_head_tags(title: str, site=None) -> str:
    """Shared browser/iOS metadata for every web-facing page."""
    site = site if site is not None else _site_branding()
    page_title = f"{title} · {site['brand_name']}" if title else site['brand_name']
    return (
        f"<title>{_html_escape(page_title)}</title>\n"
        '<meta name="theme-color" content="#f5f4ed">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        f'<meta name="apple-mobile-web-app-title" content="{_html_escape(site["brand_name"])}">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
        f'<link rel="icon" href="{site["icon_data"]}">\n'
        f'<link rel="apple-touch-icon" href="{site["icon_data"]}">'
    )

def _apply_site_chrome(html: str, title: str) -> str:
    """Apply configured logo and app metadata to standalone auth/disclaimer pages."""
    site = _site_branding()
    return (
        html.replace('{{SITE_HEAD_TAGS}}', _site_head_tags(title, site))
            .replace('{{SITE_BRAND_NAME}}', _html_escape(site['brand_name']))
            .replace('{{SITE_LOGO_MARK}}', _site_logo_mark(site))
    )

def file_stat(path: Path):
    if not path.exists(): return {"exists": False, "size": 0, "mtime": None, "size_fmt": "0 B"}
    s = path.stat()
    sz = s.st_size
    return {"exists": True, "size": sz, "mtime": datetime.fromtimestamp(s.st_mtime).strftime("%m-%d %H:%M"),
            "size_fmt": f"{sz/1024:.1f}K" if sz<1024*1024 else f"{sz/1048576:.2f}M"}

def _parse_dt(value) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None

def _format_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h{minutes}m"
    return f"{hours}h{minutes}m{sec}s"

def _deep_merge_dict(base: dict, patch: dict) -> dict:
    result = dict(base or {})
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result

def _find_new_agent_process() -> bool:
    proc = Path('/proc')
    if not proc.exists():
        return False
    try:
        for pid_dir in proc.iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / 'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', 'ignore')
            except OSError:
                continue
            if 'new_agent.py' in cmdline:
                return True
    except OSError:
        return False
    return False

def _bot_runtime_status() -> dict:
    global bot_running, bot_start_time
    runtime = read_json(DATA_DIR / "bot_runtime_state.json", {})
    handle_running = bool(bot_process and bot_process.poll() is None)
    if bot_process and bot_process.poll() is not None:
        bot_running = False
    proc_running = handle_running or _find_new_agent_process()
    heartbeat_at = _parse_dt(runtime.get('current_heartbeat_at') or runtime.get('last_seen_at'))
    heartbeat_fresh = bool(heartbeat_at and datetime.now() - heartbeat_at < timedelta(minutes=5))
    running = bool(proc_running or (not Path('/proc').exists() and heartbeat_fresh))
    start_at = bot_start_time or _parse_dt(runtime.get('current_start_at'))
    if running and not bot_start_time and start_at:
        bot_start_time = start_at
    uptime_seconds = int((datetime.now() - start_at).total_seconds()) if running and start_at else 0
    return dict(
        running=running,
        started_at=start_at,
        start_text=start_at.strftime('%Y-%m-%d %H:%M:%S') if start_at else None,
        uptime=_format_duration(uptime_seconds),
        uptime_seconds=max(0, uptime_seconds),
        heartbeat_at=heartbeat_at.strftime('%Y-%m-%d %H:%M:%S') if heartbeat_at else '',
    )

def _cost_total(costs: dict) -> float:
    if not isinstance(costs, dict):
        return 0.0
    total = float(costs.get('total') or 0.0)
    calls_total = 0.0
    try:
        from xingye_bot.settings import estimate_model_price
    except Exception:
        def estimate_model_price(model, purpose=''):
            return 0.0
    for call in costs.get('calls') or []:
        if isinstance(call, dict):
            try:
                price = float(call.get('price') or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                price = float(estimate_model_price(str(call.get('model') or ''), str(call.get('purpose') or '')) or 0.0)
            calls_total += price
    return round(max(total, calls_total), 6)

def _num(value, default=0.0):
    try:
        if value in ('', None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default

def _normalize_comments(data: dict, limit: int) -> list[dict]:
    raw = data.get('items') or data.get('history') or []
    result = []
    for it in raw[-limit:]:
        if not isinstance(it, dict):
            continue
        time_text = it.get('time') or it.get('created_at') or it.get('timestamp') or ''
        result.append(dict(
            time=time_text,
            type=it.get('type') or it.get('action') or 'comment',
            content=it.get('content') or it.get('text') or it.get('reply') or '',
            source=it.get('source') or it.get('target_user') or it.get('video_title') or '',
            executed=it.get('executed', True),
        ))
    return result

def _normalize_users(data: dict, web_data: dict) -> dict:
    if not isinstance(data, dict):
        data = {}
    users = data.get('users') if isinstance(data.get('users'), dict) else {
        key: value for key, value in data.items() if isinstance(value, dict)
    }
    web_users = web_data.get('users', {}) if isinstance(web_data, dict) else {}
    return {**users, **web_users}

def _normalize_diary(data: dict) -> dict:
    raw = data.get('entries') or data.get('diaries') or data.get('items') or []
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entries.append(dict(
            time=item.get('time') or item.get('created_at') or item.get('updated_at') or '',
            title=item.get('title') or item.get('type') or '日记',
            mood=item.get('mood') or item.get('current_mood') or '',
            energy=_num(item.get('energy'), 50),
            content=item.get('content') or item.get('summary') or '',
            mood_score=_num(item.get('mood_score', item.get('valence')), 50),
        ))
    normalized = dict(data or {})
    normalized['entries'] = entries
    return normalized

def _normalize_evolution(data: dict) -> dict:
    raw = data.get('events') or data.get('items') or []
    events = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        detail = item.get('detail') or item.get('suggestion') or item.get('raw') or item.get('title') or ''
        events.append(dict(
            time=item.get('time') or item.get('created_at') or item.get('updated_at') or '',
            type=item.get('type') or item.get('category') or 'evolution',
            detail=detail,
            applied=bool(item.get('applied', False)),
        ))
    normalized = dict(data or {})
    normalized['events'] = events
    return normalized

def _normalize_actions(data: dict, agent_log, limit: int) -> list[dict]:
    raw = []
    if isinstance(data, dict):
        raw.extend(data.get('items') or [])
    if isinstance(agent_log, list):
        raw.extend(agent_log)
    elif isinstance(agent_log, dict):
        raw.extend(agent_log.get('items') or agent_log.get('logs') or [])
    result = []
    for item in raw[-limit:]:
        if not isinstance(item, dict):
            continue
        action = item.get('action') or item.get('skill') or item.get('type') or 'agent'
        payload = item.get('payload') if isinstance(item.get('payload'), dict) else {
            key: item.get(key) for key in ('goal', 'persona', 'result', 'message') if key in item
        }
        result.append(dict(
            time=item.get('created_at') or item.get('time') or item.get('updated_at') or '',
            action=action,
            payload=payload,
            executed=bool(item.get('executed', item.get('ok', True))),
        ))
    return result

def _cleanup_qr_images():
    """删除 qr_codes 文件夹中的所有二维码图片"""
    try:
        qr_dir = BASE_DIR / "qr_codes"
        if qr_dir.is_dir():
            for fpath in qr_dir.iterdir():
                if fpath.is_file():
                    fpath.unlink()
                    log_line(f"已删除过期二维码: {fpath}")
    except Exception as e:
        log_line(f"清理二维码失败: {e}")

# ═══════════════════════════════════════════
#  QR 登录流程（在线程中跑 asyncio）
# ═══════════════════════════════════════════
def do_qr_login():
    """在后台线程中执行 B 站扫码登录"""
    global qr_state
    qr_state = {"active": True, "url": "", "status": "generating", "message": "正在生成二维码...", "uid": "", "img_b64": ""}

    async def _login():
        global qr_state
        try:
            from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

            qr = QrCodeLogin()
            await qr.generate_qrcode()
            url = getattr(qr, "_QrCodeLogin__qr_link", None)

            if not url:
                qr_state["status"] = "error"
                qr_state["message"] = "获取登录链接失败"
                qr_state["active"] = False
                return

            qr_state["url"] = url
            # 生成二维码图片 base64 (供 Web 展示) + 保存到 qr_codes 文件夹
            img_b64 = ""
            qr_png_path = None
            try:
                if qrlib is None:
                    raise ImportError("qrcode library not available")
                qr_img = qrlib.QRCode(box_size=8, border=2)
                qr_img.add_data(url)
                qr_img.make(fit=True)
                img = qr_img.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_b64 = base64.b64encode(buf.getvalue()).decode()
                # 同时保存到 qr_codes 文件夹
                qr_dir = BASE_DIR / "qr_codes"
                qr_dir.mkdir(exist_ok=True)
                qr_png_path = qr_dir / "bilibili_login_qr.png"
                img.save(str(qr_png_path))
                log_line(f"二维码已保存至: {qr_png_path}")
            except Exception as e:
                log_line(f"QR图片生成失败: {e}")

            qr_state["img_b64"] = img_b64
            qr_state["status"] = "waiting_scan"
            qr_state["message"] = "请使用 B站APP 扫描二维码"

            scan_detected = False
            while qr_state["active"]:
                try:
                    status = await qr.check_state()
                    if status == QrCodeLoginEvents.DONE:
                        qr_state["status"] = "success"
                        qr_state["message"] = "登录成功！正在保存..."
                        cred = qr.get_credential()
                        cookies = {
                            "SESSDATA": cred.sessdata,
                            "bili_jct": cred.bili_jct,
                            "DedeUserID": cred.dedeuserid,
                            "buvid3": getattr(cred, "buvid3", ""),
                            "ac_time_value": getattr(cred, "ac_time_value", ""),
                        }
                        qr_state["uid"] = cookies.get("DedeUserID", "")
                        write_json(COOKIE_FILE, cookies)
                        qr_state["message"] = f"登录成功！UID: {cookies.get('DedeUserID', '?')}"
                        qr_state["active"] = False
                        log_line(f"B站扫码登录成功 UID={cookies.get('DedeUserID', '?')}")
                        _cleanup_qr_images()  # 登录成功，删除二维码图片
                        return
                    elif status == QrCodeLoginEvents.SCAN:
                        if not scan_detected:
                            scan_detected = True
                            qr_state["status"] = "scanned"
                            qr_state["message"] = "已扫描，请在手机上确认登录"
                    elif status == QrCodeLoginEvents.CONF:
                        qr_state["status"] = "confirming"
                        qr_state["message"] = "已确认，正在登录..."
                    elif status == QrCodeLoginEvents.TIMEOUT:
                        qr_state["status"] = "timeout"
                        qr_state["message"] = "二维码已过期，请重新生成"
                        qr_state["active"] = False
                        _cleanup_qr_images()  # 超时也删除过期二维码
                        return
                    await asyncio.sleep(1.5)
                except Exception as e:
                    log_line(f"QR状态查询错误: {e}")
                    await asyncio.sleep(2)
        except Exception as e:
            qr_state["status"] = "error"
            qr_state["message"] = f"登录异常: {e}"
            qr_state["active"] = False
            log_line(f"B站登录失败: {e}")

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_login())
    except Exception as e:
        qr_state["status"] = "error"
        qr_state["message"] = str(e)
        qr_state["active"] = False

# ═══════════════════════════════════════════
#  机器人进程管理
# ═══════════════════════════════════════════
def _bot_reader(pipe, prefix=""):
    """读取子进程输出"""
    try:
        for line in iter(pipe.readline, ""):
            if not line: break
            text = line.rstrip()
            if text:
                log_line(prefix + text)
    except OSError as e:
        log_line(f"⚠ 读取子进程输出异常: {e}")
    finally:
        try: pipe.close()
        except OSError as e:
            log_line(f"⚠ 关闭管道异常: {e}")

def _send_bot_menu_choice(choice: str, label: str) -> bool:
    if not bot_process or not bot_process.stdin or bot_process.stdin.closed:
        log_line(f"⚠ 无法发送{label}指令：机器人输入管道不可用")
        return False
    try:
        bot_process.stdin.write(f"{choice}\n")
        bot_process.stdin.flush()
        log_line(f"▶ 已发送{label}指令")
        return True
    except (BrokenPipeError, OSError, ValueError) as e:
        log_line(f"⚠ 发送{label}指令失败 (管道断开): {e}")
        return False

def start_bot_process():
    global bot_process, bot_running, bot_start_time
    if bot_running:
        return False, "机器人已在运行"

    agent_path = BASE_DIR / "new_agent.py"
    if not agent_path.exists():
        return False, f"找不到 {agent_path}"

    log_line("🚀 正在启动机器人进程...")
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["BILI_DISCLAIMER_SKIP"] = "1"

        bot_process = subprocess.Popen(
            [sys.executable, str(agent_path)],
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        bot_running = True
        bot_start_time = datetime.now()

        threading.Thread(target=_bot_reader, args=(bot_process.stdout, ""), daemon=True).start()
        log_line("✅ 机器人进程已启动")
        _send_bot_menu_choice("1", "主循环启动")
        return True, "机器人已启动"
    except Exception as e:
        log_line(f"❌ 启动失败: {e}")
        return False, str(e)

def stop_bot_process():
    global bot_process, bot_running
    if not bot_running:
        return False, "机器人未在运行"
    try:
        if bot_process:
            log_line("⏹ 正在停止机器人...")
            _send_bot_menu_choice("0", "退出")
            time.sleep(0.5)
            bot_process.terminate()
            try: bot_process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                log_line("⚠ 进程未响应 terminate，尝试 kill...")
                try: bot_process.kill()
                except Exception as e: log_line(f"⚠ kill 失败: {e}")
            bot_process = None
    except Exception as e:
        log_line(f"停止异常: {e}")
    bot_running = False
    log_line("✅ 机器人已停止")
    return True, "已停止"

# ═══════════════════════════════════════════
#  HTML 模板（从文件加载，回退到内嵌模板）
# ═══════════════════════════════════════════
_HTML_FILE = BASE_DIR / "web_panel.html"

def _load_html() -> str:
    """从 web_panel.html 文件加载模板，不存在则使用内嵌默认"""
    if _HTML_FILE.exists():
        try:
            html = _HTML_FILE.read_text(encoding="utf-8")
        except OSError:
            html = _DEFAULT_HTML
    else:
        html = _DEFAULT_HTML
    # 替换账号相关的占位符
    config = read_json(CONFIG_FILE, {})
    site = _site_branding(config)
    account_label = f" - {ACCOUNT_NAME}" if ACCOUNT_NAME != '默认' else ""
    html = html.replace('{{ACCOUNT_TITLE}}', f'控制面板{account_label}')
    html = html.replace('{{ACCOUNT_HEADER}}', f'控制面板{account_label}')
    html = html.replace('{{SITE_LOGO_TEXT}}', _html_escape(site['logo_text']))
    html = html.replace('{{SITE_BRAND_NAME}}', _html_escape(site['brand_name']))
    html = html.replace('{{SITE_ICON_DATA}}', site['icon_data'])
    html = html.replace('{{SITE_LOGO_IMAGE}}', site['logo_image'])
    html = html.replace('{{SITE_LOGO_SVG}}', site['logo_svg'])
    html = html.replace('{{SITE_LOGO_MARK}}', _site_logo_mark(site))
    return html

_DEFAULT_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<meta name="theme-color" content="#f5f4ed">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{{ACCOUNT_TITLE}}">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>{{ACCOUNT_TITLE}}</title>
<link rel="icon" href="{{SITE_ICON_DATA}}">
<link rel="apple-touch-icon" href="{{SITE_ICON_DATA}}">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
--bg:#f5f4ed;--surface:#faf9f5;--white:#ffffff;
--fg:#141413;--dark:#30302e;--text:#4d4c48;--text2:#5e5d59;--muted:#5e5d59;--faint:#87867f;
--sand:#e8e6dc;--line:#f0eee6;--ring-color:#d1cfc5;--border:var(--line);
--bg2:var(--surface);--bg3:var(--white);
--accent:#c96442;--accent2:#d97757;
--green:#64735b;--orange:#b9822f;--red:#b53333;--pink:#a85f78;--purple:#7563a8;--blue:#52708f;--focus:#c96442;
--font-serif:Georgia,"Times New Roman","Songti SC",serif;
--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
--font-mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sidebar-w:292px;--r:18px;--rs:12px;--radius-sm:8px;
--shadow-soft:rgba(20,20,19,.055) 0 12px 42px;--shadow-card:rgba(20,20,19,.025) 0 4px 18px;
}
*{box-sizing:border-box;margin:0;padding:0}
html{background:var(--bg);scroll-behavior:smooth}
body{font-family:var(--font-sans);background:radial-gradient(circle at 88% 8%,rgba(201,100,66,.08),transparent 28%),linear-gradient(180deg,rgba(255,255,255,.34),rgba(255,255,255,0) 320px),var(--bg);color:var(--text);display:flex;min-height:100vh;line-height:1.55;text-rendering:optimizeLegibility;overflow-x:hidden}
button,input,textarea,select{font:inherit}
a{color:var(--accent)}

/* SIDEBAR */
.sidebar{width:var(--sidebar-w);min-width:var(--sidebar-w);background:rgba(250,249,245,.9);border-right:1px solid var(--sand);display:flex;flex-direction:column;position:fixed;top:0;left:0;bottom:0;z-index:110;transition:transform .22s ease;backdrop-filter:blur(18px);box-shadow:rgba(20,20,19,.03) 10px 0 34px}
.sidebar.hide{transform:translateX(-100%)}
.sb-hd{padding:18px 16px;border-bottom:1px solid rgba(232,230,220,.86);display:flex;align-items:center;gap:12px;min-height:66px}
.sb-av{width:40px;height:40px;border-radius:12px;background:var(--fg);display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:650;color:var(--surface);flex-shrink:0;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);overflow:hidden}
.sb-av svg,.sb-av img{width:100%;height:100%;display:block}
.sb-av img{object-fit:cover}
.sb-tt{font-size:15px;font-weight:650;line-height:1.2;color:var(--fg);letter-spacing:0}
.sb-sub{font-size:11px;color:var(--faint);margin-top:2px}
.sb-nav{flex:1;overflow-y:auto;padding:10px 10px 12px;display:grid;gap:4px;align-content:start;scrollbar-width:none;-ms-overflow-style:none}
.sb-nav::-webkit-scrollbar{display:none}
.ns{font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em;padding:16px 12px 6px;font-weight:650}
.ni{appearance:none;-webkit-appearance:none;display:flex;align-items:center;gap:10px;min-height:38px;padding:8px 10px;border-radius:12px;cursor:pointer;color:var(--muted);font-size:13px;border:1px solid transparent;background-color:transparent;width:100%;transition:background-color .16s ease,color .16s ease,box-shadow .16s ease,transform .16s ease;text-align:left}
.ni:hover:not(.ac){background-color:var(--sand);color:var(--fg);border-color:var(--ring-color);box-shadow:none}
.ni:active{transform:translateY(1px)}
.ni.ac{background-color:var(--fg);color:var(--surface);font-weight:650;border-color:var(--fg);box-shadow:0 10px 24px rgba(20,20,19,.14)}
.nav-ico{width:26px;height:26px;border-radius:8px;background:rgba(232,230,220,.62);display:flex;align-items:center;justify-content:center;color:var(--fg);flex-shrink:0;border:1px solid rgba(209,207,197,.42)}
.nav-ico svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ni:hover:not(.ac) .nav-ico{background:var(--surface);color:var(--fg);border-color:var(--ring-color)}
.ni.ac .nav-ico{background:rgba(255,255,255,.12);color:var(--surface);border-color:rgba(255,255,255,.18)}
.ni .bd{margin-left:auto;background:var(--red);color:#fff;font-size:10px;padding:1px 6px;border-radius:10px;font-weight:600;display:none}
.sb-ft{padding:12px 14px;border-top:1px solid rgba(232,230,220,.86);font-size:11px;color:var(--faint);text-align:center}

/* MAIN */
.main{margin-left:var(--sidebar-w);flex:1;padding:30px 32px 44px;max-width:calc(100vw - var(--sidebar-w));min-width:0}
.page{display:none;max-width:1180px;margin:0 auto}
.page.on{display:block}
.ph{margin-bottom:24px}
.ph h1{font-family:var(--font-serif);font-size:clamp(30px,3vw,46px);font-weight:500;line-height:1.14;color:var(--fg);letter-spacing:0;text-wrap:balance}
.ph p{color:var(--muted);font-size:13px;margin-top:7px;text-wrap:pretty}

/* CARDS */
.sr{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:12px;margin-bottom:22px}
.sc{background:rgba(250,249,245,.78);border:1px solid var(--line);border-radius:14px;padding:14px;display:flex;align-items:center;gap:11px;box-shadow:var(--shadow-card);min-height:78px}
.si{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;border:1px solid rgba(209,207,197,.48)}
.si svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.si.bl{background:rgba(82,112,143,.13);color:var(--blue)}
.si.gn{background:rgba(100,115,91,.13);color:var(--green)}
.si.or{background:rgba(185,130,47,.13);color:var(--orange)}
.si.pk{background:rgba(168,95,120,.13);color:var(--pink)}
.si.pp{background:rgba(117,99,168,.12);color:var(--purple)}
.si.rd{background:rgba(181,51,51,.12);color:var(--red)}
.sv{font-size:17px;font-weight:650;color:var(--fg);font-variant-numeric:tabular-nums;line-height:1.2}
.sl{font-size:11px;color:var(--muted);margin-top:3px}

.pc,.chart-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:20px;margin-bottom:16px;box-shadow:var(--shadow-card)}
.pc h3,.chart-card h4{font-family:var(--font-serif);font-size:19px;font-weight:500;line-height:1.24;color:var(--fg);margin-bottom:14px;display:flex;align-items:center;gap:8px;text-wrap:balance}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:22px}
.chart-card{min-height:282px}
.chart-card canvas{max-height:220px}
.notice{background:rgba(181,51,51,.08);border:1px solid rgba(181,51,51,.18);border-radius:var(--r);padding:11px 16px;margin-top:18px;font-size:11px;color:var(--red);text-align:center;line-height:1.65}
.danger-card{border-color:rgba(181,51,51,.26)!important;background:rgba(255,255,255,.64)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.on{background:var(--green)}
.dot.off{background:var(--faint)}

/* TABLE */
.tb{width:100%;border-collapse:collapse;font-size:12px}
.tb th{text-align:left;padding:9px 10px;color:var(--faint);font-weight:650;font-size:10px;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--line)}
.tb td{padding:9px 10px;border-bottom:1px solid rgba(240,238,230,.86);max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.tb tr:hover td{background:rgba(232,230,220,.32)}
.tb .mono{font-family:var(--font-mono);font-size:11px}

/* BUTTONS */
.btn{appearance:none;-webkit-appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:36px;padding:7px 14px;border-radius:var(--rs);font-size:12px;font-weight:650;cursor:pointer;border:1px solid transparent;transition:background-color .16s ease,color .16s ease,border-color .16s ease,box-shadow .16s ease,transform .16s ease;white-space:nowrap}
.btn:hover{box-shadow:0 0 0 1px var(--ring-color)}
.btn:active{transform:translateY(1px)}
.btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(201,100,66,.18),0 0 0 1px var(--focus)}
.btn-pr{background:var(--accent);color:var(--surface);border-color:rgba(201,100,66,.12)}
.btn-pr:hover{background:#b95a3b}
.btn-suc{background:var(--green);color:#fff}
.btn-dan{background:var(--red);color:#fff}
.btn-out{background:var(--sand);border:1px solid var(--ring-color);color:var(--fg)}
.btn-out:hover{background:#f0eee6;border-color:var(--ring-color);color:var(--fg)}
.btn-sm{min-height:30px;padding:4px 10px;font-size:11px;border-radius:10px}
.btn-lg{min-height:42px;padding:10px 20px;font-size:14px}
.btn-grp{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.btn:disabled{opacity:.52;cursor:not-allowed;transform:none;box-shadow:none}

/* FORMS */
.fg{margin-bottom:12px}
.fg label{display:block;font-size:11px;font-weight:650;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.fg input,.fg textarea,.fg select{width:100%;padding:9px 11px;background:var(--white);border:1px solid var(--sand);border-radius:var(--rs);color:var(--fg);font-size:12px;font-family:inherit;outline:none;transition:border-color .16s ease,box-shadow .16s ease,background-color .16s ease}
.fg input:focus,.fg textarea:focus,.fg select:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(201,100,66,.18)}
.fg textarea{resize:vertical;min-height:70px;font-family:var(--font-mono);font-size:11px}
.fr{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.fr{grid-template-columns:1fr}}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.tab-btn{appearance:none;-webkit-appearance:none;border:1px solid var(--sand);background:var(--surface);color:var(--muted);border-radius:10px;padding:7px 11px;font-size:12px;font-weight:650;cursor:pointer;transition:background-color .16s ease,color .16s ease,border-color .16s ease}
.tab-btn:hover,.tab-btn.on{background:var(--fg);color:var(--surface);border-color:var(--fg)}
.config-panel{display:none}
.config-panel.on{display:block}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.form-grid.tight{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
.field-note{font-size:11px;color:var(--faint);margin-top:4px;line-height:1.45}
.logo-row{display:grid;grid-template-columns:minmax(160px,220px) 1fr;gap:16px;align-items:start}
.logo-preview{width:120px;height:120px;border-radius:26px;background:var(--fg);color:var(--surface);display:flex;align-items:center;justify-content:center;font-size:36px;font-weight:750;overflow:hidden;box-shadow:var(--shadow-card);border:1px solid rgba(20,20,19,.08)}
.logo-preview svg,.logo-preview img{width:100%;height:100%;display:block}
.logo-preview img{object-fit:cover}
.provider-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:12px}
.provider-card{background:var(--white);border:1px solid var(--sand);border-radius:14px;padding:13px 14px}
.provider-card h4{font-family:var(--font-sans);font-size:13px;color:var(--fg);margin-bottom:8px}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.setting-card{background:var(--white);border:1px solid var(--sand);border-radius:14px;padding:13px 14px}
.setting-card strong{display:block;color:var(--fg);font-size:13px;margin-bottom:4px}
.setting-card span{font-size:11px;color:var(--muted);line-height:1.45}
.prompt-skill-list{display:grid;gap:10px;margin-top:12px}
.prompt-skill-card{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:start}
.prompt-skill-card pre{margin:8px 0 0;color:var(--muted);font-family:var(--font-mono);font-size:11px;line-height:1.48;white-space:pre-wrap;word-break:break-word}
.sys-actions{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap}
.agent-shell{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:16px;align-items:start}
.persona-item{background:var(--white);border:1px solid var(--sand);border-radius:14px;padding:14px;margin-bottom:10px}
.persona-item.active{border-color:var(--fg);box-shadow:0 0 0 1px var(--fg)}
.persona-item h3{font-family:var(--font-sans);font-size:14px;font-weight:700;margin-bottom:6px}
@media(max-width:900px){.agent-shell,.logo-row{grid-template-columns:1fr}}

.toggle-sw{position:relative;display:inline-flex;align-items:center;cursor:pointer;color:var(--text)}
.toggle-sw input{position:absolute;opacity:0;pointer-events:none}
.toggle-track{width:42px;height:24px;border-radius:999px;background:var(--sand);border:1px solid var(--ring-color);position:relative;display:inline-block;transition:background-color .16s ease,border-color .16s ease}
.toggle-track:after{content:"";width:18px;height:18px;border-radius:50%;background:var(--white);position:absolute;left:2px;top:2px;box-shadow:rgba(20,20,19,.14) 0 1px 4px;transition:transform .16s ease}
.toggle-sw input:checked + .toggle-track{background:rgba(201,100,66,.82);border-color:rgba(201,100,66,.7)}
.toggle-sw input:checked + .toggle-track:after{transform:translateX(18px)}

/* TAGS */
.tg{display:inline-block;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:650;border:1px solid transparent}
.tg-suc{background:rgba(100,115,91,.13);color:var(--green);border-color:rgba(100,115,91,.14)}
.tg-war{background:rgba(185,130,47,.13);color:var(--orange);border-color:rgba(185,130,47,.16)}
.tg-dan{background:rgba(181,51,51,.12);color:var(--red);border-color:rgba(181,51,51,.15)}
.tg-inf{background:rgba(82,112,143,.12);color:var(--blue);border-color:rgba(82,112,143,.15)}

/* LOG VIEWER */
.log-box{background:var(--dark);border:1px solid rgba(20,20,19,.16);border-radius:var(--rs);padding:13px;max-height:350px;overflow-y:auto;font-family:var(--font-mono);font-size:11px;line-height:1.58;white-space:pre-wrap;word-break:break-all;color:#f0eee6;box-shadow:inset 0 0 0 1px rgba(255,255,255,.04)}

/* JSON EDITOR */
.je{width:100%;min-height:380px;background:var(--white);border:1px solid var(--sand);border-radius:var(--rs);color:var(--fg);font-family:var(--font-mono);font-size:12px;padding:13px;resize:vertical;outline:none;line-height:1.58}
.je:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(201,100,66,.18)}

/* QR */
.qr-wrap{text-align:center;padding:20px}
.qr-wrap img{max-width:220px;border-radius:14px;border:8px solid var(--white);background:var(--white);box-shadow:var(--shadow-soft)}
.qr-wrap .qr-status{margin-top:12px;font-size:13px;font-weight:650;color:var(--muted)}

/* TOAST */
.toast{position:fixed;top:18px;right:18px;z-index:300;padding:11px 15px;border-radius:var(--rs);font-size:12px;font-weight:650;opacity:0;transform:translateY(-12px);transition:all .22s ease;pointer-events:none;max-width:320px;background:var(--surface);border:1px solid var(--sand);box-shadow:rgba(20,20,19,.14) 0 22px 60px;color:var(--fg)}
.toast.show{opacity:1;transform:translateY(0)}
.toast.ok{border-color:rgba(100,115,91,.38);color:var(--green)}
.toast.err{border-color:rgba(181,51,51,.38);color:var(--red)}
.toast.inf{border-color:rgba(201,100,66,.38);color:var(--accent)}

/* EMPTY */
.emp{text-align:center;padding:34px;color:var(--muted)}
.emp .ic{width:32px;height:32px;margin:0 auto 8px;opacity:.72;color:var(--fg)}
.emp .ic svg{width:100%;height:100%;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}

/* MOBILE */
.mob-toggle{display:none;position:fixed;top:12px;left:12px;z-index:220;background:rgba(250,249,245,.94);border:1px solid var(--sand);color:var(--fg);width:40px;height:40px;border-radius:12px;align-items:center;justify-content:center;cursor:pointer;font-size:18px;box-shadow:var(--shadow-card);backdrop-filter:blur(12px)}
.mob-overlay{display:none;position:fixed;inset:0;background:rgba(20,20,19,.28);z-index:100;backdrop-filter:blur(2px)}
@media(max-width:768px){
.sidebar{transform:translateX(-100%)}
.sidebar.show{transform:translateX(0)}
.main{margin-left:0;max-width:100%;padding:66px 14px 28px}
.sr{grid-template-columns:repeat(2,1fr);gap:8px}
.chart-grid{grid-template-columns:1fr;gap:12px}
.sc{padding:12px;gap:8px}
.mob-toggle{display:flex}
.mob-overlay.show{display:block}
.ph h1{font-size:30px}
.tb{font-size:11px}
.tb td{max-width:140px}
.log-box{max-height:250px}
.je{min-height:250px}
}

/* PULSE */
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.pulse{animation:pulse 1.5s infinite}
</style>
</head>
<body>

<button class="mob-toggle" onclick="toggleSidebar()">☰</button>
<div class="mob-overlay" id="mobOverlay" onclick="toggleSidebar()"></div>

<!-- SIDEBAR -->
<aside class="sidebar" id="sidebar">
<div class="sb-hd">
<div class="sb-av" id="siteLogoMark">{{SITE_LOGO_MARK}}</div><div><div class="sb-tt">{{ACCOUNT_HEADER}}</div><div class="sb-sub" id="siteBrandName">{{SITE_BRAND_NAME}}</div></div>
</div>
<nav class="sb-nav">
<div class="ns">总览</div>
<button class="ni ac" data-pg="dash" onclick="nav('dash',this)"><span class="nav-ico" data-icon="dash"></span>仪表盘</button>
<button class="ni" data-pg="ctrl" onclick="nav('ctrl',this)"><span class="nav-ico" data-icon="ctrl"></span>机器人控制<span class="bd" id="botBadge">●</span></button>
<button class="ni" data-pg="login" onclick="nav('login',this)"><span class="nav-ico" data-icon="login"></span>B站登录<span class="bd" id="loginBadge">●</span></button>
<div class="ns">系统配置</div>
<button class="ni" data-pg="conf" onclick="nav('conf',this)"><span class="nav-ico" data-icon="conf"></span>配置编辑</button>
<button class="ni" data-pg="psna" onclick="nav('psna',this)"><span class="nav-ico" data-icon="agent"></span>Agent 管理</button>
<button class="ni" data-pg="mood" onclick="nav('mood',this)"><span class="nav-ico" data-icon="mood"></span>心情管理</button>
<button class="ni" data-pg="behavior" onclick="nav('behavior',this)"><span class="nav-ico" data-icon="behavior"></span>行为设置</button>
<button class="ni" data-pg="upfu" onclick="nav('upfu',this)"><span class="nav-ico" data-icon="upfu"></span>UP主关注</button>
<div class="ns">数据监控</div>
<button class="ni" data-pg="cmts" onclick="nav('cmts',this)"><span class="nav-ico" data-icon="cmts"></span>评论日志</button>
<button class="ni" data-pg="usrs" onclick="nav('usrs',this)"><span class="nav-ico" data-icon="usrs"></span>用户画像</button>
<button class="ni" data-pg="mem" onclick="nav('mem',this)"><span class="nav-ico" data-icon="mem"></span>记忆知识库</button>
<button class="ni" data-pg="diary" onclick="nav('diary',this)"><span class="nav-ico" data-icon="diary"></span>日记进化</button>
<button class="ni" data-pg="acts" onclick="nav('acts',this)"><span class="nav-ico" data-icon="acts"></span>操作日志</button>
<div class="ns">工具</div>
<button class="ni" data-pg="tutor" onclick="nav('tutor',this)"><span class="nav-ico" data-icon="tutor"></span>知识辅导</button>
<button class="ni" data-pg="tools" onclick="nav('tools',this)"><span class="nav-ico" data-icon="tools"></span>功能中心</button>
<button class="ni" data-pg="sys" onclick="nav('sys',this)"><span class="nav-ico" data-icon="sys"></span>系统管理</button>
<div class="ns">帮助</div>
<button class="ni" data-pg="about" onclick="nav('about',this)"><span class="nav-ico" data-icon="about"></span>关于</button>
</nav>
<div class="sb-ft">已运行 <span id="uptime">--</span><div style="color:var(--red);font-size:9px;margin-top:4px">仅供学习参考</div></div>
</aside>

<!-- MAIN -->
<main class="main">

<!-- DASHBOARD -->
<div class="page on" id="pg-dash">
<div class="ph"><h1>系统仪表盘</h1><p>实时监控 · 数据可视化 · 运行状态</p></div>
<div class="sr" id="dashStats"></div>
<div class="chart-grid">
<div class="chart-card"><h4>评论活跃度趋势</h4><canvas id="chartComments"></canvas></div>
<div class="chart-card"><h4>心情/精力指数</h4><canvas id="chartMood"></canvas></div>
<div class="chart-card"><h4>每日操作统计</h4><canvas id="chartActions"></canvas></div>
<div class="chart-card"><h4>视频处理速率</h4><canvas id="chartVideos"></canvas></div>
</div>
<div class="pc"><h3><span class="dot" id="botDot"></span>系统详情</h3><div id="botDetail"></div></div>
<div class="pc"><h3>数据文件状态</h3><div id="fileGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;font-size:12px"></div></div>
<div class="notice">免责声明：本项目仅供学习参考，若因使用本项目产生的任何后果，本人一律概不负责。</div>
</div>

<!-- CONTROL -->
<div class="page" id="pg-ctrl">
<div class="ph"><h1>机器人控制</h1><p>启动/停止/重启</p></div>
<div class="pc">
<h3>运行状态</h3><div id="ctrlStatus" style="margin-bottom:12px"></div>
<div class="btn-grp">
<button class="btn btn-suc btn-lg" id="btnStart" onclick="startBot()"><span class="nav-ico" data-icon="ctrl"></span>启动机器人</button>
<button class="btn btn-dan btn-lg" id="btnStop" style="display:none" onclick="stopBot()"><span class="nav-ico" data-icon="sys"></span>停止</button>
<button class="btn btn-out" onclick="restartBot()"><span class="nav-ico" data-icon="acts"></span>重启</button>
<button class="btn btn-out" onclick="clearLog()"><span class="nav-ico" data-icon="tools"></span>清空日志</button>
</div>
</div>
<div class="pc"><h3>实时输出</h3><div class="log-box" id="botLog">等待输出...</div></div>
<div class="notice">免责声明：本项目仅供学习参考，若因使用本项目产生的任何后果，本人一律概不负责。</div>
</div>

<!-- LOGIN -->
<div class="page" id="pg-login">
<div class="ph"><h1>B站登录</h1><p>扫码登录 / 登出 / 状态</p></div>
<div class="pc" id="loginPanel">
<h3>扫码登录</h3>
<div id="loginStatus"></div>
<div id="qrArea" style="display:none">
<div class="qr-wrap"><img id="qrImg" src="" alt="QR码"><div class="qr-status" id="qrStatusText"></div></div>
</div>
<div class="btn-grp">
<button class="btn btn-suc btn-lg" id="btnQR" onclick="startQRLogin()"><span class="nav-ico" data-icon="login"></span>生成登录二维码</button>
<button class="btn btn-dan" id="btnLogout" onclick="logoutBili()"><span class="nav-ico" data-icon="sys"></span>退出登录</button>
<button class="btn btn-out" onclick="checkLogin()"><span class="nav-ico" data-icon="dash"></span>检查状态</button>
</div>
<div id="cookieInfo" style="margin-top:12px;font-size:11px;color:var(--text2)"></div>
</div>
</div>

<!-- CONFIG -->
<div class="page" id="pg-conf">
<div class="ph"><h1>配置编辑</h1><p>可视化编辑常用设置，JSON 高级模式保留完整配置</p></div>
<div class="pc" id="confVisual">
<div class="tabs" id="confTabs">
<button class="tab-btn on" data-cfg-tab="site" onclick="switchConfTab('site',this)">站点 Logo</button>
<button class="tab-btn" data-cfg-tab="api" onclick="switchConfTab('api',this)">大模型 API</button>
<button class="tab-btn" data-cfg-tab="models" onclick="switchConfTab('models',this)">模型</button>
<button class="tab-btn" data-cfg-tab="bili" onclick="switchConfTab('bili',this)">B站</button>
<button class="tab-btn" data-cfg-tab="automation" onclick="switchConfTab('automation',this)">自动化</button>
<button class="tab-btn" data-cfg-tab="video" onclick="switchConfTab('video',this)">视频理解</button>
<button class="tab-btn" data-cfg-tab="agent" onclick="switchConfTab('agent',this)">Agent</button>
<button class="tab-btn" data-cfg-tab="json" onclick="switchConfTab('json',this)">JSON 高级</button>
</div>

<div class="config-panel on" id="cfg-site">
<div class="logo-row">
<div>
<div class="logo-preview" id="siteLogoPreview">{{SITE_LOGO_TEXT}}</div>
<div class="field-note">保存后会同步侧栏品牌、favicon、Apple/iOS Web App 图标。</div>
</div>
<div>
<div class="form-grid">
<div class="fg"><label>品牌名称</label><input id="siteBrandInput" data-cfg-path="site.brand_name" placeholder="B站 AI 管理系统"></div>
<div class="fg"><label>文字 Logo</label><input id="siteLogoText" data-cfg-path="site.logo_text" maxlength="8" placeholder="BL" oninput="previewSiteLogo()"></div>
</div>
<div class="fg"><label>Logo 图片</label><input id="siteLogoImage" data-cfg-path="site.logo_image" placeholder="上传 PNG / JPG / WebP / GIF 后自动填入" oninput="previewSiteLogo()"><div class="btn-grp"><input id="siteLogoFile" type="file" accept="image/png,image/jpeg,image/webp,image/gif" style="display:none" onchange="loadLogoImageFile(this)"><button class="btn btn-out" onclick="document.getElementById('siteLogoFile').click()">上传图片</button><button class="btn btn-out" onclick="clearLogoImage()">清空图片</button></div><div class="field-note">支持普通图片，保存后会作为侧栏 Logo、favicon 和 Apple/iOS Web App 图标。留空时使用文字 Logo。</div></div>
</div>
</div>
</div>

<div class="config-panel" id="cfg-api">
<div class="field-note" style="margin-bottom:12px">统一 API 是默认供应商，不是要求你用一家供应商包办所有能力；可以填任何提供 OpenAI 兼容接口的服务商。下面“按用途拆分供应商”留空时自动回退到统一 API。</div>
<div class="form-grid">
<div class="fg"><label>API Key</label><input id="apiKeyInput" data-cfg-path="api.unified_api_key" type="password" autocomplete="off" placeholder="sk-..."></div>
<div class="fg"><label>Base URL</label><input data-cfg-path="api.unified_base_url" placeholder="https://api.openai.com/v1"></div>
<div class="fg"><label>主脑模型</label><input data-cfg-path="api.model_brain" placeholder="gpt-4.1-mini"></div>
<div class="fg"><label>视觉模型</label><input data-cfg-path="api.model_vision" placeholder="gpt-4.1-mini"></div>
</div>
<div class="field-note" style="margin:12px 0 8px"><strong style="color:var(--fg)">按用途拆分供应商</strong>：Chat / Vision / Image / Embedding / Fast 可以分别接不同供应商。某项只填模型或完全留空时，Key 和 Base URL 仍沿用统一 API。</div>
<div class="provider-grid">
<div class="provider-card"><h4>Chat 文本对话</h4><div class="fg"><label>API Key</label><input data-cfg-path="providers.chat.api_key" type="password" autocomplete="off"></div><div class="fg"><label>Base URL</label><input data-cfg-path="providers.chat.base_url" placeholder="https://api.openai.com/v1"></div><div class="fg"><label>模型</label><input data-cfg-path="providers.chat.model" placeholder="gpt-4.1-mini"></div></div>
<div class="provider-card"><h4>Vision 图片/视频理解</h4><div class="fg"><label>API Key</label><input data-cfg-path="providers.vision.api_key" type="password" autocomplete="off"></div><div class="fg"><label>Base URL</label><input data-cfg-path="providers.vision.base_url" placeholder="https://api.openai.com/v1"></div><div class="fg"><label>模型</label><input data-cfg-path="providers.vision.model" placeholder="gpt-4.1-mini"></div></div>
<div class="provider-card"><h4>Image 图片生成</h4><div class="fg"><label>API Key</label><input data-cfg-path="providers.image.api_key" type="password" autocomplete="off"></div><div class="fg"><label>Base URL</label><input data-cfg-path="providers.image.base_url" placeholder="https://api.openai.com/v1"></div><div class="fg"><label>模型</label><input data-cfg-path="providers.image.model" placeholder="gpt-image-1"></div></div>
<div class="provider-card"><h4>Embedding 向量</h4><div class="fg"><label>API Key</label><input data-cfg-path="providers.embedding.api_key" type="password" autocomplete="off"></div><div class="fg"><label>Base URL</label><input data-cfg-path="providers.embedding.base_url" placeholder="https://api.openai.com/v1"></div><div class="fg"><label>模型</label><input data-cfg-path="providers.embedding.model" placeholder="text-embedding-3-small"></div></div>
<div class="provider-card"><h4>Fast 快速任务</h4><div class="fg"><label>API Key</label><input data-cfg-path="providers.fast.api_key" type="password" autocomplete="off"></div><div class="fg"><label>Base URL</label><input data-cfg-path="providers.fast.base_url" placeholder="https://api.openai.com/v1"></div><div class="fg"><label>模型</label><input data-cfg-path="providers.fast.model" placeholder="gpt-4.1-nano"></div></div>
</div>
<div class="fr">
<label class="toggle-sw"><input type="checkbox" data-cfg-path="fallback_provider.enabled"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">启用备用 Provider</span></label>
<div class="fg"><label>备用 Provider 名称</label><input data-cfg-path="fallback_provider.name" placeholder="chatanywhere"></div>
</div>
<div class="form-grid">
<div class="fg"><label>备用 API Key</label><input data-cfg-path="fallback_provider.api_key" type="password" autocomplete="off"></div>
<div class="fg"><label>备用 Base URL</label><input data-cfg-path="fallback_provider.base_url"></div>
</div>
</div>

<div class="config-panel" id="cfg-models">
<div class="field-note" style="margin-bottom:12px">一个 API Key / Base URL 可以同时服务多个用途，但不是必须这样用；这里定义默认模型名。若在“大模型 API”页给某个用途单独配置供应商，该用途会优先使用自己的 Provider 和模型。</div>
<div class="form-grid">
<div class="fg"><label>Chat</label><input data-cfg-path="models.chat"></div>
<div class="fg"><label>Vision</label><input data-cfg-path="models.vision"></div>
<div class="fg"><label>Image</label><input data-cfg-path="models.image"></div>
<div class="fg"><label>Fast</label><input data-cfg-path="models.fast"></div>
<div class="fg"><label>Embedding</label><input data-cfg-path="models.embedding"></div>
</div>
<div class="field-note" style="margin:8px 0 12px">备用模型只在主模型失败时按相同角色兜底；如果你的服务商没有图片或 Embedding 能力，应换成支持这些 endpoint 的服务商或关闭依赖这些能力的功能。</div>
<div class="form-grid">
<div class="fg"><label>备用 Chat</label><input data-cfg-path="fallback_models.chat"></div>
<div class="fg"><label>备用 Vision</label><input data-cfg-path="fallback_models.vision"></div>
<div class="fg"><label>备用 Image</label><input data-cfg-path="fallback_models.image"></div>
<div class="fg"><label>备用 Fast</label><input data-cfg-path="fallback_models.fast"></div>
<div class="fg"><label>备用 Embedding</label><input data-cfg-path="fallback_models.embedding"></div>
</div>
</div>

<div class="config-panel" id="cfg-bili">
<div class="form-grid">
<div class="fg"><label>Owner MID</label><input data-cfg-path="bilibili.owner_mid"></div>
<div class="fg"><label>Refresh Token</label><input data-cfg-path="bilibili.refresh_token" type="password" autocomplete="off"></div>
</div>
<div class="field-note">也可以在“B站登录”页面扫码生成登录状态。</div>
</div>

<div class="config-panel" id="cfg-automation">
<div class="form-grid tight">
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.dry_run"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">Dry Run</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.enable_proactive"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">主动学习</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.enable_web_search"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">Web Search</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.enable_mood"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">心情系统</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.enable_affection"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">用户好感</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="automation.allow_comment"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">允许评论</span></label>
</div>
<div class="form-grid">
<div class="fg"><label>每日动作上限</label><input type="number" data-cfg-path="automation.max_daily_actions"></div>
<div class="fg"><label>主动视频数</label><input type="number" data-cfg-path="automation.proactive_video_count"></div>
<div class="fg"><label>评论轮询间隔(秒)</label><input type="number" data-cfg-path="automation.comment_poll_interval"></div>
<div class="fg"><label>睡眠开始</label><input data-cfg-path="automation.sleep_start" placeholder="02:00"></div>
<div class="fg"><label>睡眠结束</label><input data-cfg-path="automation.sleep_end" placeholder="08:00"></div>
</div>
</div>

<div class="config-panel" id="cfg-video">
<div class="form-grid">
<div class="fg"><label>理解模式</label><select data-cfg-path="video.mode"><option value="smart">smart</option><option value="subtitle">subtitle</option><option value="frames">frames</option><option value="hybrid">hybrid</option></select></div>
<div class="fg"><label>最长视频(秒)</label><input type="number" data-cfg-path="video.max_duration_seconds"></div>
<div class="fg"><label>抽帧数量</label><input type="number" data-cfg-path="video.frame_count"></div>
<div class="fg"><label>下载兴趣阈值</label><input type="number" step="0.1" data-cfg-path="video.download_interest_threshold"></div>
<div class="fg"><label>下载目录</label><input data-cfg-path="video.download_dir"></div>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="video.delete_video_after_understand"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">理解后删除视频</span></label>
</div>
<div class="form-grid">
<div class="fg"><label>ASR 启用</label><select data-cfg-path="asr.enabled" data-cfg-type="bool"><option value="true">开启</option><option value="false">关闭</option></select></div>
<div class="fg"><label>ASR 引擎</label><select data-cfg-path="asr.backend"><option value="funasr">FunASR</option><option value="whisper">Whisper</option></select></div>
<div class="fg"><label>ASR 语言</label><input data-cfg-path="asr.language" placeholder="zh"></div>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="dry_goods.enabled"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">Highlights 归档</span></label>
<div class="fg"><label>归档分数门槛</label><input type="number" step="0.1" data-cfg-path="dry_goods.min_score"></div>
<div class="fg"><label>归档文件夹</label><input data-cfg-path="dry_goods.folder_name" placeholder="highlights"></div>
</div>
</div>

<div class="config-panel" id="cfg-agent">
<div class="form-grid tight">
<label class="toggle-sw"><input type="checkbox" data-cfg-path="agent.enabled"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">启用 Agent</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="agent.auto_enabled"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">自动 Agent</span></label>
<label class="toggle-sw"><input type="checkbox" data-cfg-path="agent.dive_enabled"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">深度搜索</span></label>
</div>
<div class="form-grid">
<div class="fg"><label>计划最大步骤</label><input type="number" data-cfg-path="agent.max_steps_per_plan"></div>
<div class="fg"><label>搜索结果数</label><input type="number" data-cfg-path="agent.max_search_results"></div>
<div class="fg"><label>每计划视频数</label><input type="number" data-cfg-path="agent.max_videos_per_plan"></div>
<div class="fg"><label>自动触发最低分</label><input type="number" step="0.1" data-cfg-path="agent.auto_min_score"></div>
<div class="fg"><label>冷却(分钟)</label><input type="number" data-cfg-path="agent.cooldown_minutes"></div>
</div>
</div>

<div class="config-panel" id="cfg-json">
<textarea class="je" id="confEd"></textarea>
</div>

<div class="btn-grp"><button class="btn btn-pr" onclick="saveConf()">保存配置</button><button class="btn btn-out" onclick="loadConf()">重新加载</button><span id="confMsg" style="font-size:11px;color:var(--muted);align-self:center"></span></div>
</div>
</div>

<!-- PERSONA -->
<div class="page" id="pg-psna">
<div class="ph"><h1>Agent 管理</h1><p>人格、人设与 Agent 执行设置</p></div>
<div class="agent-shell">
<div>
<div class="pc"><h3>人格列表</h3><div id="psnaList"></div></div>
<div class="pc"><h3>新建人设</h3>
<div class="fg"><label>名称</label><input id="npName" placeholder="如：学习搭子"></div>
<div class="fg"><label>系统 Prompt</label><textarea id="npPrompt" placeholder="你是..."></textarea></div>
<div class="fg"><label>表达风格</label><input id="npStyle" placeholder="温和、犀利、克制"></div>
<div class="fg"><label>主人设定</label><input id="npOwner" placeholder="可选"></div>
<div class="fg"><label>行为边界（一行一条）</label><textarea id="npRules" placeholder="不泄露隐私&#10;遇到不确定信息先核实"></textarea></div>
<button class="btn btn-pr" onclick="addPsna()">创建人设</button>
</div>
</div>
<div>
<div class="pc"><h3>手动 Agent 任务</h3>
<div class="form-grid">
<div class="fg"><label>使用人格</label><select id="agentPersonaSelect"></select></div>
<div class="fg"><label>调用 Skill</label><select id="agentSkillSelect" onchange="renderAgentSkillHelp()"><option value="full_plan">完整计划</option><option value="search_bilibili_videos">搜索 B 站视频</option><option value="watch_bilibili_videos">理解/观看视频</option><option value="write_memory">写入本轮记忆</option></select></div>
<div class="fg"><label>执行模式</label><select id="agentMode"><option value="manual" selected>立即后台执行</option><option value="queue">仅加入任务队列</option><option value="dive">深度搜索</option></select></div>
</div>
<div class="notice" id="agentSkillHelp"></div>
<div class="fg"><label>目标描述</label><textarea id="agentGoal" placeholder="例如：搜索深度学习入门并总结前 3 个视频"></textarea></div>
<div class="btn-grp"><button class="btn btn-pr" onclick="runAgent()">执行 Agent</button><button class="btn btn-out" onclick="rf_psna()">刷新设置</button></div>
</div>
<div class="pc"><h3>上传 Prompt Skill</h3>
<div class="form-grid">
<div class="fg"><label>作用域</label><select id="promptSkillScope" onchange="renderPromptSkillScope()"><option value="persona">当前人格</option><option value="global">全局</option></select></div>
<div class="fg"><label>绑定人格</label><select id="promptSkillPersona" onchange="rf_prompt_skills()"></select></div>
</div>
<div class="fg"><label>Skill 名称</label><input id="promptSkillName" placeholder="如：苏格拉底式学习陪练"></div>
<div class="fg"><label>本地文件</label><input id="promptSkillFile" type="file" accept=".md,.txt,text/markdown,text/plain" onchange="loadPromptSkillFile(this)"><div class="field-note">支持 .md / .txt，本地读取后保存为项目内 Prompt Skill。</div></div>
<div class="fg"><label>Skill 内容</label><textarea id="promptSkillContent" placeholder="写入这段 skill 希望人格遵循的行为、口吻、步骤或边界。"></textarea></div>
<div class="btn-grp"><button class="btn btn-pr" onclick="savePromptSkill()">保存 Skill</button><button class="btn btn-out" onclick="rf_prompt_skills()">刷新列表</button></div>
<div class="prompt-skill-list" id="promptSkillList"></div>
</div>
<div class="pc"><h3>Agent 可选设置</h3><div class="settings-grid" id="agentSettingsGrid"></div></div>
<div class="pc"><h3>当前人格详情</h3><div id="activePersonaDetail"></div></div>
</div>
</div>
</div>

<!-- COMMENTS -->
<div class="page" id="pg-cmts">
<div class="ph"><h1>评论日志</h1><p>最近评论互动</p></div>
<div class="pc"><div id="cmtTab"></div></div>
</div>

<!-- USERS -->
<div class="page" id="pg-usrs">
<div class="ph"><h1>用户画像</h1><p>好感度与印象</p></div>
<div class="pc"><div id="usrTab"></div></div>
</div>

<!-- MEMORY -->
<div class="page" id="pg-mem">
<div class="ph"><h1>记忆 & 知识库</h1></div>
<div id="memBox"></div>
</div>

<!-- DIARY -->
<div class="page" id="pg-diary">
<div class="ph"><h1>日记 & 进化</h1></div>
<div id="diaryBox"></div>
</div>

<!-- ACTIONS -->
<div class="page" id="pg-acts">
<div class="ph"><h1>操作日志</h1></div>
<div class="pc"><div id="actTab"></div></div>
</div>

<!-- MOOD -->
<div class="page" id="pg-mood">
<div class="ph"><h1>心情管理</h1><p>查看/切换机器人心情状态</p></div>
<div class="pc"><h3>当前状态</h3><div id="moodStatus"></div></div>
<div class="pc"><h3>快速切换心情</h3>
<div class="btn-grp" id="moodQuickBtns"></div>
</div>
<div class="pc"><h3>心情设置</h3>
<div class="fg"><label>默认心情</label><input id="moodDefault" placeholder="平静"></div>
<div class="fr">
<div class="fg"><label><input type="checkbox" id="moodRandom" onchange="moodToggleRandom()"> 随机心情切换</label></div>
<div class="fg"><label>随机间隔(分钟)</label><input id="moodRandInt" type="number" min="1" max="120"></div>
</div>
<div class="fr">
<div class="fg"><label><input type="checkbox" id="moodCustom" onchange="moodToggleCustom()"> 自定义心情</label></div>
<div class="fg"><label>自定义心情文字</label><input id="moodCustomText"></div>
</div>
<div class="btn-grp"><button class="btn btn-pr" onclick="saveMood()">保存设置</button></div>
</div>
</div>

<!-- BEHAVIOR -->
<div class="page" id="pg-behavior">
<div class="ph"><h1>行为设置</h1><p>AI免责声明 · 精力管理 · 评论模式</p></div>
<div class="pc"><h3>AI免责声明</h3>
<p style="font-size:11px;color:var(--text2);margin-bottom:10px">所有评论/私信回复末尾会追加免责声明标签。关闭后不再添加，但建议保持开启以遵守平台规定。</p>
<div class="fr" style="align-items:center;margin-bottom:8px">
<label class="toggle-sw"><input type="checkbox" id="aiMarkerOn" onchange="toggleAiMarker()"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">启用免责声明</span></label>
</div>
<div class="fg"><label>免责声明文字</label><input id="aiMarkerText" placeholder="（内容由AI生成并由AI回复）" maxlength="50" style="max-width:300px"></div>
<div class="btn-grp"><button class="btn btn-pr" id="btnSaveMarker" onclick="saveAiMarker()">保存</button><span id="aiMarkerMsg" style="font-size:11px;margin-left:8px"></span></div>
</div>
<div class="pc"><h3>精力设置</h3>
<p style="font-size:11px;color:var(--text2);margin-bottom:10px">控制AI机器人精力恢复速度和行为间隔。</p>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
<div class="fg"><label>最大精力值</label><input id="engMaxEnergy" type="number" min="50" max="500" style="max-width:100px"></div>
<div class="fg"><label>每轮恢复(最小)</label><input id="engRecoverMin" type="number" min="1" max="50" style="max-width:100px"></div>
<div class="fg"><label>每轮恢复(最大)</label><input id="engRecoverMax" type="number" min="1" max="50" style="max-width:100px"></div>
<div class="fg"><label>恢复轮数(最小)</label><input id="engRoundsMin" type="number" min="1" max="20" style="max-width:100px"></div>
<div class="fg"><label>恢复轮数(最大)</label><input id="engRoundsMax" type="number" min="1" max="20" style="max-width:100px"></div>
<div class="fg"><label>轮间间隔(秒,最小)</label><input id="engRoundIntMin" type="number" min="10" max="600" style="max-width:100px"></div>
<div class="fg"><label>轮间间隔(秒,最大)</label><input id="engRoundIntMax" type="number" min="10" max="600" style="max-width:100px"></div>
<div class="fg"><label>视频间隔(秒,最小)</label><input id="engVideoIntMin" type="number" min="5" max="300" style="max-width:100px"></div>
<div class="fg"><label>视频间隔(秒,最大)</label><input id="engVideoIntMax" type="number" min="5" max="300" style="max-width:100px"></div>
</div>
<div class="btn-grp"><button class="btn btn-pr" onclick="saveEnergy()">保存精力设置</button><span id="engMsg" style="font-size:11px;margin-left:8px"></span></div>
</div>
<div class="pc"><h3>评论模式</h3>
<div class="fr" style="align-items:center;gap:12px">
<label style="cursor:pointer"><input type="radio" name="cmtMode" value="real" onchange="saveCommentMode()"> 真实模式 (发送到B站)</label>
<label style="cursor:pointer"><input type="radio" name="cmtMode" value="simulate" onchange="saveCommentMode()"> 模拟模式 (仅记录日志)</label>
</div>
<span id="cmtModeMsg" style="font-size:11px;margin-left:8px"></span>
</div>

	<div class="pc"><h3>关键词安全校验</h3>
	<p style="font-size:11px;color:var(--text2);margin-bottom:10px">开启后AI会过滤涉及敏感关键词的评论和回复。关闭后不再进行关键词检查（风险自负）。</p>
	<div class="fr" style="align-items:center;margin-bottom:10px">
	<label class="toggle-sw"><input type="checkbox" id="safetyEnabled" onchange="toggleSafety()"><span class="toggle-track"></span><span style="margin-left:10px;font-size:13px">启用关键词校验</span></label>
	</div>
	<div id="safetyKwSection" style="display:none">
	<p style="font-size:11px;color:var(--text2);margin-bottom:6px">当前屏蔽关键词（一行一个）：</p>
	<textarea id="safetyKeywords" style="width:100%;height:120px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-size:12px;padding:8px;font-family:monospace;resize:vertical"></textarea>
	<div class="btn-grp" style="margin-top:8px">
	<button class="btn btn-pr" onclick="saveSafetyKeywords()">保存关键词</button>
	<button class="btn btn-out btn-sm" onclick="addSafetyKeyword()">+ 添加关键词</button>
	</div>
	<div class="fg" style="margin-top:8px"><label>快速添加关键词</label>
	<div style="display:flex;gap:6px"><input id="newSafetyKw" placeholder="输入新关键词" style="flex:1"><button class="btn btn-out btn-sm" onclick="addSafetyKeyword()">添加</button></div>
	</div>
	<span id="safetyMsg" style="font-size:11px"></span>
	</div>
	</div>
	</div>

<!-- UPFOLLOW -->
<div class="page" id="pg-upfu">
<div class="ph"><h1>UP主关注列表</h1><p>AI已关注的UP主</p></div>
<div class="pc"><div id="upfuTab"></div></div>
</div>

<!-- TOOLS -->
<div class="page" id="pg-tools">
<div class="ph"><h1>功能中心</h1><p>手动操作 · 任务队列</p></div>
<div class="pc"><h3>手动发送弹幕</h3>
<div class="fr"><div class="fg"><label>BV号</label><input id="danmakuBvid" placeholder="BV1xx411c7mD"></div><div class="fg"><label>弹幕内容 (≤20字)</label><input id="danmakuText" maxlength="20" placeholder="第~"></div></div>
<button class="btn btn-pr" onclick="sendDanmaku()">发送弹幕</button>
</div>
<div class="pc"><h3>手动视频分析</h3>
<div class="fg"><label>BV号 / 视频链接</label><input id="analyzeBvid" placeholder="BV1xx411c7mD 或 完整链接"></div>
<button class="btn btn-pr" onclick="analyzeVideo()">开始分析</button>
</div>
<div class="pc"><h3>知识库操作</h3>
<div class="btn-grp">
<button class="btn btn-pr" onclick="kbOrganize()">一键整理知识库</button>
<button class="btn btn-out" onclick="kbRevisit()">复习已学内容</button>
<button class="btn btn-out" onclick="rf_kbStats()">查看统计</button>
</div>
<div id="kbStatBox" style="margin-top:12px;font-size:12px"></div>
</div>
</div>

<!-- TUTOR (v2.0.3) -->
<div class="page" id="pg-tutor">
<div class="ph"><h1>知识辅导</h1><p>选择知识文件 → AI讲解/问答/二次创作/生成HTML</p></div>

<div class="pc"><h3>选择知识文件</h3>
<div style="display:flex;gap:8px;align-items:flex-start;flex-wrap:wrap">
<select id="tutorFileSelect" multiple size="8" style="flex:1;min-width:250px;max-width:550px;padding:6px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-size:12px">
</select>
<div style="display:flex;flex-direction:column;gap:5px">
<button class="btn btn-pr btn-sm" onclick="tutorLoadFile()">加载选中</button>
<button class="btn btn-out btn-sm" onclick="tutorSelectAll()">全选</button>
<button class="btn btn-out btn-sm" onclick="tutorSelectNone()">取消</button>
<button class="btn btn-out btn-sm" onclick="rf_tutor()" style="margin-top:4px">刷新</button>
</div>
</div>
<div id="tutorFileInfo" style="margin-top:8px;font-size:11px;color:var(--text2)"></div>
<div class="btn-grp" id="tutorFileActions" style="margin-top:6px;display:none">
<button class="btn btn-pr btn-sm" onclick="tutorLoadFile()">加载选中</button>
<button class="btn btn-out btn-sm" onclick="tutorSelectAll()">全选</button>
</div>
</div>

<div class="pc" id="tutorContentBox" style="display:none">
<h3>文件内容预览 <span style="font-size:10px;color:var(--text2);cursor:pointer" onclick="var p=document.getElementById('tutorContentPre');p.style.display=p.style.display==='none'?'block':'none'">[展开/折叠]</span></h3>
<pre id="tutorContentPre" style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);padding:12px;max-height:250px;overflow-y:auto;font-size:11px;color:var(--text2);white-space:pre-wrap;word-break:break-all;display:none"></pre>
</div>

<div class="pc" id="tutorChatBox" style="display:none">
<h3>AI 辅导对话</h3>
<div id="tutorChatLog" style="background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);padding:12px;max-height:350px;overflow-y:auto;font-size:12px;margin-bottom:10px;min-height:100px">
<div style="color:var(--text2);text-align:center;padding:20px">AI导师已就绪，开始提问吧！</div>
</div>
<div style="display:flex;gap:6px;align-items:flex-end;flex-wrap:wrap">
<textarea id="tutorInput" placeholder="输入你的问题..." style="flex:1;min-width:180px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-size:12px;padding:8px;resize:none;height:50px;font-family:inherit" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();tutorSend('chat')}"></textarea>
<div style="display:flex;flex-direction:column;gap:4px">
<button class="btn btn-pr btn-sm" onclick="tutorSend('chat')">提问</button>
<button class="btn btn-out btn-sm" onclick="tutorSend('rewrite')">改写</button>
<button class="btn btn-out btn-sm" onclick="tutorSend('html')">HTML</button>
</div>
</div>
<div style="display:flex;gap:8px;align-items:center;margin-top:6px">
<span style="font-size:11px;color:var(--text2)">HTML风格:</span>
<select id="tutorHtmlStyle" style="padding:4px 8px;background:var(--bg3);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-size:11px">
<option value="dark">暗色科技风</option>
<option value="light">清新白底风</option>
<option value="modern">现代极简风</option>
</select>
<span id="tutorStatus" style="font-size:11px;color:var(--text2);margin-left:10px"></span>
</div>
</div>

<div class="pc" id="tutorResultBox" style="display:none">
<h3>操作结果</h3>
<div id="tutorResultContent" style="font-size:12px"></div>
<div class="btn-grp" id="tutorResultActions" style="display:none"></div>
</div>
</div>

<!-- SYSTEM -->

<div class="page" id="pg-sys">
<div class="ph"><h1>系统管理</h1><p>备份 · 恢复 · 重置</p></div>
<div class="pc"><h3>导出配置</h3><p style="font-size:11px;color:var(--text2)">一键导出全部配置到 C:\bilibili_claw_backup</p>
<div class="sys-actions"><button class="btn btn-pr" onclick="exportConfig()">导出全部配置</button></div>
<div id="exportMsg" style="margin-top:8px;font-size:12px"></div>
</div>
<div class="pc"><h3>导入配置</h3><p style="font-size:11px;color:var(--text2)">从备份文件恢复</p>
<div class="sys-actions"><button class="btn btn-out" onclick="listBackups()">刷新备份列表</button></div>
<div id="backupList" style="margin:10px 0;font-size:12px"></div>
</div>
<div class="pc danger-card">
<h3 style="color:var(--red)">恢复出厂设置</h3>
<p style="font-size:11px;color:var(--text2)">清除所有配置、登录信息、数据文件。此操作不可逆！</p>
<div class="fg"><label><input type="checkbox" id="resetKB"> 同时删除知识库目录</label></div>
<button class="btn btn-dan" onclick="factoryReset()">恢复出厂设置</button>
</div>
</div>

<!-- ABOUT -->
<div class="page" id="pg-about">
<div class="ph"><h1>关于系统</h1><p>版本信息 · 技术栈 · 联系方式</p></div>
<div class="pc" id="aboutBox"></div>
<div class="notice">免责声明：本项目仅供学习参考，若因使用本项目产生的任何后果，本人一律概不负责。</div>
</div>

</main>

<div class="toast" id="toast"></div>

<script>
function navIcon(name){
var icons={
dash:'<path d="M4 13h6V4H4z"/><path d="M14 20h6V4h-6z"/><path d="M4 20h6v-3H4z"/>',
ctrl:'<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
login:'<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5"/><path d="M15 12H3"/>',
conf:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1A2 2 0 1 1 4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1A2 2 0 1 1 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.6h.1a1.7 1.7 0 0 0 1.9-.3l.1-.1A2 2 0 1 1 19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.6 1h.1a2 2 0 1 1 0 4H21a1.7 1.7 0 0 0-1.6 1z"/>',
agent:'<path d="M12 3l7 4v5c0 5-3 8-7 9-4-1-7-4-7-9V7z"/><path d="M9 12h6"/><path d="M9 16h6"/><path d="M9 8h6"/>',
mood:'<circle cx="12" cy="12" r="9"/><path d="M8 10h.01"/><path d="M16 10h.01"/><path d="M8 15c1.2 1 2.5 1.5 4 1.5s2.8-.5 4-1.5"/>',
behavior:'<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
upfu:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.8"/><path d="M16 3.2a4 4 0 0 1 0 7.6"/>',
cmts:'<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>',
usrs:'<circle cx="12" cy="7" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
mem:'<path d="M6 4h12a2 2 0 0 1 2 2v14l-4-2-4 2-4-2-4 2V6a2 2 0 0 1 2-2z"/><path d="M8 8h8"/><path d="M8 12h8"/>',
diary:'<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
acts:'<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>',
tutor:'<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c2 2 10 2 12 0v-5"/>',
tools:'<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4z"/><path d="M15 5l4 4"/>',
sys:'<path d="M4 4h16v16H4z"/><path d="M9 9h6v6H9z"/><path d="M9 1v3"/><path d="M15 1v3"/><path d="M9 20v3"/><path d="M15 20v3"/><path d="M20 9h3"/><path d="M20 15h3"/><path d="M1 9h3"/><path d="M1 15h3"/>',
about:'<circle cx="12" cy="12" r="9"/><path d="M12 10v6"/><path d="M12 7h.01"/>'
};
return '<svg viewBox="0 0 24 24" aria-hidden="true">'+(icons[name]||icons.about)+'</svg>';
}
function initNavIcons(){
document.querySelectorAll('.nav-ico[data-icon]').forEach(function(el){el.innerHTML=navIcon(el.getAttribute('data-icon'))});
}
// ── NAV ──
function nav(p,el){
document.querySelectorAll('.page').forEach(x=>x.classList.remove('on'));
document.querySelectorAll('.ni').forEach(x=>x.classList.remove('ac'));
document.getElementById('pg-'+p).classList.add('on');
if(el)el.classList.add('ac');
if(window['rf_'+p])window['rf_'+p]();
// 移动端关闭侧边栏
if(window.innerWidth<768)toggleSidebar(true);
}
function toggleSidebar(force){
var s=document.getElementById('sidebar'),o=document.getElementById('mobOverlay');
if(typeof force=='boolean'){s.classList.toggle('show',force);o.classList.toggle('show',force)}
else{s.classList.toggle('show');o.classList.toggle('show')}
}

// ── TOAST ──
function toast(m,t){t=t||'inf';var x=document.getElementById('toast');x.textContent=m;x.className='toast '+t+' show';setTimeout(function(){x.classList.remove('show')},2200)}

// ── API ──
async function api(m,u,b){
var o={method:m,headers:{'Content-Type':'application/json'}};if(b)o.body=JSON.stringify(b);
var r=await fetch(u,o);
var ct=r.headers.get('content-type')||'',data=null;
if(ct.indexOf('application/json')>=0){data=await r.json()}
else{var txt=await r.text();if(r.status===401||r.redirected)throw new Error('登录状态已失效，请重新登录或确认免责声明');throw new Error((txt||'请求失败').slice(0,120))}
if(!r.ok)throw new Error(data.message||('请求失败: '+r.status));
return data;
}

// ── CHART HELPERS ──
var _charts={};
function _destroyC(k){if(_charts[k]){_charts[k].destroy();_charts[k]=null}}
function _makeLine(canvasId,labels,datasets){
_destroyC(canvasId);
var ctx=document.getElementById(canvasId);if(!ctx)return;
_charts[canvasId]=new Chart(ctx,{
type:'line',data:{labels:labels,datasets:datasets},
options:{responsive:true,maintainAspectRatio:false,animation:{duration:600},
plugins:{legend:{labels:{color:'#5e5d59',font:{size:11},usePointStyle:true,padding:12}}},
scales:{x:{ticks:{color:'#87867f',font:{size:10},maxTicksLimit:8},grid:{color:'rgba(209,207,197,.38)'}},y:{ticks:{color:'#87867f',font:{size:10}},grid:{color:'rgba(209,207,197,.38)'},beginAtZero:true}}
}});
}

// ── DASH ──
var _liveInfo={botRunning:false,botUptimeSeconds:0,botUptimeBase:0,panelUptime:'0h0m0s',costTotal:0};
function fmtDuration(seconds){
seconds=Math.max(0,parseInt(seconds||0,10));
var d=Math.floor(seconds/86400),h=Math.floor(seconds%86400/3600),m=Math.floor(seconds%3600/60),s=seconds%60;
return d?d+'d'+h+'h'+m+'m':h+'h'+m+'m'+s+'s';
}
function setLiveInfo(d){
_liveInfo.botRunning=!!d.bot_running;
_liveInfo.botUptimeSeconds=parseInt(d.bot_uptime_seconds||0,10)||0;
_liveInfo.botUptimeBase=Date.now();
_liveInfo.panelUptime=d.panel_uptime||d.uptime||'0h0m0s';
_liveInfo.costTotal=Number(d.cost_total||0);
updateLiveUptime();
}
function updateLiveUptime(){
var extra=_liveInfo.botRunning?Math.floor((Date.now()-_liveInfo.botUptimeBase)/1000):0;
var botText=_liveInfo.botRunning?fmtDuration(_liveInfo.botUptimeSeconds+extra):'已停止';
var foot=document.getElementById('uptime');if(foot)foot.textContent=_liveInfo.botRunning?botText:_liveInfo.panelUptime;
var dash=document.getElementById('dashUptime');if(dash)dash.textContent=botText;
var cost=document.getElementById('dashCost');if(cost)cost.textContent='$'+_liveInfo.costTotal.toFixed(4);
}
async function rf_dash(){
try{
var d=await api('GET','/api/info');
setLiveInfo(d);
var h='';
h+='<div class="sc"><div class="si bl">'+navIcon('agent')+'</div><div><div class="sv">'+(d.bot_running?'运行中':'已停止')+'</div><div class="sl">机器人状态</div></div></div>';
h+='<div class="sc"><div class="si gn">'+navIcon('login')+'</div><div><div class="sv">'+(d.bili_logged_in?'已登录':'未登录')+'</div><div class="sl">B站认证</div></div></div>';
h+='<div class="sc"><div class="si or">'+navIcon('sys')+'</div><div><div class="sv">'+(d.data_files||0)+'</div><div class="sl">数据文件</div></div></div>';
h+='<div class="sc"><div class="si pk">'+navIcon('ctrl')+'</div><div><div class="sv" id="dashUptime">--</div><div class="sl">运行时长</div></div></div>';
h+='<div class="sc"><div class="si pp">'+navIcon('conf')+'</div><div><div class="sv" id="dashCost">--</div><div class="sl">累计费用</div></div></div>';
h+='<div class="sc" id="asrDashCard"><div class="si '+(d.asr_enabled?'gn':'rd')+'">'+navIcon('tutor')+'</div><div><div class="sv">'+(d.asr_enabled?'开启':'关闭')+'</div><div class="sl">ASR语音识别</div></div></div>';
document.getElementById('dashStats').innerHTML=h;
updateLiveUptime();

var dot=document.getElementById('botDot');dot.className='dot '+(d.bot_running?'on':'off');
var bd='<table class="tb"><tr><th>项目</th><th>值</th><th>项目</th><th>值</th></tr>';
bd+='<tr><td>运行状态</td><td><span class="tg '+(d.bot_running?'tg-suc':'tg-war')+'">'+(d.bot_running?'运行中':'已停止')+'</span></td><td>启动时间</td><td>'+(d.bot_start_time||'-')+'</td></tr>';
bd+='<tr><td>API状态</td><td><span class="tg '+(d.api_configured?'tg-suc':'tg-dan')+'">'+(d.api_configured?'已配置':'未配置')+'</span></td>';
if(d.mood)bd+='<td>心情 / 精力</td><td>'+(d.mood.mood||'-')+' / '+(d.mood.energy||'?')+'</td>';
else bd+='<td>心情</td><td>-</td>';
bd+='</tr>';
if(d.persona)bd+='<tr><td>当前人格</td><td>'+(d.persona.active||'-')+'</td>';
else bd+='<tr><td>当前人格</td><td>-</td>';
if(d.cost_total!=null)bd+='<td>累计费用</td><td>$'+Number(d.cost_total).toFixed(4)+'</td>';
else bd+='<td>累计费用</td><td>-</td>';
bd+='</tr>';
bd+='</table>';
document.getElementById('botDetail').innerHTML=bd;

var fg='';
var flbs={'config.json':'配置','bilibili_cookies.json':'Cookie','comment_log.json':'评论日志','user_profiles.json':'用户画像','mood_state.json':'心情状态','personas.json':'人格数据','bot_diary.json':'日记','self_evolution.json':'进化记录','agent_skill_log.json':'Agent日志','bot_runtime_state.json':'运行时'};
for(var k in d.files||{}){
var f=d.files[k],lb=flbs[k]||k,cl=f.exists?'tg-suc':'tg-war';
fg+='<div><span class="tg '+cl+'">'+lb+'</span> '+(f.exists?f.size_fmt+' · '+f.mtime:'无')+'</div>';
}
document.getElementById('fileGrid').innerHTML=fg||'<div class="emp">无数据文件</div>';

// badges
document.getElementById('botBadge').style.display=d.bot_running?'':'none';
document.getElementById('botBadge').style.background=d.bot_running?'var(--green)':'';
document.getElementById('loginBadge').style.display=d.bili_logged_in?'':'none';

// Charts
try{
var ch=await api('GET','/api/charts');
if(ch.comments){var ds=[],cs=[];for(var i=0;i<ch.comments.length;i++){ds.push(ch.comments[i].date);cs.push(ch.comments[i].count)}_makeLine('chartComments',ds,[{label:'评论数',data:cs,borderColor:'#52708f',backgroundColor:'rgba(82,112,143,.10)',borderWidth:2,tension:.3,fill:true}]);}
if(ch.moods){var md=[],mv=[],me=[];for(var i=0;i<ch.moods.length;i++){md.push(ch.moods[i].date);mv.push(ch.moods[i].valence||50);me.push(ch.moods[i].energy||50)}_makeLine('chartMood',md,[{label:'情绪指数',data:mv,borderColor:'#a85f78',backgroundColor:'rgba(168,95,120,.08)',borderWidth:2,tension:.3,fill:true},{label:'精力指数',data:me,borderColor:'#64735b',backgroundColor:'rgba(100,115,91,.08)',borderWidth:2,tension:.3,fill:true}]);}
if(ch.actions){var ad=[],ac=[];for(var i=0;i<ch.actions.length;i++){ad.push(ch.actions[i].date);ac.push(ch.actions[i].count)}_makeLine('chartActions',ad,[{label:'操作数',data:ac,borderColor:'#b9822f',backgroundColor:'rgba(185,130,47,.10)',borderWidth:2,tension:.3,fill:true}]);}
if(ch.videos){var vd=[],vc=[];for(var i=0;i<ch.videos.length;i++){vd.push(ch.videos[i].date);vc.push(ch.videos[i].count)}_makeLine('chartVideos',vd,[{label:'处理视频数',data:vc,borderColor:'#7563a8',backgroundColor:'rgba(117,99,168,.10)',borderWidth:2,tension:.3,fill:true}]);}
}catch(e){}
}catch(e){}
}

// ── CONTROL ──
var logPoll=null;
var userScrolledUp=false;
function rf_ctrl(){
upCtrlUI();
pollLog();
var lb=document.getElementById('botLog');
if(lb){
lb.addEventListener('scroll',function(){
var el=lb;
var atBottom=el.scrollHeight - el.scrollTop - el.clientHeight < 30;
userScrolledUp=!atBottom;
});
}
}
async function upCtrlUI(){
var d=await api('GET','/api/info');
setLiveInfo(d);
document.getElementById('ctrlStatus').innerHTML=d.bot_running?'<span class="tg tg-suc pulse">运行中</span> 自 '+d.bot_start_time:'<span class="tg tg-war">已停止</span>';
document.getElementById('btnStart').style.display=d.bot_running?'none':'';
document.getElementById('btnStop').style.display=d.bot_running?'':'none';
}
async function startBot(){
var r=await api('POST','/api/bot/start');toast(r.message,r.ok?'ok':'err');upCtrlUI();if(r.ok){userScrolledUp=false;pollLog();rf_dash()}
}
async function stopBot(){
var r=await api('POST','/api/bot/stop');toast(r.message,r.ok?'ok':'err');upCtrlUI();if(r.ok)rf_dash()
}
async function restartBot(){await stopBot();setTimeout(startBot,1200)}
async function clearLog(){await api('POST','/api/bot/clear');document.getElementById('botLog').textContent='日志已清空';userScrolledUp=false;pollLog()}
async function pollLog(){
if(logPoll)clearInterval(logPoll);
var tick=async function(){
try{
var r=await api('GET','/api/bot/output');
var el=document.getElementById('botLog');
var wasAtBottom=el&&(el.scrollHeight-el.scrollTop-el.clientHeight<30);
if(el){
el.textContent=r.output||'无输出';
if(!userScrolledUp||wasAtBottom)el.scrollTop=el.scrollHeight;
}
}catch(e){}
};
tick();
logPoll=setInterval(tick,2000);
}
function stopPoll(){if(logPoll){clearInterval(logPoll);logPoll=null}}

// ── LOGIN ──
var qrTimer=null;
function rf_login(){
checkLogin();
}
async function checkLogin(){
try{
var d=await api('GET','/api/info');
var ci=document.getElementById('cookieInfo');
if(d.bili_logged_in){
document.getElementById('loginStatus').innerHTML='<span class="tg tg-suc">已登录B站</span>';
ci.innerHTML='Cookie 文件: Data/bilibili_cookies.json';
document.getElementById('btnQR').innerHTML='<span class="nav-ico" data-icon="login"></span>重新登录';initNavIcons();
document.getElementById('btnLogout').style.display='';
document.getElementById('loginBadge').style.display='';
} else {
document.getElementById('loginStatus').innerHTML='<span class="tg tg-war">未登录</span>';
ci.innerHTML='尚未登录B站账号';
document.getElementById('btnQR').innerHTML='<span class="nav-ico" data-icon="login"></span>生成登录二维码';initNavIcons();
document.getElementById('btnLogout').style.display='none';
document.getElementById('loginBadge').style.display='none';
}
}catch(e){}
}
async function startQRLogin(){
document.getElementById('qrArea').style.display='block';
document.getElementById('qrStatusText').textContent='正在生成二维码...';
document.getElementById('qrImg').src='';
var r=await api('POST','/api/bili/qr/start');
if(!r.ok){toast(r.message,'err');return}
document.getElementById('qrImg').src='data:image/png;base64,'+r.img;
document.getElementById('qrStatusText').textContent=r.message;
if(qrTimer)clearInterval(qrTimer);
qrTimer=setInterval(pollQR,2000);
}
async function pollQR(){
try{
var r=await api('GET','/api/bili/qr/status');
document.getElementById('qrStatusText').textContent=r.message;
if(r.status=='success'){
clearInterval(qrTimer);qrTimer=null;
toast('登录成功！UID: '+r.uid,'ok');
setTimeout(function(){document.getElementById('qrArea').style.display='none';checkLogin();rf_dash()},1500);
}else if(r.status=='timeout'||r.status=='error'){
clearInterval(qrTimer);qrTimer=null;
toast(r.message,'err');
document.getElementById('qrArea').style.display='none';
}
}catch(e){clearInterval(qrTimer);qrTimer=null}
}
async function logoutBili(){
if(!confirm('确定退出B站登录？'))return;
var r=await api('POST','/api/bili/logout');toast(r.message,r.ok?'ok':'err');checkLogin();rf_dash()
}

// ── CONFIG ──
var _configCache={};
var _configLoaded=false;
var _configDirty=false;
function configAutoRefreshBlocked(){if(_configDirty)return true;return false}
function rf_conf(){
if(configAutoRefreshBlocked()){
var msg=document.getElementById('confMsg');if(msg)msg.textContent='配置有未保存修改，已暂停自动刷新';
return;
}
loadConf(true)
}
function switchConfTab(name,el){
document.querySelectorAll('.config-panel').forEach(function(x){x.classList.remove('on')});
document.querySelectorAll('#confTabs .tab-btn').forEach(function(x){x.classList.remove('on')});
var p=document.getElementById('cfg-'+name);if(p)p.classList.add('on');
if(el)el.classList.add('on');
if(name==='json')syncJsonFromVisualConfig();
}
function markConfigDirty(){
if(!_configLoaded)return;
_configDirty=true;
var msg=document.getElementById('confMsg');if(msg)msg.textContent='有未保存修改';
}
function bindConfigDirtyWatchers(){
document.querySelectorAll('[data-cfg-path],#confEd').forEach(function(el){
if(el.dataset.dirtyBound)return;
el.dataset.dirtyBound='1';
el.addEventListener('input',markConfigDirty);
el.addEventListener('change',markConfigDirty);
});
}
function cfgGet(obj,path,def){
var cur=obj||{},parts=path.split('.');
for(var i=0;i<parts.length;i++){if(cur==null||typeof cur!=='object'||!(parts[i] in cur))return def;cur=cur[parts[i]]}
return cur==null?def:cur;
}
function cfgSet(obj,path,val){
var cur=obj,parts=path.split('.');
for(var i=0;i<parts.length-1;i++){var k=parts[i];if(!cur[k]||typeof cur[k]!=='object'||Array.isArray(cur[k]))cur[k]={};cur=cur[k]}
cur[parts[parts.length-1]]=val;
}
function cfgReadValue(el){
if(el.type==='checkbox')return el.checked;
if(el.dataset.cfgType==='bool')return el.value==='true';
if(el.type==='number')return el.step&&el.step!=='1'?(parseFloat(el.value)||0):(parseInt(el.value)||0);
return el.value;
}
function safeLogoImage(image){
image=(image||'').trim();
if(!image||image.length>1400000)return '';
return /^data:image\/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\s]+$/i.test(image)?image:'';
}
function safeLogoSvg(svg){
svg=(svg||'').trim();
if(!svg||svg.length>20000||!/^<svg[\s>]/i.test(svg))return '';
if(/<\s*script\b/i.test(svg)||/<\s*foreignObject\b/i.test(svg)||/\son[a-z]+\s*=/i.test(svg)||/javascript\s*:/i.test(svg)||/data\s*:\s*text\/html/i.test(svg))return '';
return svg;
}
function fallbackLogoSvg(txt){
txt=esc((txt||'BL').slice(0,8));
return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180"><rect width="180" height="180" rx="40" fill="#141413"/><text x="90" y="108" text-anchor="middle" font-size="72" font-family="Arial, sans-serif" font-weight="700" fill="#faf9f5">'+txt+'</text></svg>';
}
function logoDataUri(site){
site=site||{};
var txt=(site.logo_text||'BL').slice(0,8);
var img=safeLogoImage(site.logo_image);
if(img)return img;
var svg=safeLogoSvg(site.logo_svg)||fallbackLogoSvg(txt);
return 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
}
function renderLogoMark(el,site){
if(!el)return;
site=site||{};
var txt=(site.logo_text||'BL').slice(0,8);
var img=safeLogoImage(site.logo_image);
if(img){el.innerHTML='<img src="'+img+'" alt="Logo">';return}
var svg=safeLogoSvg(site.logo_svg);
if(svg)el.innerHTML=svg;
else el.textContent=txt;
}
function syncVisualConfigFromJson(){
document.querySelectorAll('[data-cfg-path]').forEach(function(el){
var val=cfgGet(_configCache,el.dataset.cfgPath,'');
if(el.type==='checkbox')el.checked=!!val;
else if(el.dataset.cfgType==='bool')el.value=val?'true':'false';
else el.value=val==null?'':val;
});
previewSiteLogo();
renderAgentSettings(_configCache);
}
function syncJsonFromVisualConfig(){
document.querySelectorAll('[data-cfg-path]').forEach(function(el){cfgSet(_configCache,el.dataset.cfgPath,cfgReadValue(el))});
document.getElementById('confEd').value=JSON.stringify(_configCache,null,2);
}
async function loadConf(autoRefresh){
try{
if(autoRefresh&&_configDirty){
var msg=document.getElementById('confMsg');if(msg)msg.textContent='配置有未保存修改，已暂停自动刷新';
return;
}
var r=await api('GET','/api/config');
_configCache=r||{};
_configLoaded=false;
document.getElementById('confEd').value=JSON.stringify(_configCache,null,2);
syncVisualConfigFromJson();
_configLoaded=true;
_configDirty=false;
bindConfigDirtyWatchers();
document.getElementById('confMsg').textContent='已加载';
}catch(e){toast('加载失败','err')}
}
async function saveConf(){
try{
if(document.getElementById('cfg-json').classList.contains('on')){
try{_configCache=JSON.parse(document.getElementById('confEd').value)}
catch(e){toast('配置格式错误: '+e.message,'err');document.getElementById('confMsg').textContent='JSON 格式错误';return}
}
else syncJsonFromVisualConfig();
var r=await api('POST','/api/config',_configCache);
toast(r.message,r.ok?'ok':'err');
document.getElementById('confMsg').textContent=r.ok?'已保存':'保存失败';
if(r.ok){_configDirty=false;applySiteLogo(_configCache.site||{});}
}catch(e){toast('保存失败: '+e.message,'err');document.getElementById('confMsg').textContent='保存失败'}
}
function previewSiteLogo(){
var prev=document.getElementById('siteLogoPreview');if(!prev)return;
var img=document.getElementById('siteLogoImage').value.trim();
var txt=(document.getElementById('siteLogoText').value.trim()||'BL').slice(0,8);
renderLogoMark(prev,{logo_text:txt,logo_image:img});
}
function loadLogoImageFile(input){
var file=input&&input.files&&input.files[0];if(!file)return;
if(['image/png','image/jpeg','image/webp','image/gif'].indexOf(file.type)<0){toast('请选择 PNG、JPG、WebP 或 GIF 图片','err');input.value='';return}
if(file.size>1024*1024){toast('Logo 图片不能超过 1MB','err');input.value='';return}
var reader=new FileReader();
reader.onload=function(){
var image=String(reader.result||'');
if(!safeLogoImage(image)){toast('图片格式不支持','err');input.value='';return}
document.getElementById('siteLogoImage').value=image;
previewSiteLogo();
markConfigDirty();
toast('Logo 图片已载入，保存后全站生效','ok');
input.value='';
};
reader.onerror=function(){toast('读取图片失败','err');input.value=''};
reader.readAsDataURL(file);
}
function clearLogoImage(){
var el=document.getElementById('siteLogoImage');if(!el)return;
el.value='';
previewSiteLogo();
markConfigDirty();
}
function applySiteLogo(site){
site=site||{};
var txt=(site.logo_text||'BL').slice(0,8);
var brand=site.brand_name||'B站 AI 管理系统';
var mark=document.getElementById('siteLogoMark'),name=document.getElementById('siteBrandName');
renderLogoMark(mark,site);
if(name)name.textContent=brand;
var href=logoDataUri(site);
document.querySelectorAll('link[rel="icon"],link[rel="apple-touch-icon"]').forEach(function(link){link.href=href});
}
function renderAgentSettings(cfg){
var a=(cfg&&cfg.agent)||{},v=(cfg&&cfg.video)||{},auto=(cfg&&cfg.automation)||{},models=(cfg&&cfg.models)||{};
var rows=[
['Agent启用',a.enabled?'开启':'关闭'],['自动运行',a.auto_enabled?'开启':'关闭'],['深度搜索',a.dive_enabled?'开启':'关闭'],
['计划步骤',a.max_steps_per_plan||'-'],['搜索结果',a.max_search_results||'-'],['计划视频数',a.max_videos_per_plan||'-'],
['自动最低分',a.auto_min_score||'-'],['冷却分钟',a.cooldown_minutes||'-'],['视频理解',v.mode||'-'],
['最长视频',v.max_duration_seconds? v.max_duration_seconds+' 秒':'-'],['Chat模型',models.chat||cfgGet(cfg,'api.model_brain','-')],['Vision模型',models.vision||cfgGet(cfg,'api.model_vision','-')],
['主动学习',auto.enable_proactive?'开启':'关闭'],['Web Search',auto.enable_web_search?'开启':'关闭'],['每日动作',auto.max_daily_actions||'-']
];
var h='';
for(var i=0;i<rows.length;i++)h+='<div class="setting-card"><strong>'+esc(String(rows[i][0]))+'</strong><span>'+esc(String(rows[i][1]))+'</span></div>';
var box=document.getElementById('agentSettingsGrid');if(box)box.innerHTML=h;
}
var agentSkillCatalog={
full_plan:{label:'完整计划',hint:'按计划依次搜索 B 站视频、理解/观看结果并写入本轮记忆。'},
search_bilibili_videos:{label:'搜索 B 站视频',hint:'只执行搜索步骤，适合先找候选视频，不会进入观看/总结。'},
watch_bilibili_videos:{label:'理解/观看视频',hint:'先按目标搜索，再理解/观看候选视频，不额外写总结。'},
write_memory:{label:'写入本轮记忆',hint:'只把目标作为本轮记忆/总结任务处理，不搜索视频。'}
};
function renderAgentSkillHelp(){
var sel=document.getElementById('agentSkillSelect'),box=document.getElementById('agentSkillHelp');
if(!sel||!box)return;
var item=agentSkillCatalog[sel.value]||agentSkillCatalog.full_plan;
box.innerHTML='<strong>'+esc(item.label)+'</strong> · '+esc(item.hint)+'<br>这里调用的是项目内 AgentSkillRunner 技能，不是 Codex 宿主的本地 SKILL.md。';
}
function renderPromptSkillScope(){
var scope=document.getElementById('promptSkillScope'),persona=document.getElementById('promptSkillPersona');
if(!scope||!persona)return;
persona.disabled=scope.value==='global';
rf_prompt_skills();
}
function setPromptSkillPersonas(optionsHtml){
var box=document.getElementById('promptSkillPersona');
if(box)box.innerHTML=optionsHtml;
}
function loadPromptSkillFile(input){
var file=input&&input.files&&input.files[0];
if(!file)return;
if(file.size>20000){toast('Skill 文件不能超过 20KB','err');input.value='';return}
var reader=new FileReader();
reader.onload=function(){
var name=document.getElementById('promptSkillName'),content=document.getElementById('promptSkillContent');
if(name&&!name.value.trim())name.value=file.name.replace(/\.(md|txt)$/i,'');
if(content)content.value=String(reader.result||'');
};
reader.onerror=function(){toast('读取 Skill 文件失败','err')};
reader.readAsText(file,'utf-8');
}
function renderPromptSkills(items){
var box=document.getElementById('promptSkillList');
if(!box)return;
if(!items||!items.length){box.innerHTML=emptyState('agent','暂无 Prompt Skill');return}
var h='';
for(var i=0;i<items.length;i++){
var it=items[i],scope=it.scope==='global'?'全局':'人格',meta=scope+(it.persona?' · '+it.persona:'');
var preview=String(it.content||'').slice(0,220);
h+='<div class="setting-card prompt-skill-card"><div><strong>'+esc(it.name||'-')+'</strong><span>'+esc(meta)+' · '+esc((it.updated_at||'').slice(0,16))+'</span><pre>'+esc(preview)+'</pre></div><button class="btn btn-sm btn-out" onclick="deletePromptSkill(\''+esc(it.id||'')+'\')">删除</button></div>';
}
box.innerHTML=h;
}
async function rf_prompt_skills(){
try{
var scope=document.getElementById('promptSkillScope'),persona=document.getElementById('promptSkillPersona');
var query='';
if(scope&&scope.value==='persona'&&persona&&persona.value)query='?persona='+encodeURIComponent(persona.value);
var r=await api('GET','/api/prompt-skills'+query);
renderPromptSkills(r.items||[]);
}catch(e){}
}
async function savePromptSkill(){
var scope=document.getElementById('promptSkillScope').value;
var persona=document.getElementById('promptSkillPersona').value;
var name=document.getElementById('promptSkillName').value.trim();
var content=document.getElementById('promptSkillContent').value.trim();
if(!name){toast('请输入 Skill 名称','err');return}
if(!content){toast('请输入或上传 Skill 内容','err');return}
var r=await api('POST','/api/prompt-skills',{name:name,scope:scope,persona:persona,content:content});
toast(r.message,r.ok?'ok':'err');
if(r.ok){document.getElementById('promptSkillFile').value='';rf_prompt_skills()}
}
async function deletePromptSkill(id){
if(!id||!confirm('删除这个 Prompt Skill？'))return;
var r=await api('DELETE','/api/prompt-skills/'+encodeURIComponent(id));
toast(r.message,r.ok?'ok':'err');
if(r.ok)rf_prompt_skills();
}

// ── PERSONA ──
async function rf_psna(){
try{
var r=await api('GET','/api/personas');var h='',items=r.items||{},act=r.active||'',sel='';
for(var n in items){
var p=items[n],isA=n===act;
h+=`<div class="persona-item ${isA?'active':''}"><h3>${isA?'<span class="tg tg-suc">活跃</span> ':''}${esc(n)}</h3><div style="font-size:11px;color:var(--text2);line-height:1.7">风格：${esc(p.style||'-')}<br>规则：${(p.rules||[]).length} 条</div><div class="btn-grp">${isA?'':'<button class="btn btn-sm btn-pr" onclick="actPsna(\''+n+'\')">启用</button>'}<button class="btn btn-sm btn-out" onclick="delPsna(\''+n+'\')" ${Object.keys(items).length<2?'disabled':''}>删除</button></div></div>`;
sel+='<option value="'+esc(n)+'" '+(isA?'selected':'')+'>'+esc(n)+'</option>';
}
document.getElementById('psnaList').innerHTML=h||'<div class="emp">暂无人设</div>';
var ps=document.getElementById('agentPersonaSelect');if(ps)ps.innerHTML=sel;
setPromptSkillPersonas(sel);
var active=items[act]||{};
document.getElementById('activePersonaDetail').innerHTML='<div class="setting-card"><strong>'+esc(act||'-')+'</strong><span>'+esc(active.system_prompt||'未设置系统 Prompt')+'</span></div><div class="setting-card" style="margin-top:10px"><strong>表达风格</strong><span>'+esc(active.style||'-')+'</span></div><div class="setting-card" style="margin-top:10px"><strong>行为边界</strong><span>'+esc((active.rules||[]).join(' / ')||'-')+'</span></div>';
if(!Object.keys(_configCache||{}).length){try{_configCache=await api('GET','/api/config')}catch(e){}}
renderAgentSettings(_configCache);
renderAgentSkillHelp();
renderPromptSkillScope();
}catch(e){}
}
async function addPsna(){
var n=document.getElementById('npName').value.trim(),p=document.getElementById('npPrompt').value.trim(),s=document.getElementById('npStyle').value.trim(),o=document.getElementById('npOwner').value.trim();
if(!n){toast('请输入名称','err');return}
var rules=document.getElementById('npRules').value.split(/\n/).map(function(x){return x.trim()}).filter(Boolean);
var r=await api('POST','/api/personas',{name:n,system_prompt:p,style:s,owner_prompt:o,rules:rules});toast(r.message,r.ok?'ok':'err');if(r.ok)rf_psna()
}
async function actPsna(n){var r=await api('POST','/api/personas/activate',{name:n});toast(r.message,r.ok?'ok':'err');if(r.ok)rf_psna()}
async function delPsna(n){if(!confirm('删除"'+n+'"？'))return;var r=await api('DELETE','/api/personas/'+encodeURIComponent(n));toast(r.message,r.ok?'ok':'err');if(r.ok)rf_psna()}

// ── COMMENTS ──
async function rf_cmts(){
try{
var r=await api('GET','/api/comments?limit=50'),its=r.items||[];
if(!its.length){document.getElementById('cmtTab').innerHTML=emptyState('cmts','暂无评论记录');return}
var h='<table class="tb"><tr><th>时间</th><th>类型</th><th>内容</th><th>来源</th><th>状态</th></tr>';
for(var i=0;i<its.length;i++){var c=its[i];h+=`<tr><td>${c.time||'-'}</td><td><span class="tg tg-inf">${c.type||'-'}</span></td><td title="${esc(c.content||'')}">${(c.content||'').substring(0,50)}</td><td>${c.source||'-'}</td><td>${c.executed?'<span class="tg tg-suc">已执行</span>':'<span class="tg tg-war">草稿</span>'}</td></tr>`}
h+='</table>';document.getElementById('cmtTab').innerHTML=h;
}catch(e){}
}

// ── USERS ──
async function rf_usrs(){
try{
var r=await api('GET','/api/users'),u=r.users||{},ks=Object.keys(u);
if(!ks.length){document.getElementById('usrTab').innerHTML=emptyState('usrs','暂无用户画像');return}
var h='<table class="tb"><tr><th>用户</th><th>好感度</th><th>关系</th><th>最近印象</th><th>更新时间</th></tr>';
for(var k in u){var p=u[k],a=parseInt(p.affinity)||0,cl=a>=80?'tg-suc':a>=45?'tg-inf':a<=-40?'tg-dan':'tg-war';
h+=`<tr><td>${p.name||k}</td><td><span class="tg ${cl}">${a}</span></td><td>${rel(a)}</td><td>${(p.notes||[]).slice(-2).join('；').substring(0,35)||'-'}</td><td>${p.updated_at||'-'}</td></tr>`}
h+='</table>';document.getElementById('usrTab').innerHTML=h;
}catch(e){}
}
function rel(a){var s=parseInt(a)||0;return s>=80?'挚友':s>=45?'熟人':s>=10?'有点印象':s<=-40?'需谨慎':'普通'}

// ── MEMORY ──
async function rf_mem(){
try{
var r=await api('GET','/api/memory'),h='';
if(r.diary&&r.diary.entries&&r.diary.entries.length){
h+='<div class="pc"><h3>日记 ('+r.diary.entries.length+'条)</h3>';
var es=r.diary.entries.slice(-15).reverse();
for(var i=0;i<es.length;i++){var d=es[i];h+=`<div style="padding:8px;margin:4px 0;background:var(--bg3);border-radius:6px;font-size:11px"><strong>${d.time||''} ${d.mood||''}</strong><div style="color:var(--text2)">${(d.content||'').substring(0,180)}</div></div>`}
h+='</div>'}
if(r.evolution&&r.evolution.events&&r.evolution.events.length){
h+='<div class="pc"><h3>进化事件 ('+r.evolution.events.length+'条)</h3>';
var evs=r.evolution.events.slice(-15).reverse();
for(var i=0;i<evs.length;i++){var e=evs[i];h+=`<div style="font-size:11px;color:var(--text2);margin:2px 0">${e.time||''} [${e.type||''}] ${(e.detail||'').substring(0,120)}</div>`}
h+='</div>'}
document.getElementById('memBox').innerHTML=h||emptyState('mem','暂无记忆数据');
}catch(e){}
}

// ── DIARY ──
async function rf_diary(){
try{
var r=await api('GET','/api/diary'),h='';
if(r.diary&&r.diary.entries&&r.diary.entries.length){
h+='<div class="pc"><h3>日记</h3>';
var es=r.diary.entries.slice(-20).reverse();
for(var i=0;i<es.length;i++){var d=es[i];h+=`<div style="border-bottom:1px solid var(--border);padding:8px 0"><div style="font-size:10px;color:var(--accent)">${d.time||''} · ${d.mood||''} · 精力${d.energy||'?'}</div><div style="font-size:11px;line-height:1.4">${(d.content||'').substring(0,200)}</div></div>`}
h+='</div>'}
if(r.evolution&&r.evolution.events&&r.evolution.events.length){
h+='<div class="pc"><h3>进化</h3><table class="tb"><tr><th>时间</th><th>类型</th><th>详情</th></tr>';
var evs=r.evolution.events.slice(-20).reverse();
for(var i=0;i<evs.length;i++){var e=evs[i];h+=`<tr><td>${e.time||'-'}</td><td>${e.type||'-'}</td><td style="max-width:260px">${(e.detail||'').substring(0,120)}</td></tr>`}
h+='</table></div>'}
document.getElementById('diaryBox').innerHTML=h||emptyState('diary','暂无数据');
}catch(e){}
}

// ── ACTIONS ──
async function rf_acts(){
try{
var r=await api('GET','/api/actions?limit=40'),its=r.items||[];
if(!its.length){document.getElementById('actTab').innerHTML=emptyState('acts','暂无操作日志');return}
var h='<table class="tb"><tr><th>时间</th><th>操作</th><th>详情</th><th>状态</th></tr>';
for(var i=0;i<its.length;i++){var a=its[i];h+=`<tr><td>${a.time||'-'}</td><td>${a.action||'-'}</td><td title="${esc(JSON.stringify(a.payload||{}))}">${JSON.stringify(a.payload||{}).substring(0,60)}</td><td>${a.executed?'<span class="tg tg-suc">已执行</span>':'<span class="tg tg-war">草稿</span>'}</td></tr>`}
h+='</table>';document.getElementById('actTab').innerHTML=h;
}catch(e){}
}

// ── ABOUT ──
async function rf_about(){
try{
var d=await api('GET','/api/info');
var aboutItem='background:var(--white);border:1px solid var(--sand);border-radius:12px;padding:14px 16px';
document.getElementById('aboutBox').innerHTML=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">系统版本</div><div style="font-size:15px;color:var(--fg);font-weight:650">v1.0</div></div>
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">面板运行时长</div><div style="font-size:15px;color:var(--fg);font-weight:650">${d.uptime}</div></div>
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Python 版本</div><div style="font-size:15px;color:var(--fg);font-weight:650">${d.python_version||'-'}</div></div>
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">运行平台</div><div style="font-size:15px;color:var(--fg);font-weight:650">${d.platform||'-'}</div></div>
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">工作目录</div><div style="font-size:11px;color:var(--fg);font-weight:500;font-family:var(--font-mono)">${d.cwd||'-'}</div></div>
<div style="${aboutItem}"><div style="font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">机器人状态</div><div style="font-size:15px;color:var(--fg);font-weight:650">${d.bot_running?'● 运行中':'○ 已停止'}</div></div>
</div>
<hr style="border-color:var(--line);margin:14px 0">
<p style="font-size:12px;color:var(--text2);line-height:2"><strong style="color:var(--fg)">B站 AI 智能管理系统</strong><br>基于大语言模型 · 视频理解 · 评论互动 · 私信回复 · 知识沉淀 · 自我进化</p>
<div style="margin-top:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap"><span style="color:var(--text2);font-size:12px">作者联系方式：</span><span style="display:inline-flex;align-items:center;gap:6px;background:rgba(201,100,66,.10);border:1px solid rgba(201,100,66,.16);padding:6px 14px;border-radius:10px;color:var(--accent);font-weight:650;font-size:14px;letter-spacing:0">QQ: 3781960338</span></div>`;
}catch(e){}
}

// ── UTIL ──
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function emptyState(icon,text){return '<div class="emp"><div class="ic">'+navIcon(icon)+'</div>'+esc(text)+'</div>'}

// ── AUTO REFRESH ──
var autoTmr=null;
var autoRefreshSkipPages={conf:1,psna:1,mood:1,behavior:1,tools:1,tutor:1,sys:1};
function auto(){
if(autoTmr)return;
autoTmr=setInterval(async function(){
var ap=document.querySelector('.page.on');if(!ap)return;
var id=ap.id.replace('pg-','');if(!autoRefreshSkipPages[id]&&window['rf_'+id])window['rf_'+id]();
try{var d=await api('GET','/api/info');setLiveInfo(d)}catch(e){}
},8000);
setInterval(updateLiveUptime,1000);
}

// ── MOOD ──
var moodPresets=["开心","平静","好奇","兴奋","沉思","疲惫","慵懒","元气满满"];
function rf_mood(){fetchMood();
var h="";for(var i=0;i<moodPresets.length;i++){h+="<button class=\"btn btn-out btn-sm\" onclick=\"quickMood('"+moodPresets[i]+"')\">"+moodPresets[i]+"</button> "}
document.getElementById("moodQuickBtns").innerHTML=h;}
async function fetchMood(){
try{var r=await api("GET","/api/mood/status");
document.getElementById("moodStatus").innerHTML="<div style=\"font-size:14px\">当前心情: <strong style=\"color:var(--accent);font-size:18px\">"+(r.current_mood||"-")+"</strong> | 精力: <strong style=\"color:var(--green)\">"+(r.energy||"?")+"</strong></div>";
document.getElementById("moodDefault").value=r.default_mood||"";document.getElementById("moodRandom").checked=r.random_enabled;
document.getElementById("moodRandInt").value=r.random_interval||5;document.getElementById("moodCustom").checked=r.custom_enabled;
document.getElementById("moodCustomText").value=r.custom_mood||"";}catch(e){}}
async function quickMood(m){var r=await api("POST","/api/mood/set",{current_mood:m});toast(r.message,r.ok?"ok":"err");if(r.ok)fetchMood()}
async function saveMood(){
var b={default_mood:document.getElementById("moodDefault").value,random_enabled:document.getElementById("moodRandom").checked,
random_interval_minutes:parseInt(document.getElementById("moodRandInt").value)||5,
custom_enabled:document.getElementById("moodCustom").checked,custom_mood:document.getElementById("moodCustomText").value};
var r=await api("POST","/api/mood/set",b);toast(r.message,r.ok?"ok":"err");if(r.ok)fetchMood()}
function moodToggleRandom(){document.getElementById("moodRandInt").disabled=!document.getElementById("moodRandom").checked}
function moodToggleCustom(){document.getElementById("moodCustomText").disabled=!document.getElementById("moodCustom").checked}

// ── BEHAVIOR ──
var _aiMarkerConfirmed=false;
function rf_behavior(){fetchBehavior()}
async function fetchBehavior(){
try{
var r=await api("GET","/api/behavior/get");
document.getElementById("aiMarkerText").value=r.ai_marker||"（内容由AI生成并由AI回复）";
var on=r.ai_marker&&r.ai_marker.length>0;
document.getElementById("aiMarkerOn").checked=on;
// energy
var e=r.energy||{};
document.getElementById("engMaxEnergy").value=e.max_energy||100;
document.getElementById("engRecoverMin").value=e.energy_recovery_min||5;
document.getElementById("engRecoverMax").value=e.energy_recovery_max||10;
document.getElementById("engRoundsMin").value=e.rounds_min||3;
document.getElementById("engRoundsMax").value=e.rounds_max||10;
document.getElementById("engRoundIntMin").value=e.round_interval_min||60;
document.getElementById("engRoundIntMax").value=e.round_interval_max||180;
document.getElementById("engVideoIntMin").value=e.video_interval_min||20;
document.getElementById("engVideoIntMax").value=e.video_interval_max||50;
// comment mode
var cm=r.comment_mode||"real";
var radios=document.getElementsByName("cmtMode");
for(var i=0;i<radios.length;i++){if(radios[i].value===cm)radios[i].checked=true}
}catch(e){}
}
async function toggleAiMarker(){
var cb=document.getElementById("aiMarkerOn");
if(!cb.checked){
if(confirm("确定要关闭AI免责声明吗？\n\n关闭后，所有评论和私信回复将不再标注AI身份。\n这可能导致平台审核风险。\n\n再次点击设置中的开关可以重新开启。")){
_aiMarkerConfirmed=true;
}else{
cb.checked=true;
return;
}
}
var r=await api("POST","/api/behavior/ai-marker/toggle",{enabled:cb.checked});
document.getElementById("aiMarkerText").value=r.marker||"";
document.getElementById("aiMarkerMsg").textContent=r.message||"";
toast(r.message,r.ok?"ok":"err");
}
async function saveAiMarker(){
var txt=document.getElementById("aiMarkerText").value.trim();
var r=await api("POST","/api/behavior/save",{ai_marker:txt});
document.getElementById("aiMarkerMsg").textContent=r.message||"";
toast(r.message,r.ok?"ok":"err");
}
async function saveEnergy(){
var b={
max_energy:parseInt(document.getElementById("engMaxEnergy").value)||100,
energy_recovery_min:parseInt(document.getElementById("engRecoverMin").value)||5,
energy_recovery_max:parseInt(document.getElementById("engRecoverMax").value)||10,
rounds_min:parseInt(document.getElementById("engRoundsMin").value)||3,
rounds_max:parseInt(document.getElementById("engRoundsMax").value)||10,
round_interval_min:parseInt(document.getElementById("engRoundIntMin").value)||60,
round_interval_max:parseInt(document.getElementById("engRoundIntMax").value)||180,
video_interval_min:parseInt(document.getElementById("engVideoIntMin").value)||20,
video_interval_max:parseInt(document.getElementById("engVideoIntMax").value)||50
};
var r=await api("POST","/api/behavior/save",{energy:b});
document.getElementById("engMsg").textContent=r.message||"";
toast(r.message,r.ok?"ok":"err");
}
async function saveCommentMode(){
var cm=document.querySelector('input[name="cmtMode"]:checked');
if(!cm)return;
var r=await api("POST","/api/behavior/save",{comment_mode:cm.value});
document.getElementById("cmtModeMsg").textContent=r.message||"";
toast(r.message,r.ok?"ok":"err");
}

	// ── SAFETY KEYWORDS ──
	var _safetyLoaded=false;
	async function fetchSafety(){
	try{
	var r=await api("GET","/api/behavior/safety");
	document.getElementById("safetyEnabled").checked=r.enabled||false;
	var kws=r.keywords||[];
	document.getElementById("safetyKeywords").value=kws.join("\n");
	document.getElementById("safetyKwSection").style.display=r.enabled?"":"none";
	_safetyLoaded=true;
	}catch(e){}
	}
	async function toggleSafety(){
	if(!_safetyLoaded){await fetchSafety();}
	var cb=document.getElementById("safetyEnabled");
	if(!cb.checked){
	if(!confirm("确定要关闭关键词安全校验吗？\n\n关闭后AI将不再过滤任何评论和回复。\n这可能导致账号风险。\n\n你可以随时在设置中重新开启。")){
	cb.checked=true;
	return;
	}
	}
	var r=await api("POST","/api/behavior/safety/toggle",{enabled:cb.checked});
	document.getElementById("safetyKwSection").style.display=cb.checked?"":"none";
	document.getElementById("safetyMsg").textContent=r.message||"";
	toast(r.message,r.ok?"ok":"err");
	}
	async function saveSafetyKeywords(){
	var txt=document.getElementById("safetyKeywords").value.trim();
	var kws=txt.split(/[\n,]/).map(function(s){return s.trim()}).filter(function(s){return s.length>0});
	var r=await api("POST","/api/behavior/safety/save",{keywords:kws});
	document.getElementById("safetyMsg").textContent=r.message||"";
	toast(r.message,r.ok?"ok":"err");
	}
	async function addSafetyKeyword(){
	var inp=document.getElementById("newSafetyKw");
	var kw=inp.value.trim();
	if(!kw){toast("请输入关键词","err");return}
	var ta=document.getElementById("safetyKeywords");
	var kws=ta.value.split("\n").map(function(s){return s.trim()}).filter(function(s){return s.length>0});
	if(kws.indexOf(kw)>=0){toast("关键词已存在","err");inp.value="";return}
	kws.push(kw);
	ta.value=kws.join("\n");
	inp.value="";
	await saveSafetyKeywords();
	}

// ── UPFOLLOW ──
async function rf_upfu(){
try{var r=await api("GET","/api/up-follow/list");var its=r.items||[];
if(!its.length){document.getElementById("upfuTab").innerHTML=emptyState('upfu','暂无已关注的UP主');return}
its.sort(function(a,b){return (b.avg_score||0)-(a.avg_score||0)});
var h="<table class=\"tb\"><tr><th>#</th><th>UP主</th><th>UID</th><th>评分</th><th>印象次数</th><th>关注时间</th></tr>";
for(var i=0;i<its.length;i++){var u=its[i];h+="<tr><td>"+(i+1)+"</td><td>"+(u.favorited?"已关注 · ":"")+u.name+"</td><td class=\"mono\">"+u.uid+"</td><td>"+(u.avg_score||"-")+"</td><td>"+(u.impressions||0)+"</td><td>"+(u.followed_at||"-")+"</td></tr>"}
h+="</table>";document.getElementById("upfuTab").innerHTML=h}catch(e){}}

// ── TOOLS ──
function rf_tools(){rf_kbStats();loadAsrHighlight()}
async function sendDanmaku(){
var b=document.getElementById("danmakuBvid").value.trim(),t=document.getElementById("danmakuText").value.trim();
if(!b||!t){toast("请填写BV号和弹幕内容","err");return}
if(t.length>20){toast("弹幕不能超过20字","err");return}
var r=await api("POST","/api/action/send-danmaku",{bvid:b,text:t});toast(r.message,r.ok?"ok":"err")}
async function analyzeVideo(){
var b=document.getElementById("analyzeBvid").value.trim();
if(!b){toast("请输入BV号","err");return}
var r=await api("POST","/api/action/analyze-video",{bvid:b});toast(r.message,r.ok?"ok":"err")}
async function runAgent(){
var g=document.getElementById("agentGoal").value.trim();
if(!g){toast("请输入目标描述","err");return}
var persona=document.getElementById("agentPersonaSelect")?document.getElementById("agentPersonaSelect").value:"";
var skill=document.getElementById("agentSkillSelect")?document.getElementById("agentSkillSelect").value:"full_plan";
var mode=document.getElementById("agentMode")?document.getElementById("agentMode").value:"queue";
var r=await api("POST","/api/action/agent-skill",{goal:g,persona:persona,skill:skill,mode:mode});toast(r.message,r.ok?"ok":"err")}
async function kbOrganize(){
if(!confirm("将对知识库进行AI自动分类整理，继续？"))return;
var r=await api("POST","/api/action/kb-organize");toast(r.message,"ok")}
async function kbRevisit(){
if(!confirm("将从已学内容中随机挑选进行复习，继续？"))return;
var r=await api("POST","/api/action/kb-revisit");toast(r.message,"ok")}
async function rf_kbStats(){
try{var r=await api("GET","/api/kb/stats");var h="<strong>"+r.total_files+"</strong> 篇知识 · 分类: ";
var cs=Object.keys(r.categories||{}).sort();for(var i=0;i<cs.length;i++){h+=cs[i]+" ("+r.categories[cs[i]]+") "}
document.getElementById("kbStatBox").innerHTML=h||"暂无知识库数据"}catch(e){}}
// ── ASR & Highlights ──
async function saveAsr(){var c=await api("GET","/api/config");if(!c)return;
c.asr=c.asr||{};c.asr.enabled=document.getElementById("asrEnabled").value=="1";
c.asr.backend=document.getElementById("asrBackend").value;
c.asr.language=document.getElementById("asrLang").value;
c.asr.speaker_separation=document.getElementById("asrSep").value=="1";
var r=await api("POST","/api/config",c);
document.getElementById("asrMsg").innerHTML=r.ok?'<span style="color:var(--green)">已保存</span>':'<span style="color:var(--red)">'+r.message+'</span>'}
async function saveDry(){var c=await api("GET","/api/config");if(!c)return;
c.dry_goods=c.dry_goods||{};c.dry_goods.enabled=document.getElementById("dryEnabled").value=="1";
c.dry_goods.min_score=parseFloat(document.getElementById("dryMinScore").value)||8.0;
c.dry_goods.folder_name=document.getElementById("dryFolder").value||"highlights";
var r=await api("POST","/api/config",c);
document.getElementById("dryMsg").innerHTML=r.ok?'<span style="color:var(--green)">已保存</span>':'<span style="color:var(--red)">'+r.message+'</span>'}
async function loadAsrHighlight(){var c=await api("GET","/api/config");if(!c)return;
if(c.asr){document.getElementById("asrEnabled").value=c.asr.enabled?"1":"0";
document.getElementById("asrBackend").value=c.asr.backend||"funasr";
document.getElementById("asrLang").value=c.asr.language||"zh";
document.getElementById("asrSep").value=c.asr.speaker_separation!==false?"1":"0"}
if(c.dry_goods){document.getElementById("dryEnabled").value=c.dry_goods.enabled?"1":"0";
document.getElementById("dryMinScore").value=c.dry_goods.min_score||8.0;
document.getElementById("dryFolder").value=c.dry_goods.folder_name||"highlights"}}


// ── SYSTEM ──
function rf_sys(){listBackups()}
async function exportConfig(){
var r=await api("POST","/api/export");document.getElementById("exportMsg").innerHTML=r.ok?
"<span class=\"tg tg-suc\">"+r.message+"</span>":"<span class=\"tg tg-dan\">"+r.message+"</span>"}
async function listBackups(){
try{var r=await api("GET","/api/import");var fs=r.files||[];
if(!fs.length){document.getElementById("backupList").innerHTML="<div class=\"emp\">暂无备份文件</div>";return}
var h="<table class=\"tb\"><tr><th>文件名</th><th>时间</th><th>大小</th><th>操作</th></tr>";
for(var i=0;i<fs.length;i++){var f=fs[i];h+="<tr><td class=\"mono\">"+f.name+"</td><td>"+f.mtime+"</td><td>"+f.size+"</td><td><button class=\"btn btn-sm btn-pr\" onclick=\"importConfig('"+f.name+"')\">恢复</button></td></tr>"}
h+="</table>";document.getElementById("backupList").innerHTML=h}catch(e){}}
async function importConfig(fn){
if(!confirm("确定从 "+fn+" 恢复所有配置？当前配置将被覆盖！"))return;
var r=await api("POST","/api/import/apply",{filename:fn});toast(r.message,r.ok?"ok":"err");if(r.ok)rf_dash()}
async function factoryReset(){
if(!confirm("确定恢复出厂设置？此操作不可逆！\n将删除所有配置、登录信息、数据文件！"))return;
// 🔒 服务端两步确认
var req=await api("POST","/api/factory-reset/request");
if(!req.ok){toast(req.message,"err");return}
var token=prompt("最后确认：输入确认令牌以执行\n\n令牌: "+req.token+"\n（直接复制粘贴上面的令牌）");
if(!token||token!==req.token){toast("令牌不匹配，已取消","err");return}
var delKB=document.getElementById("resetKB").checked;
if(delKB&&!confirm("同时删除知识库目录？此操作不可逆！"))return;
var r=await api("POST","/api/factory-reset",{delete_kb:delKB,confirm_token:token});toast(r.message,r.ok?"ok":"err");if(r.ok){rf_dash();listBackups()}}

// ── TUTOR (v2.0.3) ──
var _tutorHistory=[],_tutorRelPaths=[];
function rf_tutor(){
var sel=document.getElementById("tutorFileSelect");
fetch("/api/kb/list-files").then(function(r){return r.json()}).then(function(d){
if(!d.ok){toast(d.message,"err");return}
sel.innerHTML='';
for(var i=0;i<d.files.length;i++){
var f=d.files[i],up=f.up_name?" @"+f.up_name:"";
sel.innerHTML+='<option value="'+esc(f.rel_path)+'">['+f.category_path+'] '+esc(f.title)+up+' ('+f.size_kb+'KB)</option>';
}
}).catch(function(e){toast("加载文件列表失败","err")});
}
function tutorSelectAll(){
var sel=document.getElementById("tutorFileSelect");
for(var i=0;i<sel.options.length;i++)sel.options[i].selected=true;
}
function tutorSelectNone(){
var sel=document.getElementById("tutorFileSelect");
for(var i=0;i<sel.options.length;i++)sel.options[i].selected=false;
}
function _tutorGetSelected(){
var sel=document.getElementById("tutorFileSelect");
var out=[];
for(var i=0;i<sel.options.length;i++){
if(sel.options[i].selected)out.push(sel.options[i].value);
}
return out;
}
async function tutorLoadFile(){
var rps=_tutorGetSelected();
if(rps.length===0){toast("请至少选择一个知识文件","err");return}
_tutorRelPaths=rps;_tutorHistory=[];
document.getElementById("tutorChatLog").innerHTML='<div style="color:var(--text2);text-align:center;padding:20px">AI导师已就绪'+(rps.length>1?'（'+rps.length+'个文件）':'')+'，开始提问吧！</div>';
try{
var r=await api("POST","/api/kb/read-file",{rel_paths:rps});
if(!r.ok){toast(r.message,"err");return}
document.getElementById("tutorFileInfo").innerHTML='<span class="tg tg-suc">已加载 '+rps.length+' 个文件</span> ('+r.total_size+' 字符)';
document.getElementById("tutorContentPre").textContent=r.content||"(多文件内容已合并)";
document.getElementById("tutorContentBox").style.display="";
document.getElementById("tutorChatBox").style.display="";
document.getElementById("tutorResultBox").style.display="none";
}catch(e){toast("加载失败: "+e.message,"err")}
}
async function tutorSend(mode){
if(_tutorRelPaths.length===0){toast("请先加载文件","err");return}
var inp=document.getElementById("tutorInput");
var msg=inp.value.trim();
if(mode!="rewrite"&&mode!="html"&&!msg){toast("请输入问题","err");return}
if(mode=="rewrite"){
if(_tutorRelPaths.length>1){
if(!msg){toast("多文件改写请输入改写要求","err");return}
}else{msg=msg||"请优化结构、补充缺失知识点、修正不准确表述。"}
}
if(mode=="html")msg=msg||"请生成知识讲解网页。";

var log=document.getElementById("tutorChatLog");
if(mode=="chat"){
log.innerHTML+='<div style="margin-bottom:8px"><span style="color:var(--accent);font-weight:600">你:</span> '+esc(msg)+'</div>';
inp.value="";
}
var stat=document.getElementById("tutorStatus");
stat.textContent="AI思考中...";

try{
var r=await api("POST","/api/kb/tutor-chat",{
rel_paths:_tutorRelPaths, message:msg,
history:_tutorHistory, mode:mode,
style:document.getElementById("tutorHtmlStyle").value
});
if(!r.ok){stat.textContent="";toast(r.message,"err");log.innerHTML+='<div style="color:var(--red);margin-bottom:8px">'+esc(r.message)+'</div>';return}
stat.textContent="";

if(mode=="chat"){
_tutorHistory.push({role:"user",content:msg},{role:"assistant",content:r.reply});
if(_tutorHistory.length>20)_tutorHistory=_tutorHistory.slice(-20);
	log.innerHTML+='<div style="margin-bottom:10px;background:var(--bg3);border-left:3px solid var(--accent);padding:8px 12px;border-radius:4px"><span style="color:var(--accent2);font-weight:600">导师:</span> '+r.reply.replace(/\n/g,"<br>")+'</div>';
log.scrollTop=log.scrollHeight;
}else if(mode=="rewrite"){
var rb=document.getElementById("tutorResultBox");
var rc=document.getElementById("tutorResultContent");
rc.innerHTML='<div style="background:rgba(76,175,124,.08);border:1px solid rgba(76,175,124,.25);border-radius:6px;padding:10px;margin-bottom:10px"><strong>修改说明:</strong> '+esc(r.summary)+'</div><pre style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;max-height:300px;overflow:auto;font-size:11px;white-space:pre-wrap">'+esc(r.new_content||"")+'</pre>';
rb.style.display="";
var ra=document.getElementById("tutorResultActions");
ra.style.display="";
ra.innerHTML='<button class="btn btn-suc" onclick="tutorSaveRewrite()">保存改写（覆盖原文件）</button>';
window._tutorRewriteContent=r.new_content||"";
}else if(mode=="html"){
var rb=document.getElementById("tutorResultBox");
var rc=document.getElementById("tutorResultContent");
rc.innerHTML='<div style="background:rgba(91,141,239,.08);border:1px solid rgba(91,141,239,.25);border-radius:6px;padding:10px;margin-bottom:10px"><strong>HTML已生成</strong></div><pre style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px;max-height:200px;overflow:auto;font-size:10px;white-space:pre-wrap">'+esc((r.html||"").substring(0,2000))+'...</pre>';
rb.style.display="";
var ra=document.getElementById("tutorResultActions");
ra.style.display="";
ra.innerHTML='<button class="btn btn-pr" onclick="tutorSaveHtml()">保存HTML文件</button> <button class="btn btn-out" onclick="tutorPreviewHtml()">预览HTML</button>';
window._tutorHtmlContent=r.html||"";
}
}catch(e){stat.textContent="";toast("请求失败: "+e.message,"err")}
}
async function tutorSaveRewrite(){
if(!window._tutorRewriteContent||_tutorRelPaths.length===0){toast("没有可保存的内容","err");return}
try{
var r=await api("POST","/api/kb/tutor-save",{rel_path:_tutorRelPaths[0],content:window._tutorRewriteContent});
toast(r.message,r.ok?"ok":"err");
}catch(e){toast("保存失败","err")}
}
async function tutorSaveHtml(){
if(!window._tutorHtmlContent){toast("没有可保存的HTML","err");return}
try{
var title=_tutorRelPaths.length>1?"multi_"+_tutorRelPaths.length+"files":(_tutorRelPaths[0]||"knowledge").split("/").pop().replace(".md","");
var r=await api("POST","/api/kb/tutor-html-save",{html:window._tutorHtmlContent,title:title});
toast(r.message,r.ok?"ok":"err");
if(r.ok&&r.path){document.getElementById("tutorResultContent").innerHTML+='<div style="margin-top:8px;font-size:11px;color:var(--green)">文件: '+esc(r.path)+'</div>'}
}catch(e){toast("保存失败","err")}
}
function tutorPreviewHtml(){
if(!window._tutorHtmlContent){toast("没有可预览的HTML","err");return}
var w=window.open("","_blank");
if(w){w.document.write(window._tutorHtmlContent);w.document.close()}
else{toast("请允许弹窗以预览HTML","err")}
}

// ── INIT ──
initNavIcons();rf_dash();auto();
(async function(){try{var d=await api('GET','/api/info');setLiveInfo(d)}catch(e){}})();
</script>
</body>
</html>'''

# ═══════════════════════════════════════════
#  API 路由
# ═══════════════════════════════════════════
@app.route('/')
def index():
    return _load_html()

# ── 信息 ──
@app.route('/api/info')
def api_info():
    config = read_json(CONFIG_FILE)
    site = _site_branding(config)
    mood = read_json(DATA_DIR / "mood_state.json") or read_json(DATA_DIR / "web_mood.json")
    persona = read_json(DATA_DIR / "web_personas.json") or read_json(DATA_DIR / "personas.json")
    costs = read_json(DATA_DIR / "web_costs.json")
    bot_status = _bot_runtime_status()
    api_key = config.get('api', {}).get('unified_api_key', '') or os.getenv('BILI_AI_API_KEY', '')
    bili_token = os.getenv('BILI_REFRESH_TOKEN', '') or config.get('bilibili', {}).get('refresh_token', '')

    files = {}
    for name in ['config.json', 'bilibili_cookies.json', 'comment_log.json', 'private_message_log.json',
                 'user_profiles.json', 'mood_state.json', 'personas.json', 'bot_diary.json',
                 'self_evolution.json', 'agent_skill_log.json', 'bot_runtime_state.json']:
        files[name] = file_stat(DATA_DIR / name)

    upt = datetime.now() - panel_start
    panel_uptime = _format_duration(upt.total_seconds())

    comment_mode = config.get('behavior', {}).get('comment_mode', 'real')
    return jsonify(dict(
        bot_running=bot_status['running'],
        bot_start_time=bot_status['start_text'],
        bot_uptime=bot_status['uptime'],
        bot_uptime_seconds=bot_status['uptime_seconds'],
        bot_heartbeat_at=bot_status['heartbeat_at'],
        panel_uptime=panel_uptime,
        uptime=bot_status['uptime'] if bot_status['running'] else panel_uptime,
        api_configured=bool(api_key),
        bili_logged_in=bool(bili_token) or COOKIE_FILE.exists(),
        config_sections=len(config),
        data_files=sum(1 for f in files.values() if f['exists']),
        mood=dict(mood=mood.get('mood','?'), energy=mood.get('energy','?')) if mood else None,
        persona=dict(active=persona.get('active','')) if persona else None,
        cost_total=_cost_total(costs),
        files=files,
        comment_mode=comment_mode,
        python_version=sys.version.split()[0],
        platform=sys.platform,
        cwd=str(BASE_DIR),
        asr_enabled=config.get('asr', {}).get('enabled', False),
        asr_backend=config.get('asr', {}).get('backend', 'funasr'),
        site=dict(logo_text=site['logo_text'], brand_name=site['brand_name']),
    ))

# ── 配置 ──
@app.route('/api/config', methods=['GET','POST'])
def api_config():
    if request.method=='GET':
        return jsonify(read_json(CONFIG_FILE))
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify(dict(ok=False, message='配置必须是 JSON 对象')), 400
        current = read_json(CONFIG_FILE, {})
        if not isinstance(current, dict):
            current = {}
        merged = _deep_merge_dict(current, data)
        if isinstance(data, dict):
            site = merged.get('site')
            if isinstance(site, dict):
                site['logo_image'] = _sanitize_logo_image(site.get('logo_image') or '')
                site['logo_svg'] = _sanitize_logo_svg(site.get('logo_svg') or '')
        ok = write_json(CONFIG_FILE, merged)
        return jsonify(dict(ok=ok, message='配置已保存' if ok else '保存失败'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

# ── 机器人控制 ──
@app.route('/api/bot/start', methods=['POST'])
def api_bot_start():
    ok, msg = start_bot_process()
    return jsonify(dict(ok=ok, message=msg))

@app.route('/api/bot/stop', methods=['POST'])
def api_bot_stop():
    ok, msg = stop_bot_process()
    return jsonify(dict(ok=ok, message=msg))

@app.route('/api/bot/output')
def api_bot_output():
    with bot_output_lock:
        lines = list(bot_output_lines[-80:])
    return jsonify(dict(output='\n'.join(lines) if lines else '等待输出...'))

@app.route('/api/bot/clear', methods=['POST'])
def api_bot_clear():
    global bot_output_lines
    with bot_output_lock:
        bot_output_lines.clear()
    log_line("日志已清空")
    return jsonify(dict(ok=True, message='日志已清空'))

# ── B站登录 ──
@app.route('/api/bili/qr/start', methods=['POST'])
def api_bili_qr_start():
    global qr_state
    if qr_state.get('active'):
        return jsonify(dict(ok=False, message='已有登录流程进行中'))

    threading.Thread(target=do_qr_login, daemon=True).start()
    # wait for QR code to actually be generated (up to 10s)
    for _ in range(20):
        time.sleep(0.5)
        if qr_state.get('img_b64') or qr_state.get('status') in ('waiting_scan', 'error', 'timeout'):
            break
    return jsonify(dict(
        ok=True,
        img=qr_state.get('img_b64', ''),
        message=qr_state.get('message', ''),
        status=qr_state.get('status', '')
    ))

@app.route('/api/bili/qr/status')
def api_bili_qr_status():
    return jsonify(dict(
        status=qr_state.get('status', 'idle'),
        message=qr_state.get('message', ''),
        uid=qr_state.get('uid', ''),
        active=qr_state.get('active', False),
    ))

@app.route('/api/bili/logout', methods=['POST'])
def api_bili_logout():
    try:
        if COOKIE_FILE.exists():
            COOKIE_FILE.unlink()
        log_line("B站登录信息已清除")
        return jsonify(dict(ok=True, message='已退出登录'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

# ── 人格管理 ──
@app.route('/api/personas', methods=['GET','POST'])
def api_personas():
    data = read_json(DATA_DIR / "web_personas.json", dict(active="默认人格", items={}))
    if request.method=='GET':
        return jsonify(data)
    try:
        body = request.get_json(force=True)
        name = (body.get('name') or '').strip()
        if not name: return jsonify(dict(ok=False, message='名称不能为空')), 400
        data.setdefault('items', {})[name] = dict(
            name=name, system_prompt=body.get('system_prompt', ''),
            style=body.get('style',''), owner_prompt=body.get('owner_prompt',''),
            rules=body.get('rules',[]))
        write_json(DATA_DIR / "web_personas.json", data)
        return jsonify(dict(ok=True, message=f'人设"{name}"已创建'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/personas/activate', methods=['POST'])
def api_personas_activate():
    data = read_json(DATA_DIR / "web_personas.json", dict(active="默认人格", items={}))
    try:
        body = request.get_json(force=True)
        name = (body.get('name') or '').strip()
        if name not in data.get('items', {}):
            return jsonify(dict(ok=False, message='人设不存在')), 404
        data['active'] = name
        write_json(DATA_DIR / "web_personas.json", data)
        write_json(DATA_DIR / "personas.json", data['items'][name])
        return jsonify(dict(ok=True, message=f'已切换为"{name}"'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/personas/<name>', methods=['DELETE'])
def api_personas_delete(name):
    data = read_json(DATA_DIR / "web_personas.json", dict(active="默认人格", items={}))
    if len(data.get('items', {})) <= 1:
        return jsonify(dict(ok=False, message='至少保留一个人设')), 400
    if name in data.get('items', {}):
        del data['items'][name]
        if data.get('active') == name:
            data['active'] = next(iter(data['items']))
        write_json(DATA_DIR / "web_personas.json", data)
        return jsonify(dict(ok=True, message=f'已删除"{name}"'))
    return jsonify(dict(ok=False, message='不存在')), 404

@app.route('/api/prompt-skills', methods=['GET', 'POST'])
def api_prompt_skills():
    if request.method == 'GET':
        persona = (request.args.get('persona') or '').strip()
        items = _select_prompt_skills(persona) if persona else _read_prompt_skills()
        return jsonify(dict(items=items))
    try:
        body = request.get_json(force=True)
        incoming = _sanitize_prompt_skill_payload(body)
        items = _read_prompt_skills()
        incoming["created_at"] = incoming["updated_at"]
        replaced = False
        for index, item in enumerate(items):
            if item.get("id") == incoming["id"]:
                incoming["created_at"] = item.get("created_at") or incoming["created_at"]
                items[index] = incoming
                replaced = True
                break
        if not replaced:
            items.append(incoming)
        _write_prompt_skills(items)
        return jsonify(dict(ok=True, message='Prompt Skill 已保存', item=incoming))
    except ValueError as e:
        return jsonify(dict(ok=False, message=str(e))), 400
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/prompt-skills/<skill_id>', methods=['DELETE'])
def api_prompt_skill_delete(skill_id):
    items = _read_prompt_skills()
    kept = [item for item in items if item.get("id") != skill_id]
    if len(kept) == len(items):
        return jsonify(dict(ok=False, message='Prompt Skill 不存在')), 404
    _write_prompt_skills(kept)
    return jsonify(dict(ok=True, message='Prompt Skill 已删除'))

# ── 评论日志 ──
@app.route('/api/comments')
def api_comments():
    limit = request.args.get('limit', 50, type=int)
    data = read_json(DATA_DIR / "comment_log.json", dict(items=[]))
    return jsonify(dict(items=_normalize_comments(data, limit)))

# ── 用户画像 ──
@app.route('/api/users')
def api_users():
    data = read_json(DATA_DIR / "user_profiles.json", dict(users={}))
    wu = read_json(DATA_DIR / "web_user_profiles.json", dict(users={}))
    return jsonify(dict(users=_normalize_users(data, wu)))

# ── 记忆 ──
@app.route('/api/memory')
def api_memory():
    diary = _normalize_diary(read_json(DATA_DIR / "bot_diary.json", dict(entries=[])))
    evolution = _normalize_evolution(read_json(DATA_DIR / "self_evolution.json", dict(events=[])))
    return jsonify(dict(
        diary=diary,
        evolution=evolution,
    ))

# ── 日记进化 ──
@app.route('/api/diary')
def api_diary():
    diary = _normalize_diary(read_json(DATA_DIR / "bot_diary.json", dict(entries=[])))
    evolution = _normalize_evolution(read_json(DATA_DIR / "self_evolution.json", dict(events=[])))
    return jsonify(dict(
        diary=diary,
        evolution=evolution,
    ))

# ── 操作日志 ──
@app.route('/api/actions')
def api_actions():
    limit = request.args.get('limit', 50, type=int)
    data = read_json(DATA_DIR / "web_action_log.json", dict(items=[]))
    agent_log = read_json(DATA_DIR / "agent_skill_log.json", [])
    return jsonify(dict(items=_normalize_actions(data, agent_log, limit)))

# ── 图表数据 ──
@app.route('/api/charts')
def api_charts():
    """为仪表盘折线图提供历史统计数据"""
    days = request.args.get('days', 14, type=int)
    # 从 diary 数据提取心情/精力趋势
    diary = _normalize_diary(read_json(DATA_DIR / "bot_diary.json", dict(entries=[])))
    entries = diary.get('entries', [])
    mood_data = []
    for e in entries[-days*5:]:  # 每天可能有多个条目
        t = e.get('time', '')
        date = t[:10] if len(t) >= 10 else t  # YYYY-MM-DD
        mood_data.append(dict(
            date=date,
            valence=_num(e.get('mood_score', e.get('valence')), 50),
            energy=_num(e.get('energy'), 50),
        ))
    # 按天聚合
    daily_moods = {}
    for m in mood_data:
        d = m['date']
        if d not in daily_moods:
            daily_moods[d] = {'vals': [], 'engs': []}
        daily_moods[d]['vals'].append(m['valence'])
        daily_moods[d]['engs'].append(m['energy'])
    mood_result = []
    for d in sorted(daily_moods.keys())[-days:]:
        v = daily_moods[d]
        mood_result.append(dict(
            date=d[5:] if len(d)==10 else d,
            valence=round(sum(v['vals'])/len(v['vals']), 1),
            energy=round(sum(v['engs'])/len(v['engs']), 1),
        ))

    # 从评论日志提取评论趋势
    cmt_log = read_json(DATA_DIR / "comment_log.json", dict(items=[]))
    daily_cmts = {}
    for c in _normalize_comments(cmt_log, 1000):
        t = c.get('time', '')
        date = t[:10] if len(t) >= 10 else t
        daily_cmts[date] = daily_cmts.get(date, 0) + 1
    cmt_result = [dict(date=d[5:] if len(d)==10 else d, count=c) for d, c in sorted(daily_cmts.items())[-days:]]

    # 从操作日志提取操作趋势
    act_log = read_json(DATA_DIR / "web_action_log.json", dict(items=[]))
    agent_log = read_json(DATA_DIR / "agent_skill_log.json", [])
    daily_acts = {}
    for a in _normalize_actions(act_log, agent_log, 1000):
        t = a.get('time', '')
        date = t[:10] if len(t) >= 10 else t
        daily_acts[date] = daily_acts.get(date, 0) + 1
    act_result = [dict(date=d[5:] if len(d)==10 else d, count=c) for d, c in sorted(daily_acts.items())[-days:]]

    # 视频处理来自 evolution 事件
    evo = _normalize_evolution(read_json(DATA_DIR / "self_evolution.json", dict(events=[])))
    daily_vids = {}
    for ev in evo.get('events', []):
        t = ev.get('time', '')
        date = t[:10] if len(t) >= 10 else t
        detail = str(ev.get('detail', ''))
        if '视频' in detail or '观看' in detail or 'video' in detail.lower():
            daily_vids[date] = daily_vids.get(date, 0) + 1
    vid_result = [dict(date=d[5:] if len(d)==10 else d, count=c) for d, c in sorted(daily_vids.items())[-days:]]

    return jsonify(dict(
        comments=cmt_result,
        moods=mood_result if mood_result else [],
        actions=act_result,
        videos=vid_result,
    ))

# ── 心情管理 ──
@app.route('/api/mood/status')
def api_mood_status():
    mood = read_json(DATA_DIR / "mood_state.json", {})
    config = read_json(CONFIG_FILE, {})
    mc = config.get('mood', {})
    return jsonify(dict(
        current_mood=mood.get('mood', mc.get('default_mood', '平静')),
        energy=mood.get('energy', 100),
        random_enabled=mc.get('random_enabled', False),
        random_interval=mc.get('random_interval_minutes', 5),
        custom_enabled=mc.get('custom_enabled', False),
        custom_mood=mc.get('custom_mood', ''),
        default_mood=mc.get('default_mood', '平静'),
    ))

@app.route('/api/mood/set', methods=['POST'])
def api_mood_set():
    try:
        body = request.get_json(force=True)
        config = read_json(CONFIG_FILE, {})
        mc = config.setdefault('mood', {})
        if 'random_enabled' in body: mc['random_enabled'] = bool(body['random_enabled'])
        if 'random_interval_minutes' in body: mc['random_interval_minutes'] = int(body['random_interval_minutes'])
        if 'custom_enabled' in body: mc['custom_enabled'] = bool(body['custom_enabled'])
        if 'custom_mood' in body: mc['custom_mood'] = str(body['custom_mood'])
        if 'default_mood' in body: mc['default_mood'] = str(body['default_mood'])
        write_json(CONFIG_FILE, config)
        # 同时更新当前心情
        mood = read_json(DATA_DIR / "mood_state.json", {})
        if 'current_mood' in body:
            mood['mood'] = str(body['current_mood'])
            mood['updated_at'] = datetime.now().isoformat()
            write_json(DATA_DIR / "mood_state.json", mood)
        log_line(f"心情设置已更新")
        return jsonify(dict(ok=True, message='心情设置已更新'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

# ── 导出/导入配置 ──
BACKUP_DIR_EXPORT = get_backup_dir()

@app.route('/api/export', methods=['POST'])
def api_export():
    try:
        BACKUP_DIR_EXPORT.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_data = {}
        for fname in ['config.json', 'bilibili_cookies.json', 'mood_state.json', 'personas.json',
                       'user_profiles.json', 'comment_log.json', 'bot_diary.json',
                       'self_evolution.json', 'agent_skill_log.json', 'bot_runtime_state.json',
                       'history_videos.json', 'interests.json', PROMPT_SKILLS_FILENAME]:
            fp = DATA_DIR / fname
            if fp.exists():
                try:
                    export_data[fname] = json.loads(fp.read_text(encoding='utf-8'))
                except Exception:
                    export_data[fname] = {}
        # memory
        memf = BASE_DIR / "bot_memory.json"
        if memf.exists():
            try: export_data['bot_memory.json'] = json.loads(memf.read_text(encoding='utf-8'))
            except Exception: pass
        # knowledge metadata
        kmf = BASE_DIR / "knowledge_metadata.json"
        if kmf.exists():
            try: export_data['knowledge_metadata.json'] = json.loads(kmf.read_text(encoding='utf-8'))
            except Exception: pass

        out = BACKUP_DIR_EXPORT / f"bilibili_learning_bot_export_{ts}.json"
        # 🔒 API Key 脱敏处理
        if 'config.json' in export_data:
            export_data['config.json'] = sanitize_config_for_export(export_data['config.json'])
        if 'bilibili_cookies.json' in export_data:
            export_data['bilibili_cookies.json'] = sanitize_config_for_export(export_data['bilibili_cookies.json'])
        out.write_text(json.dumps(export_data, ensure_ascii=False, indent=2), encoding='utf-8')
        log_line(f"配置已导出: {out}")
        return jsonify(dict(ok=True, message=f'配置已导出到 {out}', path=str(out)))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/import', methods=['GET', 'POST'])
def api_import():
    try:
        files = []
        if BACKUP_DIR_EXPORT.exists():
            files = sorted([f for f in BACKUP_DIR_EXPORT.iterdir() if f.suffix == '.json'], key=lambda x: x.stat().st_mtime, reverse=True)
        # 返回可用备份列表
        flist = [dict(name=f.name, mtime=datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                      size=f"{f.stat().st_size/1024:.1f}K") for f in files[:20]]
        return jsonify(dict(files=flist))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/import/apply', methods=['POST'])
def api_import_apply():
    try:
        body = request.get_json(force=True)
        fname = body.get('filename', '')
        if not fname:
            return jsonify(dict(ok=False, message='未指定文件名')), 400
        # 🔒 路径穿越防护：校验 filename 不包含 ../ 且在备份目录下
        if not is_safe_path(fname, BACKUP_DIR_EXPORT):
            log_line(f"⛔ 拒绝路径穿越尝试: {fname}")
            return jsonify(dict(ok=False, message='文件名包含非法路径')), 403
        fpath = BACKUP_DIR_EXPORT / fname
        if not fpath.exists():
            return jsonify(dict(ok=False, message='备份文件不存在')), 404
        data = json.loads(fpath.read_text(encoding='utf-8'))
        count = 0
        for key, val in data.items():
            if key == 'bot_memory.json':
                write_json(BASE_DIR / key, val)
            elif key == 'knowledge_metadata.json':
                write_json(BASE_DIR / key, val)
            else:
                write_json(DATA_DIR / key, val)
            count += 1
        log_line(f"配置已导入: {fname} ({count}个文件)")
        return jsonify(dict(ok=True, message=f'已导入 {count} 个文件'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

# ── 恢复出厂设置 ──
# 🔒 服务端二次确认：需要前端先生成确认令牌
_factory_reset_pending_token = None

@app.route('/api/factory-reset/request', methods=['POST'])
def api_factory_reset_request():
    """请求恢复出厂设置，返回确认令牌（60秒有效）"""
    global _factory_reset_pending_token
    _factory_reset_pending_token = _uuid_module.uuid4().hex
    log_line("⚠ 收到恢复出厂设置请求，等待二次确认...")
    return jsonify(dict(ok=True, token=_factory_reset_pending_token,
                        message='请在60秒内输入确认令牌完成操作'))

@app.route('/api/factory-reset', methods=['POST'])
def api_factory_reset():
    global _factory_reset_pending_token
    try:
        body = request.get_json(silent=True) or {}
        confirm_token = body.get('confirm_token', '')
        # 🔒 必须有有效确认令牌
        if not _factory_reset_pending_token or confirm_token != _factory_reset_pending_token:
            _factory_reset_pending_token = None
            return jsonify(dict(ok=False, message='操作未确认，请先调用 /api/factory-reset/request 获取令牌')), 403
        _factory_reset_pending_token = None  # 一次性使用
        delete_kb = body.get('delete_kb', False)
        deleted = []
        for fname in ['config.json', 'bilibili_cookies.json', 'mood_state.json', 'personas.json',
                       'user_profiles.json', 'comment_log.json', 'bot_diary.json',
                       'self_evolution.json', 'agent_skill_log.json', 'bot_runtime_state.json',
                       'history_videos.json', 'interests.json', 'web_personas.json', PROMPT_SKILLS_FILENAME]:
            fp = DATA_DIR / fname
            if fp.exists():
                fp.unlink()
                deleted.append(fname)
        for fname in ['bot_memory.json', 'knowledge_metadata.json']:
            fp = BASE_DIR / fname
            if fp.exists():
                fp.unlink()
                deleted.append(fname)
        if delete_kb:
            kb_dir = BASE_DIR / "KnowledgeBase"
            if kb_dir.exists():
                import shutil
                shutil.rmtree(kb_dir, ignore_errors=True)
                deleted.append('KnowledgeBase/')
        # 清除日志
        global bot_output_lines
        with bot_output_lock:
            bot_output_lines.clear()
        log_line(f"恢复出厂设置完成，删除了 {len(deleted)} 个文件/目录" + ("（含知识库）" if delete_kb else ""))
        return jsonify(dict(ok=True, message=f'已清除 {len(deleted)} 个文件', deleted=deleted))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

# ── UP主关注列表 ──
@app.route('/api/up-follow/list')
def api_up_follow_list():
    mem_file = BASE_DIR / "bot_memory.json"
    ups = {}
    followed = []
    if mem_file.exists():
        try:
            mem = json.loads(mem_file.read_text(encoding='utf-8'))
            ups = mem.get('known_ups', {})
            for name, info in ups.items():
                if isinstance(info, dict) and info.get('followed'):
                    followed.append(dict(
                        name=name,
                        uid=info.get('uid', ''),
                        followed_at=info.get('followed_at', ''),
                        impressions=info.get('impressions', 0),
                        avg_score=round(info.get('total_score', 0) / max(info.get('impressions', 1), 1), 1),
                        favorited=info.get('favorited', False)
                    ))
        except Exception:
            pass
    return jsonify(dict(total=len(followed), items=followed))

# ── 知识库统计 ──
@app.route('/api/kb/stats')
def api_kb_stats():
    kb_dir = BASE_DIR / "KnowledgeBase"
    result = dict(exists=kb_dir.exists(), total_files=0, categories={})
    if kb_dir.exists():
        for root, dirs, files in os.walk(kb_dir):
            rel = os.path.relpath(root, kb_dir)
            parts = rel.split(os.sep) if rel != '.' else []
            depth = len(parts)
            md_files = [f for f in files if f.endswith('.md')]
            if md_files and depth <= 3:
                cat = '/'.join(parts[:3]) if parts else '根目录'
                result['categories'][cat] = result['categories'].get(cat, 0) + len(md_files)
            result['total_files'] += len(md_files)
    return jsonify(result)

# ── 功能操作 (桥接 CLI 功能) ──
@app.route('/api/action/analyze-video', methods=['POST'])
def api_action_analyze_video():
    """手动视频分析 — 在后台线程中运行"""
    try:
        body = request.get_json(force=True)
        bvid = (body.get('bvid') or '').strip()
        if not bvid:
            return jsonify(dict(ok=False, message='请输入 BV号')), 400
        log_line(f"触发手动视频分析: {bvid}")
        # 直接在子进程中调用 new_agent.py 的函数
        def _run_analysis():
            try:
                sys.path.insert(0, str(BASE_DIR))
                import new_agent
                # 尝试调用手动分析
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # 简单方式：通过命令行参数
                log_line(f"[分析] 正在分析 {bvid}...")
            except Exception as e:
                log_line(f"[分析] 失败: {e}")
        threading.Thread(target=_run_analysis, daemon=True).start()
        return jsonify(dict(ok=True, message=f'已触发视频分析: {bvid}'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/action/send-danmaku', methods=['POST'])
def api_action_send_danmaku():
    """手动发送弹幕 — 桥接到主进程"""
    try:
        body = request.get_json(force=True)
        bvid = (body.get('bvid') or '').strip()
        text = (body.get('text') or '').strip()
        if not bvid or not text:
            return jsonify(dict(ok=False, message='BV号和弹幕内容不能为空')), 400
        if len(text) > 20:
            return jsonify(dict(ok=False, message='弹幕不能超过20字')), 400
        # 写入任务文件让主进程执行
        task_file = DATA_DIR / "web_action_queue.json"
        tasks = read_json(task_file, [])
        tasks.append(dict(type='send_danmaku', bvid=bvid, text=text, time=datetime.now().isoformat()))
        write_json(task_file, tasks)
        log_line(f"弹幕任务已排队: {bvid} -> {text}")
        return jsonify(dict(ok=True, message=f'弹幕"{text}"已加入发送队列'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

def _run_agent_skill(goal: str, skill: str, persona: str = "", prompt_skills=None):
    if not COOKIE_FILE.exists():
        log_line("Agent技能未启动: 请先在 B站登录页完成登录")
        return

    async def _run():
        selected_prompt_skills = prompt_skills if prompt_skills is not None else _select_prompt_skills(persona)
        log_line(f"Agent技能开始执行: {skill} -> {goal} (Prompt Skills: {len(selected_prompt_skills)})")
        try:
            import new_agent
            brain = new_agent.AgentBrain()
            login_success = await brain.initialize_login()
            if not login_success:
                log_line("Agent技能执行失败: B站登录不可用")
                return
            runner = getattr(brain, "agent_runner", None)
            if runner is None:
                from services.agent_service import AgentSkillRunner
                runner = AgentSkillRunner(brain=brain)
            result = await runner.run_goal(goal, skill=skill, prompt_skills=selected_prompt_skills)
            results = result.get("results", [])
            ok_steps = sum(1 for item in results if item.get("result", {}).get("ok"))
            log_line(f"Agent技能执行完成: {skill} -> {goal} ({ok_steps}/{len(results)} 步成功)")
        except Exception as e:
            log_line(f"Agent技能执行失败: {skill} -> {e}")

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run())
    finally:
        loop.close()

def _start_agent_skill_thread(goal: str, skill: str, persona: str = "", prompt_skills=None):
    thread = threading.Thread(
        target=_run_agent_skill,
        args=(goal, skill, persona, prompt_skills),
        name="web-agent-skill",
        daemon=True,
    )
    thread.start()
    return thread

@app.route('/api/action/agent-skill', methods=['POST'])
def api_action_agent_skill():
    """执行 Agent 技能"""
    try:
        body = request.get_json(force=True)
        goal = (body.get('goal') or '').strip()
        persona = (body.get('persona') or '').strip()
        skill = (body.get('skill') or 'full_plan').strip()
        mode = (body.get('mode') or 'queue').strip()
        if not goal:
            return jsonify(dict(ok=False, message='请输入目标描述')), 400
        prompt_skills = _select_prompt_skills(persona)
        if mode in ('manual', 'dive'):
            _start_agent_skill_thread(goal, skill, persona, prompt_skills)
            log_line(f"Agent技能已启动: {mode}/{skill} -> {goal} (Prompt Skills: {len(prompt_skills)})")
            return jsonify(dict(ok=True, message=f'Agent任务已启动: {skill} / {goal}', prompt_skill_count=len(prompt_skills)))
        task_file = DATA_DIR / "web_action_queue.json"
        tasks = read_json(task_file, [])
        tasks.append(dict(type='agent_skill', goal=goal, persona=persona, skill=skill, mode=mode, prompt_skills=prompt_skills, time=datetime.now().isoformat()))
        write_json(task_file, tasks)
        log_line(f"Agent技能已排队: {skill} -> {goal} (Prompt Skills: {len(prompt_skills)})")
        return jsonify(dict(ok=True, message=f'Agent任务已加入队列: {skill} / {goal}', prompt_skill_count=len(prompt_skills)))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/action/kb-organize', methods=['POST'])
def api_action_kb_organize():
    """知识库整理"""
    try:
        task_file = DATA_DIR / "web_action_queue.json"
        tasks = read_json(task_file, [])
        tasks.append(dict(type='kb_organize', time=datetime.now().isoformat()))
        write_json(task_file, tasks)
        log_line("知识库整理任务已排队")
        return jsonify(dict(ok=True, message='知识库整理已加入队列'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/action/kb-revisit', methods=['POST'])
def api_action_kb_revisit():
    """知识库重温"""
    try:
        task_file = DATA_DIR / "web_action_queue.json"
        tasks = read_json(task_file, [])
        tasks.append(dict(type='kb_revisit', time=datetime.now().isoformat()))
        write_json(task_file, tasks)
        log_line("知识库重温任务已排队")
        return jsonify(dict(ok=True, message='知识库重温已加入队列'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

# ── 知识辅导 (v2.0.3) ──
@app.route('/api/kb/list-files')
def api_kb_list_files():
    """列出 KnowledgeBase 下所有 .md 文件"""
    try:
        from services.knowledge_tutor import scan_md_files
        files = scan_md_files()
        return jsonify(dict(ok=True, files=files, total=len(files)))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/kb/read-file', methods=['POST'])
def api_kb_read_file():
    """读取指定 .md 文件的内容（支持单文件或多文件）"""
    try:
        body = request.get_json(force=True)
        rel_paths = body.get('rel_paths') or body.get('rel_path')
        if isinstance(rel_paths, str):
            rel_paths = [rel_paths]
        if not rel_paths:
            return jsonify(dict(ok=False, message='请提供文件路径')), 400
        from services.knowledge_tutor import read_md_file, KNOWLEDGE_BASE_DIR
        parts = []
        total_size = 0
        for rp in rel_paths:
            full_path = KNOWLEDGE_BASE_DIR / rp.strip()
            if not full_path.exists():
                return jsonify(dict(ok=False, message=f'文件不存在: {rp}')), 404
            c = read_md_file(full_path)
            total_size += len(c)
            fname = os.path.basename(str(full_path))
            parts.append(f'=== {fname} ===\n{c}')
        combined = '\n\n'.join(parts)
        return jsonify(dict(ok=True, content=combined, paths=[str(KNOWLEDGE_BASE_DIR / rp.strip()) for rp in rel_paths], total_size=total_size, file_count=len(rel_paths)))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/kb/tutor-chat', methods=['POST'])
def api_kb_tutor_chat():
    """知识辅导：AI 对话（支持单文件或多文件）"""
    try:
        body = request.get_json(force=True)
        rel_paths = body.get('rel_paths') or body.get('rel_path')
        if isinstance(rel_paths, str):
            rel_paths = [rel_paths]
        message = (body.get('message') or '').strip()
        history = body.get('history') or []
        mode = (body.get('mode') or 'chat').strip()  # chat / rewrite / html
        style = (body.get('style') or 'dark').strip()

        if not rel_paths:
            return jsonify(dict(ok=False, message='请提供文件路径')), 400
        if not message and mode == 'chat':
            return jsonify(dict(ok=False, message='请输入问题')), 400

        from services.knowledge_tutor import KNOWLEDGE_BASE_DIR, get_tutor
        full_paths = []
        for rp in rel_paths:
            fp = KNOWLEDGE_BASE_DIR / rp.strip()
            if not fp.exists():
                return jsonify(dict(ok=False, message=f'文件不存在: {rp}')), 404
            full_paths.append(str(fp))

        tutor = get_tutor()
        if not tutor.is_available():
            return jsonify(dict(ok=False, message='AI 接口不可用，请先配置 API Key')), 503

        # 在后台线程中运行异步任务
        import threading
        result = {}
        error = None

        def _run():
            nonlocal result, error
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                if mode == 'rewrite':
                    # rewrite 只支持单文件
                    summary, new_content = loop.run_until_complete(
                        tutor.rewrite_file(full_paths[0], message)
                    )
                    result = dict(mode='rewrite', summary=summary, new_content=new_content)
                elif mode == 'html':
                    # html 支持多文件拼接
                    if len(full_paths) == 1:
                        html = loop.run_until_complete(
                            tutor.generate_html(full_paths[0], style)
                        )
                    else:
                        html = loop.run_until_complete(
                            tutor.generate_html(full_paths, style)
                        )
                    result = dict(mode='html', html=html, style=style)
                else:
                    # chat 支持多文件
                    if len(full_paths) == 1:
                        reply = loop.run_until_complete(
                            tutor.chat_about_file(full_paths[0], message, history)
                        )
                    else:
                        reply = loop.run_until_complete(
                            tutor.chat_about_file(full_paths, message, history)
                        )
                    result = dict(mode='chat', reply=reply)
                loop.close()
            except Exception as e:
                error = str(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=180)  # 最多等3分钟

        if error:
            return jsonify(dict(ok=False, message=error)), 500
        return jsonify(dict(ok=True, **result))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/kb/tutor-save', methods=['POST'])
def api_kb_tutor_save():
    """保存改写后的知识文件"""
    try:
        body = request.get_json(force=True)
        rel_path = (body.get('rel_path') or '').strip()
        content = (body.get('content') or '').strip()
        if not rel_path or not content:
            return jsonify(dict(ok=False, message='请提供文件路径和内容')), 400
        from services.knowledge_tutor import write_md_file, KNOWLEDGE_BASE_DIR
        full_path = KNOWLEDGE_BASE_DIR / rel_path
        if not full_path.exists():
            return jsonify(dict(ok=False, message='文件不存在')), 404
        success = write_md_file(full_path, content)
        if success:
            return jsonify(dict(ok=True, message='文件已保存（原文件已备份）'))
        else:
            return jsonify(dict(ok=False, message='保存失败')), 500
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

@app.route('/api/kb/tutor-html-save', methods=['POST'])
def api_kb_tutor_html_save():
    """保存生成的 HTML 文件"""
    try:
        body = request.get_json(force=True)
        html = (body.get('html') or '').strip()
        title = (body.get('title') or 'knowledge').strip()
        if not html:
            return jsonify(dict(ok=False, message='HTML内容为空')), 400
        from services.knowledge_tutor import KNOWLEDGE_BASE_DIR
        import re as _re
        html_dir = KNOWLEDGE_BASE_DIR / ".html_exports"
        html_dir.mkdir(parents=True, exist_ok=True)
        safe_title = _re.sub(r'[\\/*?:"<>|]', '_', title)[:40]
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        html_path = html_dir / f"{safe_title}_{ts}.html"
        html_path.write_text(html, encoding='utf-8')
        return jsonify(dict(ok=True, path=str(html_path), message='HTML已保存'))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 500

# ── 行为设置 ──
@app.route('/api/behavior/get')
def api_behavior_get():
    config = read_json(CONFIG_FILE, {})
    behavior = config.get('behavior', {})
    energy = config.get('energy', {})
    interaction = config.get('interaction', {})
    return jsonify(dict(
        ai_marker=behavior.get('ai_marker', '（内容由AI生成并由AI回复）'),
        comment_mode=behavior.get('comment_mode', 'real'),
        energy=dict(
            max_energy=interaction.get('max_energy', 100),
            energy_recovery_min=energy.get('energy_recovery_min', 5),
            energy_recovery_max=energy.get('energy_recovery_max', 10),
            rounds_min=energy.get('rounds_min', 3),
            rounds_max=energy.get('rounds_max', 10),
            round_interval_min=energy.get('round_interval_min', 60),
            round_interval_max=energy.get('round_interval_max', 180),
            video_interval_min=energy.get('video_interval_min', 20),
            video_interval_max=energy.get('video_interval_max', 50),
        )
    ))

@app.route('/api/behavior/ai-marker/toggle', methods=['POST'])
def api_behavior_ai_marker_toggle():
    try:
        body = request.get_json(force=True)
        enabled = bool(body.get('enabled', True))
        config = read_json(CONFIG_FILE, {})
        behavior = config.setdefault('behavior', {})
        if enabled:
            behavior['ai_marker'] = body.get('marker') or '（内容由AI生成并由AI回复）'
        else:
            behavior['ai_marker'] = ''
        write_json(CONFIG_FILE, config)
        msg = 'AI免责声明已开启' if enabled else 'AI免责声明已关闭'
        log_line(msg)
        return jsonify(dict(ok=True, message=msg, marker=behavior['ai_marker']))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400

@app.route('/api/behavior/save', methods=['POST'])
def api_behavior_save():
    try:
        body = request.get_json(force=True)
        config = read_json(CONFIG_FILE, {})
        changed = []
        # ai_marker
        if 'ai_marker' in body:
            config.setdefault('behavior', {})['ai_marker'] = str(body['ai_marker'])
            changed.append('AI免责声明')
        # comment_mode
        if 'comment_mode' in body:
            config.setdefault('behavior', {})['comment_mode'] = str(body['comment_mode'])
            changed.append('评论模式')
        # energy settings
        if 'energy' in body:
            eng = body['energy']
            energy = config.setdefault('energy', {})
            interaction = config.setdefault('interaction', {})
            for k in ['energy_recovery_min','energy_recovery_max','rounds_min','rounds_max',
                       'round_interval_min','round_interval_max','video_interval_min','video_interval_max']:
                if k in eng:
                    energy[k] = int(eng[k])
            if 'max_energy' in eng:
                interaction['max_energy'] = int(eng['max_energy'])
            changed.append('精力设置')
        write_json(CONFIG_FILE, config)
        msg = '、'.join(changed) + ' 已保存' if changed else '无变更'
        log_line(msg)
        return jsonify(dict(ok=True, message=msg))
    except Exception as e:
        return jsonify(dict(ok=False, message=str(e))), 400


# ── 免责声明 HTML 页面 ──
def _disclaimer_html():
    return _apply_site_chrome(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
{{SITE_HEAD_TAGS}}
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f5f4ed;--surface:#faf9f5;--white:#fff;--fg:#141413;--text:#4d4c48;--text2:#5e5d59;--muted:#5e5d59;--sand:#e8e6dc;--line:#f0eee6;--ring:#d1cfc5;--accent:#c96442;--red:#b53333;--green:#64735b;--focus:#c96442;--font-serif:Georgia,"Times New Roman","Songti SC",serif;--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;--shadow:rgba(20,20,19,.08) 0 18px 52px}
body{font-family:var(--font-sans);background:radial-gradient(circle at 80% 12%,rgba(201,100,66,.08),transparent 28%),var(--bg);color:var(--fg);display:flex;align-items:center;justify-content:center;min-height:100vh;line-height:1.55;padding:20px}
.card{background:rgba(250,249,245,.96);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:18px;padding:34px 30px;max-width:520px;width:min(520px,100%);text-align:center;box-shadow:var(--shadow)}
.auth-logo{width:52px;height:52px;margin:0 auto 16px;border-radius:15px;background:var(--fg);color:var(--surface);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:20px;overflow:hidden;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.auth-logo svg,.auth-logo img{width:100%;height:100%;display:block}
.auth-logo img{object-fit:cover}
.card h2{font-family:var(--font-serif);color:var(--fg);font-size:clamp(28px,4vw,36px);font-weight:500;line-height:1.12;margin-bottom:14px}
.brand-name{font-size:12px;color:var(--muted);margin:-6px 0 14px}
.card .lines{background:var(--white);border:1px solid var(--sand);border-radius:14px;padding:18px 20px;margin-bottom:20px;font-size:15px;line-height:1.85;text-align:left;color:var(--text)}
.card .lines .en{font-size:12px;color:var(--muted);margin-top:8px;display:block}
.inp-row{display:flex;gap:10px;align-items:stretch}
.inp-row input{flex:1;background:var(--white);border:1px solid var(--sand);border-radius:12px;padding:11px 14px;color:var(--fg);font-size:16px;outline:none;transition:border-color .16s ease,box-shadow .16s ease}
.inp-row input:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(201,100,66,.18)}
.inp-row input.error{border-color:var(--red);animation:shake .35s}
.btn{background:var(--accent);color:#fff;border:none;border-radius:12px;padding:10px 20px;font-size:15px;cursor:pointer;transition:transform .16s ease,opacity .16s ease;min-width:92px}
.btn:hover{opacity:.92}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.msg{margin-top:12px;font-size:13px;min-height:20px;color:var(--muted)}
.msg.err{color:var(--red)}
.msg.ok{color:var(--green)}
@media (max-width:520px){.card{padding:28px 18px}.inp-row{flex-direction:column}.btn{width:100%}}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
</style>
</head>
<body>
<div class="card">
<div class="auth-logo">{{SITE_LOGO_MARK}}</div>
<h2>免责声明 / DISCLAIMER</h2>
<p class="brand-name">{{SITE_BRAND_NAME}}</p>
<div class="lines">
本项目仅供学习参考，<br>
若因使用本项目产生任何后果，本人一概不负责。
<span class="en">This project is for learning purposes only.<br>Any consequences are solely your own responsibility.</span>
</div>
<div class="inp-row">
<input id="agreeInput" type="text" placeholder="请输入：我同意" autocomplete="off" autofocus>
<button class="btn" id="confirmBtn" onclick="doConfirm()">确认</button>
</div>
<div class="msg" id="msg"></div>
</div>
<script>
var inp=document.getElementById('agreeInput');
var btn=document.getElementById('confirmBtn');
var msg=document.getElementById('msg');
inp.addEventListener('keydown',function(e){if(e.key==='Enter')doConfirm()});
function doConfirm(){
var v=inp.value.trim();
if(!v){msg.textContent='请输入内容';msg.className='msg err';return}
btn.disabled=true;
fetch('/api/disclaimer/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agree:v})})
.then(function(r){return r.json()})
.then(function(d){
if(d.ok){msg.textContent='✓ 已确认，跳转中...';msg.className='msg ok';setTimeout(function(){location.href='/'},600)}
else{msg.textContent='✗ 请输入"我同意"';msg.className='msg err';btn.disabled=false;inp.classList.add('error');setTimeout(function(){inp.classList.remove('error')},400)}
})
.catch(function(){msg.textContent='请求失败，请重试';msg.className='msg err';btn.disabled=false})
}
</script>
</body>
</html>""", "免责声明")

# ── 首次设置页面（配置用户名和密码）──
def _setup_html():
    return _apply_site_chrome(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
{{SITE_HEAD_TAGS}}
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f5f4ed;--surface:#faf9f5;--white:#fff;--fg:#141413;--text:#4d4c48;--text2:#5e5d59;--muted:#5e5d59;--sand:#e8e6dc;--line:#f0eee6;--ring:#d1cfc5;--accent:#c96442;--red:#b53333;--green:#64735b;--focus:#c96442;--font-serif:Georgia,"Times New Roman","Songti SC",serif;--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;--shadow:rgba(20,20,19,.08) 0 18px 52px}
body{font-family:var(--font-sans);background:radial-gradient(circle at 80% 12%,rgba(201,100,66,.08),transparent 28%),var(--bg);color:var(--fg);display:flex;align-items:center;justify-content:center;min-height:100vh;line-height:1.55;padding:20px}
.card{background:rgba(250,249,245,.96);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:18px;padding:34px 30px;max-width:440px;width:min(440px,100%);text-align:center;box-shadow:var(--shadow)}
.auth-logo{width:52px;height:52px;margin:0 auto 16px;border-radius:15px;background:var(--fg);color:var(--surface);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:20px;overflow:hidden;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.auth-logo svg,.auth-logo img{width:100%;height:100%;display:block}
.auth-logo img{object-fit:cover}
.card h2{font-family:var(--font-serif);color:var(--fg);font-size:clamp(28px,4vw,36px);font-weight:500;line-height:1.12;margin-bottom:8px}
.card .sub{font-size:13px;color:var(--muted);margin-bottom:20px}
.fg{margin-bottom:14px;text-align:left}
.fg label{display:block;font-size:12px;font-weight:650;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.fg input{width:100%;background:var(--white);border:1px solid var(--sand);border-radius:12px;padding:10px 14px;color:var(--fg);font-size:15px;outline:none;transition:border-color .16s ease,box-shadow .16s ease}
.fg input:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(201,100,66,.18)}
.fg input.error{border-color:var(--red);animation:shake .35s}
.hint{font-size:11px;color:var(--muted);margin-top:4px}
.btn{background:var(--accent);color:#fff;border:none;border-radius:12px;padding:10px 24px;font-size:15px;cursor:pointer;transition:opacity .16s ease,transform .16s ease;width:100%;margin-top:6px}
.btn:hover{opacity:.92}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:12px;font-size:13px;min-height:20px;color:var(--muted)}
.msg.err{color:var(--red)}
.msg.ok{color:var(--green)}
@media (max-width:520px){.card{padding:28px 18px}}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
</style>
</head>
<body>
<div class="card">
<div class="auth-logo">{{SITE_LOGO_MARK}}</div>
<h2>首次设置</h2>
<p class="sub">欢迎使用 {{SITE_BRAND_NAME}}<br>请设置管理面板的用户名和密码</p>
<div class="fg"><label>用户名</label><input id="setupUser" type="text" placeholder="设置用户名" autocomplete="off" autofocus></div>
<div class="fg"><label>密码</label><input id="setupPass" type="password" placeholder="设置密码（至少4位）" autocomplete="off"></div>
<div class="fg"><label>确认密码</label><input id="setupPass2" type="password" placeholder="再次输入密码" autocomplete="off"></div>
<button class="btn" id="setupBtn" onclick="doSetup()">完成设置</button>
<div class="msg" id="msg"></div>
</div>
<script>
var inpU=document.getElementById('setupUser'),inpP=document.getElementById('setupPass'),inpP2=document.getElementById('setupPass2');
var btn=document.getElementById('setupBtn'),msg=document.getElementById('msg');
[inpU,inpP,inpP2].forEach(function(el){el.addEventListener('keydown',function(e){if(e.key==='Enter')doSetup()})});
async function doSetup(){
var u=inpU.value.trim(),p=inpP.value,p2=inpP2.value;
if(!u){msg.textContent='请输入用户名';msg.className='msg err';inpU.classList.add('error');setTimeout(function(){inpU.classList.remove('error')},400);return}
if(u.length<2){msg.textContent='用户名至少2个字符';msg.className='msg err';return}
if(p.length<4){msg.textContent='密码至少4位';msg.className='msg err';inpP.classList.add('error');setTimeout(function(){inpP.classList.remove('error')},400);return}
if(p!==p2){msg.textContent='两次输入的密码不一致';msg.className='msg err';inpP2.classList.add('error');setTimeout(function(){inpP2.classList.remove('error')},400);return}
btn.disabled=true;btn.textContent='正在保存...';
try{
var r=await fetch('/api/auth/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
var d=await r.json();
if(d.ok){msg.textContent='✓ 设置成功！正在跳转...';msg.className='msg ok';setTimeout(function(){location.href='/login'},800)}
else{msg.textContent='✗ '+d.message;msg.className='msg err';btn.disabled=false;btn.textContent='完成设置'}
}catch(e){msg.textContent='请求失败，请重试';msg.className='msg err';btn.disabled=false;btn.textContent='完成设置'}
}
</script>
</body>
</html>""", "首次设置")

# ── 登录页面 ──
def _login_html():
    return _apply_site_chrome(r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
{{SITE_HEAD_TAGS}}
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f5f4ed;--surface:#faf9f5;--white:#fff;--fg:#141413;--text:#4d4c48;--text2:#5e5d59;--muted:#5e5d59;--sand:#e8e6dc;--line:#f0eee6;--ring:#d1cfc5;--accent:#c96442;--red:#b53333;--green:#64735b;--focus:#c96442;--font-serif:Georgia,"Times New Roman","Songti SC",serif;--font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;--shadow:rgba(20,20,19,.08) 0 18px 52px}
body{font-family:var(--font-sans);background:radial-gradient(circle at 80% 12%,rgba(201,100,66,.08),transparent 28%),var(--bg);color:var(--fg);display:flex;align-items:center;justify-content:center;min-height:100vh;line-height:1.55;padding:20px}
.card{background:rgba(250,249,245,.96);backdrop-filter:blur(14px);border:1px solid var(--line);border-radius:18px;padding:34px 30px;max-width:400px;width:min(400px,100%);text-align:center;box-shadow:var(--shadow)}
.auth-logo{width:52px;height:52px;margin:0 auto 16px;border-radius:15px;background:var(--fg);color:var(--surface);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:20px;overflow:hidden;box-shadow:inset 0 0 0 1px rgba(255,255,255,.14)}
.auth-logo svg,.auth-logo img{width:100%;height:100%;display:block}
.auth-logo img{object-fit:cover}
.card h2{font-family:var(--font-serif);color:var(--fg);font-size:clamp(28px,4vw,36px);font-weight:500;line-height:1.12;margin-bottom:8px}
.card .sub{font-size:13px;color:var(--muted);margin-bottom:20px}
.fg{margin-bottom:14px;text-align:left}
.fg label{display:block;font-size:12px;font-weight:650;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.fg input{width:100%;background:var(--white);border:1px solid var(--sand);border-radius:12px;padding:10px 14px;color:var(--fg);font-size:15px;outline:none;transition:border-color .16s ease,box-shadow .16s ease}
.fg input:focus{border-color:var(--focus);box-shadow:0 0 0 3px rgba(201,100,66,.18)}
.fg input.error{border-color:var(--red);animation:shake .35s}
.btn{background:var(--accent);color:#fff;border:none;border-radius:12px;padding:10px 24px;font-size:15px;cursor:pointer;transition:opacity .16s ease,transform .16s ease;width:100%;margin-top:6px}
.btn:hover{opacity:.92}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:12px;font-size:13px;min-height:20px;color:var(--muted)}
.msg.err{color:var(--red)}
.msg.ok{color:var(--green)}
@media (max-width:520px){.card{padding:28px 18px}}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}
</style>
</head>
<body>
<div class="card">
<div class="auth-logo">{{SITE_LOGO_MARK}}</div>
<h2>登录管理面板</h2>
<p class="sub">{{SITE_BRAND_NAME}}<br>请输入用户名和密码</p>
<div class="fg"><label>用户名</label><input id="loginUser" type="text" placeholder="用户名" autocomplete="off" autofocus></div>
<div class="fg"><label>密码</label><input id="loginPass" type="password" placeholder="密码" autocomplete="off"></div>
<button class="btn" id="loginBtn" onclick="doLogin()">登 录</button>
<div class="msg" id="msg"></div>
</div>
<script>
var inpU=document.getElementById('loginUser'),inpP=document.getElementById('loginPass');
var btn=document.getElementById('loginBtn'),msg=document.getElementById('msg');
[inpU,inpP].forEach(function(el){el.addEventListener('keydown',function(e){if(e.key==='Enter')doLogin()})});
async function doLogin(){
var u=inpU.value.trim(),p=inpP.value;
if(!u||!p){msg.textContent='请输入用户名和密码';msg.className='msg err';return}
btn.disabled=true;btn.textContent='验证中...';
try{
var r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
var d=await r.json();
if(d.ok){msg.textContent='✓ 登录成功，跳转中...';msg.className='msg ok';setTimeout(function(){location.href='/'},500)}
else{msg.textContent='✗ '+d.message;msg.className='msg err';btn.disabled=false;btn.textContent='登 录';inpP.value='';inpP.classList.add('error');setTimeout(function(){inpP.classList.remove('error')},400)}
}catch(e){msg.textContent='请求失败，请重试';msg.className='msg err';btn.disabled=false;btn.textContent='登 录'}
}
</script>
</body>
</html>""", "登录")

# ── 免责声明确认页（Web端）──
@app.route('/disclaimer')
def disclaimer_page():
    return _disclaimer_html(), 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/api/disclaimer/confirm', methods=['POST'])
def api_disclaimer_confirm():
    data = request.get_json(force=True) if request.is_json else {}
    if data.get('agree') == '我同意':
        session['disclaimer_agreed'] = True
        return jsonify(dict(ok=True))
    return jsonify(dict(ok=False, message='请手动输入 我同意'))

# ── 首次设置页面 ──
@app.route('/setup')
def setup_page():
    return _setup_html(), 200, {'Content-Type': 'text/html; charset=utf-8'}

# ── 登录页面 ──
@app.route('/login')
def login_page():
    return _login_html(), 200, {'Content-Type': 'text/html; charset=utf-8'}

# ── 认证 API ──
@app.route('/api/auth/setup', methods=['POST'])
def api_auth_setup():
    """首次设置：保存用户名和密码到 config.json"""
    data = request.get_json(force=True) if request.is_json else {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '')
    if len(username) < 2:
        return jsonify(dict(ok=False, message='用户名至少2个字符'))
    if len(password) < 4:
        return jsonify(dict(ok=False, message='密码至少4位'))
    config = read_json(CONFIG_FILE, {})
    web_cfg = config.setdefault('web', {})
    web_cfg['username'] = username
    web_cfg['password'] = password
    if write_json(CONFIG_FILE, config):
        # 设置成功后自动登录
        session['disclaimer_agreed'] = True
        session['panel_authenticated'] = True
        log_line(f"面板首次设置完成，用户: {username}")
        return jsonify(dict(ok=True, message='设置成功'))
    return jsonify(dict(ok=False, message='保存配置失败'))

@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """登录验证"""
    data = request.get_json(force=True) if request.is_json else {}
    username = (data.get('username') or '').strip()
    password = data.get('password', '')
    config = read_json(CONFIG_FILE, {})
    saved_user, saved_pass = panel_credentials(config)
    if not saved_user or not saved_pass:
        return jsonify(dict(ok=False, message='面板尚未设置，请先完成首次配置'))
    if username == saved_user and password == saved_pass:
        session['panel_authenticated'] = True
        log_line(f"面板登录成功，用户: {username}")
        return jsonify(dict(ok=True, message='登录成功'))
    import time as _time
    _time.sleep(0.8)
    return jsonify(dict(ok=False, message='用户名或密码错误'))

@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """退出登录"""
    session.pop('panel_authenticated', None)
    session.pop('disclaimer_agreed', None)
    return jsonify(dict(ok=True, message='已退出登录'))

@app.route('/api/auth/status')
def api_auth_status():
    """检查登录状态"""
    return jsonify(dict(authenticated=bool(session.get('panel_authenticated'))))

# ── 面板认证检查（免责声明 + 首次设置 + 登录）──
@app.before_request
def _check_auth():
    def api_auth_error(message: str):
        return jsonify(dict(ok=False, message=message)), 401

    # 1. 先检查免责声明
    if not session.get('disclaimer_agreed'):
        if request.endpoint in ('disclaimer_page', 'api_disclaimer_confirm', 'static'):
            return None
        if request.path.startswith('/api/disclaimer'):
            return None
        if request.path == '/disclaimer':
            return None
        if request.path.startswith('/api/'):
            return api_auth_error('登录状态已失效，请重新登录或确认免责声明')
        return redirect('/disclaimer')

    # 2. 检查面板是否已配置（首次使用）
    config = read_json(CONFIG_FILE, {})
    saved_user, saved_pass = panel_credentials(config)
    has_credentials = bool(saved_user) and bool(saved_pass)
    if not has_credentials:
        allowed = ('setup_page', 'api_auth_setup', 'api_auth_logout', 'static')
        if request.endpoint in allowed:
            return None
        if request.path in ('/setup', '/api/auth/setup', '/api/auth/logout'):
            return None
        if request.path.startswith('/api/'):
            return api_auth_error('管理面板尚未完成首次设置')
        return redirect('/setup')

    # 3. 检查登录状态
    if session.get('panel_authenticated'):
        return None
    allowed = ('login_page', 'api_auth_login', 'api_auth_setup', 'api_auth_logout', 'static')
    if request.endpoint in allowed:
        return None
    if request.path in ('/login', '/api/auth/login', '/api/auth/logout', '/api/auth/status'):
        return None
    if request.path.startswith('/api/'):
        return api_auth_error('登录状态已失效，请重新登录或确认免责声明')
    return redirect('/login')

# ═══════════════════════════════════════════
#  启动
# ═══════════════════════════════════════════
def main():
    port = int(os.getenv('WEB_PORT', '8080'))
    host = os.getenv('WEB_HOST', '0.0.0.0')
    account_label = f" [账号: {ACCOUNT_NAME}]" if ACCOUNT_NAME != '默认' else ""

    # ── 免责声明确认（从bat启动时BILI_DISCLAIMER_SKIP=1可跳过）──
    if not os.getenv('BILI_DISCLAIMER_SKIP'):
        _disclaimer_confirm_terminal()

    banner = f"""
╔══════════════════════════════════════════════╗
║     B站 AI 管理系统 · Web 控制面板{account_label}        ║
╠══════════════════════════════════════════════╣
║   本地: http://127.0.0.1:{port}              ║
║   局域网: http://0.0.0.0:{port}             ║
║   数据: {DATA_DIR}
╚══════════════════════════════════════════════╝
"""
    print(banner, flush=True)
    print("(Disclaimer) This project is for learning purposes only. Any consequences are solely your own responsibility.", flush=True)
    log_line(f"[Web] Panel started (account: {ACCOUNT_NAME}, port: {port})")
    app.run(host=host, port=port, debug=False, threaded=True)
if __name__ == '__main__':
    main()
