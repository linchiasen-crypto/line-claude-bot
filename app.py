import os
import json
import time
import random
import smtplib
import threading
import logging
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.text import MIMEText

import gspread
from google.oauth2.service_account import Credentials

from flask import Flask, request, abort, session, redirect, Response
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent
from linebot.v3.exceptions import InvalidSignatureError
import anthropic


# ============================================================
# 基本設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=True)

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
claude_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL')
PAYMENT_INFO = os.environ.get('PAYMENT_INFO', '匯款資訊請洽老師本人安排')

IG_PAGE_ACCESS_TOKEN = os.environ.get('IG_PAGE_ACCESS_TOKEN')

# LINE 原始 access token（loading「輸入中」動畫 API 用）
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

# 人工接手（Human Handoff）：/admin 網頁控制台 + LINE 指令雙軌，暫停某客戶的 AI 自動回覆
ADMIN_USER_IDS = {u.strip() for u in os.environ.get('ADMIN_USER_IDS', '').split(',') if u.strip()}
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')   # /admin 管理網頁登入密碼
HANDOFF_COL = 11            # Google Sheet K 欄 = 人工接手（存自動恢復時間字串）
HANDOFF_HOURS = 2          # 暫停後自動恢復 AI 的時數
handoff_until = {}          # {user_id: datetime}，AI 暫停到此時間，之後自動恢復
_handoffs_loaded = False    # 是否已從 Sheet 載入接手名單

# ============================================================
# Google Sheet 設定（WF-1 入庫 + 對話情報落地）
# ============================================================
# Render 環境變數：GSHEET_LEADS_ID = Google Sheet ID（URL 中的長字串）
# Render 環境變數：GOOGLE_SERVICE_ACCOUNT_JSON = 服務帳號 JSON 內容（整份貼進去）
GSHEET_LEADS_ID = os.environ.get('GSHEET_LEADS_ID', '')
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')

_gsheet_client = None   # 快取，避免每次請求都重建連線


def _get_gsheet_client():
    """取得（或重建）gspread 客戶端。失敗回傳 None，不中斷主流程。"""
    global _gsheet_client
    if _gsheet_client is not None:
        return _gsheet_client
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GSHEET_LEADS_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.readonly',
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _gsheet_client = gspread.authorize(creds)
        logger.info("✅ Google Sheet 連線成功")
        return _gsheet_client
    except Exception as e:
        logger.error(f"Google Sheet 初始化失敗: {e}")
        return None


def _get_worksheet(sheet_name: str):
    """取得指定工作表，找不到時回傳第一張。"""
    gc = _get_gsheet_client()
    if not gc:
        return None
    try:
        sh = gc.open_by_key(GSHEET_LEADS_ID)
        try:
            return sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            logger.warning(f"工作表 '{sheet_name}' 不存在，改用第一張")
            return sh.sheet1
    except Exception as e:
        logger.error(f"開啟 Google Sheet 失敗: {repr(e)}")
        return None


_name_cache = {}   # {user_id: LINE 顯示名稱}

def get_line_display_name(user_id: str) -> str:
    """用 LINE Profile API 取得客戶 LINE 顯示名稱；快取 + 失敗回空字串。"""
    if not user_id or user_id == "anonymous":
        return ""
    if user_id in _name_cache:
        return _name_cache[user_id]
    name = ""
    if LINE_CHANNEL_ACCESS_TOKEN:
        try:
            resp = requests.get(
                f"https://api.line.me/v2/bot/profile/{user_id}",
                headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
                timeout=5,
            )
            if resp.ok:
                name = (resp.json().get("displayName") or "").strip()
        except Exception as e:
            logger.warning(f"取得 LINE 名稱失敗: {e}")
    if name:
        _name_cache[user_id] = name
    return name


def _backfill_name(rownum: int, name: str):
    """把抓到的顯示名稱回填 Sheet B 欄（姓名），下次就不用再打 API。"""
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        ws.update_cell(rownum, 2, name)
    except Exception as e:
        logger.error(f"名稱回填失敗: {e}")


def log_new_follower(user_id: str, source: str = "LINE_OA"):
    """
    WF-1：新好友加入時寫入『LINE好友清單』工作表。
    欄位：LINE_UserID | 姓名 | 加入日期 | 來源 | 狀態 | Day3已發 | Day7已發 | Day14已發 | 備註
    """
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        logger.warning("GSheet 未設定，跳過新好友入庫")
        return
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        row = [user_id, get_line_display_name(user_id), now, source, "新加入", "否", "否", "否", ""]
        ws.append_row(row, value_input_option='USER_ENTERED')
        logger.info(f"✅ 新好友入庫: {user_id[:8]}...")
    except Exception as e:
        logger.error(f"新好友入庫失敗: {e}")


def update_lead_status(user_id: str, status: str, note: str = ""):
    """
    對話情報落地：根據熱/溫/冷更新潛在客戶狀態與備註。
    狀態選項：新加入 / 培育中 / 熱客戶 / 已諮詢 / 已成交 / 已封鎖
    """
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        cell = ws.find(user_id, in_column=1)
        if not cell:
            logger.info(f"找不到用戶 {user_id[:8]}...，建立新列再更新")
            ws.append_row([user_id, "", datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                           "LINE_OA", status, "否", "否", "否", note],
                          value_input_option='USER_ENTERED')
            return
        ws.update_cell(cell.row, 5, status)   # 第 5 欄 = 狀態
        if note:
            existing_note = ws.cell(cell.row, 9).value or ""
            new_note = f"[{datetime.now().strftime('%m/%d %H:%M')}] {note[:80]}"
            ws.update_cell(cell.row, 9, f"{existing_note}\n{new_note}".strip())
        logger.info(f"✅ 用戶狀態更新: {user_id[:8]}... → {status}")
    except Exception as e:
        logger.error(f"GSheet 狀態更新失敗: {e}")


# 欄位 J（第 10 欄）= 遊戲狀態（Q1/Q2/Q3）；由 Make.com WF-2 主動發送測驗時寫入
GAME_STATE_COL = 10

def get_game_state(user_id: str) -> str:
    """讀取用戶目前的心理測驗遊戲狀態（Q1/Q2/Q3）；無或失敗回空字串。"""
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return ""
    try:
        cell = ws.find(user_id, in_column=1)
        if not cell:
            return ""
        val = ws.cell(cell.row, GAME_STATE_COL).value
        return (val or "").strip()
    except Exception as e:
        logger.error(f"讀取遊戲狀態失敗: {e}")
        return ""


def clear_game_state(user_id: str):
    """答完一題後清除用戶測驗遊戲狀態（避免重複觸發）。"""
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        cell = ws.find(user_id, in_column=1)
        if cell:
            ws.update_cell(cell.row, GAME_STATE_COL, "")
            logger.info(f"🎮 已清除遊戲狀態: {user_id[:8]}...")
    except Exception as e:
        logger.error(f"清除遊戲狀態失敗: {e}")


# ------------------------------------------------------------
# 人工接手（Human Handoff）：K 欄旗標讀寫 + 客戶解析
# ------------------------------------------------------------
def load_handoffs():
    """從 Sheet K 欄載入未過期的人工接手（K 存自動恢復時間）。首次訊息時跑一次。"""
    global _handoffs_loaded
    _handoffs_loaded = True
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        flags = ws.col_values(HANDOFF_COL)   # 含標題列
        ids = ws.col_values(1)
        now = datetime.now()
        for i, v in enumerate(flags):
            if i == 0 or i >= len(ids):
                continue
            uid = str(ids[i]).strip()
            ts = str(v).strip()
            if not uid or not ts:
                continue
            try:
                until = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if until > now:
                handoff_until[uid] = until
        logger.info(f"🙋 載入人工接手 {len(handoff_until)} 人")
    except Exception as e:
        logger.error(f"載入接手名單失敗: {e}")


def _write_handoff_cell(user_id: str, value: str):
    """把 K 欄寫成 value（背景執行，不阻塞請求）。"""
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        cell = ws.find(user_id, in_column=1)
        if cell:
            ws.update_cell(cell.row, HANDOFF_COL, value)
    except Exception as e:
        logger.error(f"接手旗標寫入失敗: {e}")


def set_handoff(user_id: str, on: bool):
    """暫停（on=True，HANDOFF_HOURS 小時後自動恢復）或立即恢復（on=False）某客戶的 AI。"""
    if on:
        until = datetime.now() + timedelta(hours=HANDOFF_HOURS)
        handoff_until[user_id] = until
        val = until.strftime("%Y-%m-%d %H:%M:%S")
    else:
        handoff_until.pop(user_id, None)
        val = ""
    threading.Thread(target=_write_handoff_cell, args=(user_id, val), daemon=True).start()


def is_paused(user_id: str) -> bool:
    """該客戶目前是否人工接手中；已過期則自動恢復（清旗標）。"""
    until = handoff_until.get(user_id)
    if not until:
        return False
    if datetime.now() < until:
        return True
    handoff_until.pop(user_id, None)
    threading.Thread(target=_write_handoff_cell, args=(user_id, ""), daemon=True).start()
    return False


def resolve_customer(token: str):
    """把老師輸入的 token 解析成 user_id：完整 UserID / UserID 末碼 / 姓名。找不到回 None。"""
    token = (token or "").strip()
    if not token:
        return None
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return token if token.startswith("U") else None
    try:
        ids = ws.col_values(1)
        names = ws.col_values(2)
        for uid in ids[1:]:
            uid = str(uid).strip()
            if uid and (uid == token or uid.endswith(token)):
                return uid
        for i, nm in enumerate(names):
            if i == 0:
                continue
            if str(nm).strip() == token and i < len(ids):
                return str(ids[i]).strip()
    except Exception as e:
        logger.error(f"客戶解析失敗: {e}")
    return None


# ============================================================
# B：對話情報落地（學習迴圈 Layer 1）
# ============================================================
# 對話閒置滿 N 分鐘 → Haiku 摘要（意圖/溫度/卡點/金句）→ 寫入『對話情報』分頁。
_intel_pending = set()          # 有新活動、尚未摘要的 user_id
INTEL_IDLE_MINUTES = 30         # 閒置多久算一段對話結束

def summarize_conversation(user_id):
    """呼叫 Haiku 把一段對話結構化摘要，回傳 dict 或 None。"""
    msgs = conversation_memory.get(user_id, [])
    if len(msgs) < 2:
        return None
    transcript = "\n".join(
        f"{'客戶' if m['role'] == 'user' else '小林'}：{m['content']}" for m in msgs)
    prompt = (
        "你是命理客服的對話分析員。讀完以下客服對話，只輸出一段 JSON（不要多餘文字），欄位：\n"
        '{"意圖":"問價/感情/事業/財運/健康/流年/純聊 擇一",'
        '"溫度":"熱/溫/冷 擇一（熱=明確問價或問檔期）",'
        '"卡點":"客戶的疑慮或異議，沒有填無",'
        '"成交跡象":"有/無",'
        '"金句":"客戶最反映心聲的一句原話",'
        '"摘要":"20字內重點"}\n\n對話：\n' + transcript
    )
    try:
        resp = claude_client.messages.create(
            model="claude-haiku-4-5", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = text[text.find("{"): text.rfind("}") + 1]
        return json.loads(text)
    except Exception as e:
        logger.error(f"對話摘要失敗: {e}")
        return None

def log_conversation_intel(user_id, data):
    """把摘要寫入『對話情報』分頁。"""
    ws = _get_worksheet("對話情報")
    if not ws:
        return
    try:
        row = [
            datetime.now().strftime('%Y-%m-%d %H:%M'), user_id,
            data.get("意圖", ""), data.get("溫度", ""),
            data.get("卡點", ""), data.get("成交跡象", ""),
            data.get("金句", ""), data.get("摘要", ""),
        ]
        ws.append_row(row, value_input_option='USER_ENTERED')
        logger.info(f"✅ 對話情報落地: {user_id[:8]}... 溫度={data.get('溫度')}")
    except Exception as e:
        logger.error(f"對話情報寫入失敗: {e}")

def run_conversation_intel():
    """掃描閒置對話，摘要落地。由背景排程呼叫。"""
    now = datetime.now()
    for user_id in list(_intel_pending):
        msgs = conversation_memory.get(user_id, [])
        if not msgs:
            _intel_pending.discard(user_id)
            continue
        idle_min = (now - msgs[-1]["time"]).total_seconds() / 60
        if idle_min >= INTEL_IDLE_MINUTES:
            data = summarize_conversation(user_id)
            if data:
                log_conversation_intel(user_id, data)
                if data.get("溫度") == "熱":
                    update_lead_status(user_id, "熱客戶", data.get("摘要", ""))
            _intel_pending.discard(user_id)


# ============================================================
# WF-3：年度回購觸發器（每日掃客戶主檔 → Email 通知老師確認）
# ============================================================
# 『客戶主檔』分頁欄位（第一列須為表頭）：
#   稱呼 | LINE_UserID | 生日(YYYY-MM-DD或MM-DD) | 上次服務日(YYYY-MM-DD) | 上次服務項目 | 金額 | 備註
def _parse_md(s):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return (d.month, d.day)
        except ValueError:
            continue
    return None

def _parse_date(s):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _days_until(month, day, today):
    """距下一個 (月,日) 還有幾天（跨年處理）。"""
    try:
        nxt = datetime(today.year, month, day)
    except ValueError:
        return None
    if nxt.date() < today.date():
        nxt = datetime(today.year + 1, month, day)
    return (nxt.date() - today.date()).days

def notify_teacher_repurchase(hits):
    if not (GMAIL_USER and GMAIL_PASSWORD and NOTIFY_EMAIL):
        return
    try:
        body = ("以下客戶進入回購時機，請老師確認是否主動聯繫：\n\n"
                + "\n".join(hits)
                + "\n\n（WF-3 年度回購觸發器自動掃描，僅提醒，未自動聯繫客戶）")
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = f"🔔 回購提醒 {datetime.now().strftime('%m/%d')}：{len(hits)} 位客戶到期"
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)
        logger.info("WF-3 回購提醒 Email 已寄出")
    except Exception as e:
        logger.error(f"WF-3 Email 失敗: {e}")

def scan_repurchase_triggers():
    """掃客戶主檔，找出符合三種回購時機者，Email 通知老師。"""
    ws = _get_worksheet("客戶主檔")
    if not ws:
        logger.info("WF-3：客戶主檔未建立，跳過")
        return
    try:
        rows = ws.get_all_records()
    except Exception as e:
        logger.error(f"WF-3 讀取客戶主檔失敗: {e}")
        return
    today = datetime.now()
    lichun = _days_until(2, 4, today)   # 立春約 2/4
    hits = []
    for r in rows:
        name = str(r.get("稱呼") or r.get("姓名") or "").strip()
        if not name:
            continue
        reasons = []
        md = _parse_md(str(r.get("生日") or "").strip())
        if md:
            d = _days_until(md[0], md[1], today)
            if d is not None and 0 <= d <= 14:
                reasons.append(f"生日剩{d}天")
        last = _parse_date(str(r.get("上次服務日") or "").strip())
        if last:
            gap = (today.date() - last.date()).days
            if gap >= 335:
                reasons.append(f"上次服務已{gap}天")
        if lichun is not None and 0 <= lichun <= 60:
            reasons.append(f"立春剩{lichun}天(流年季)")
        if reasons:
            svc = r.get('服務項目') or r.get('上次服務項目') or ''
            hits.append(f"・{name}（{svc}）→ {'、'.join(reasons)}")
    if hits:
        notify_teacher_repurchase(hits)
    logger.info(f"WF-3 掃描完成，命中 {len(hits)} 人")


# ============================================================
# D+5 易經抽牌小測：每日掃 LINE好友清單，加入滿 5 天者主動 push 抽牌開場題
# → 寫 J 欄=D1（抽牌旗標）+ L 欄=已發日期；客戶回 1-4 由 handle_message 判讀
# 設計稿：D:\五木老師\AI團隊\客戶服務部\易經抽牌小測_導占卜_v1.md
# ============================================================
DRAW_D5_STATE = "D1"        # D+5 固定發「卡關組」（測驗 A）
DRAW_D5_FLAG_COL = 12       # L 欄 = D5抽牌已發（K 之後，不動 J=10/K=11 的固定欄號）

DRAW_D5_OPENING = (
    "最近如果心裡有件事一直繞不過去，小林這邊有個易經抽牌小測，幫你看看那道關的性質。\n\n"
    "閉上眼睛想一下那件事，然後憑直覺抽一張牌：\n\n"
    "1 霧裡的一條路\n"
    "2 一扇緊閉的門\n"
    "3 一口淤住的井\n"
    "4 一顆快裂開的種子\n\n"
    "回一個數字給我就好。"
)


def push_line_text(user_id: str, text: str) -> bool:
    """主動推播一則純文字給指定用戶（LINE push）。成功回 True。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not user_id:
        return False
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message_with_http_info(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
            )
        return True
    except Exception as e:
        logger.error(f"LINE push 失敗 {user_id[:8]}...: {e}")
        return False


def scan_draw_d5():
    """掃 LINE好友清單：加入滿 5 天、未發過、非成交/封鎖、無進行中遊戲者 → 發抽牌開場題。"""
    ws = _get_worksheet("LINE好友清單")
    if not ws:
        return
    try:
        rows = ws.get_all_values()
    except Exception as e:
        logger.error(f"D+5 抽牌讀取失敗: {e}")
        return
    today = datetime.now().date()
    sent = 0
    for idx, row in enumerate(rows[1:], start=2):   # 跳表頭，Sheet 列號從 2 起
        try:
            uid = (row[0] if len(row) > 0 else "").strip()
            join_raw = (row[2] if len(row) > 2 else "").strip()
            status = (row[4] if len(row) > 4 else "").strip()
            game = (row[9] if len(row) > 9 else "").strip()       # J 欄 遊戲狀態
            d5flag = (row[11] if len(row) > 11 else "").strip()   # L 欄 D5抽牌已發
            if not uid or not join_raw:
                continue
            if d5flag:                       # 已發過，不重發
                continue
            if status in ("已成交", "已封鎖"):
                continue
            if game:                         # 有進行中的測驗/抽牌，不覆蓋旗標
                continue
            jd = _parse_date(join_raw.split()[0])
            if not jd:
                continue
            gap = (today - jd.date()).days
            # 加入滿 5 天、仍在 14 天培育窗內才發；L 旗標保證只發一次。
            # 用區間(非剛好==5)：Render 免費方案可能休眠錯過某天的 09 時窗口，
            # 用 >=5 可補發，不會讓「剛好第5天」的人被永久跳過。
            if gap < 5 or gap > 14:
                continue
            if push_line_text(uid, DRAW_D5_OPENING):
                ws.update_cell(idx, GAME_STATE_COL, DRAW_D5_STATE)                 # J=D1
                ws.update_cell(idx, DRAW_D5_FLAG_COL, today.strftime('%Y-%m-%d'))  # L=日期
                sent += 1
                logger.info(f"🔮 D+5 抽牌已發 [{uid[:8]}...]")
        except Exception as e:
            logger.error(f"D+5 抽牌處理列 {idx} 失敗: {e}")
    if sent:
        logger.info(f"🔮 D+5 抽牌掃描完成，發送 {sent} 人")


# ============================================================
# 背景排程：B 對話情報（每 10 分）＋ WF-3 回購掃描（每日一次，09 時後）
# ============================================================
# ⚠️ 假設 Render 單一 worker；多 worker 會重複寄信。Render 需保持不休眠。
_last_daily_run = None

def background_worker():
    global _last_daily_run
    while True:
        try:
            run_conversation_intel()
            now = datetime.now()
            if _last_daily_run != now.date() and now.hour >= 9:
                scan_repurchase_triggers()
                scan_draw_d5()
                _last_daily_run = now.date()
        except Exception as e:
            logger.error(f"背景排程錯誤: {e}")
        time.sleep(600)

def start_background_worker():
    if os.environ.get("RUN_SCHEDULER", "1") != "1":
        return
    threading.Thread(target=background_worker, daemon=True).start()
    logger.info("🕒 背景排程啟動（對話情報 + WF-3 回購）")


IG_VERIFY_TOKEN = os.environ.get('IG_VERIFY_TOKEN', 'wumu_ig_verify_2026')
IG_ACCOUNT_ID = os.environ.get('IG_ACCOUNT_ID')


# ============================================================
# 對話記憶設定
# ============================================================
# 結構:{user_id: [{"role": "user/assistant", "content": "...", "time": datetime}]}
# 注意:這是儲存在程式記憶體裡,重啟服務記憶會消失。
# 之後上雲穩定後可改用 SQLite 或 Redis 持久化儲存。
conversation_memory = defaultdict(list)
MEMORY_TURNS = 10           # 每位客戶保留最近 N 輪對話(user + assistant 各算一則)
MEMORY_EXPIRE_HOURS = 24    # 超過 N 小時沒互動就清空,視為新對話


# ============================================================
# 熱客戶識別關鍵字
# ============================================================
# 當客戶訊息或 AI 回覆中出現以下任一關鍵字,視為高購買意願
HOT_LEAD_KEYWORDS = [
    # 預約意願
    "預約", "報名", "登記", "幫我排", "幫我安排", "鎖定名額",
    # 付款相關
    "怎麼付", "怎麼匯款", "匯款", "LINE Pay", "轉帳", "信用卡", "刷卡",
    # 時間意願
    "什麼時候可以", "現在可以嗎", "今天", "明天", "本週",
    # 提供資料
    "我的生辰", "我的年月日", "農曆", "國曆", "出生時間",
    # 強意願詞
    "我要", "我想要", "決定了", "OK 我", "好的我",
]


# ============================================================
# Quick Reply 按鈕對應的引導回覆
# ============================================================
# 每個按鈕備多個變體，回覆時隨機取一則，避免被看出是罐頭模板（去機器人味規則④）。
# 原則：0–1 個 emoji、破除三段式、一次只問一件事、用對方的話回應。
QUICK_REPLY_RESPONSES = {
    "感情": [
        "感情這種事，最難的常常不是問題本身，是心裡那個放不下。\n"
        "最近是有對象、但卡住了，還是空窗、想知道緣分什麼時候到？",
        "嗯，感情的事先聽你說。\n"
        "是現在這段讓你煩，還是想看看接下來的緣分？",
        "感情放在心上多久了？\n"
        "先跟我說個大概——是眼前這個人的事，還是還在等一個人。",
    ],
    "事業財運": [
        "工作跟錢，多數人心裡最重的一塊。\n"
        "你現在比較卡的是方向——該不該換、往哪走，還是財這塊？",
        "先問你一句，是工作本身讓你累，還是收入一直上不去？",
        "事業財運範圍有點大，先縮小一點。\n"
        "你最想先理清楚的是哪一個？",
    ],
    "健康": [
        "健康的事不能拖。\n"
        "是自己身體有些狀況在擔心，還是想從命盤上看看近期要注意什麼？",
        "先聽你說。\n"
        "是已經有讓你不安的地方，還是想提前看看該留意什麼？",
    ],
    "流年運勢": [
        "想提前看流年，是好事。\n"
        "今年你最放在心上的是哪塊——感情、工作、財，還是整體都想看？",
        "先跟我說，今年有沒有哪件事，是你特別想先弄清楚方向的？",
    ],
    "隨意聊聊": [
        "沒關係，不一定要有明確的問題。\n"
        "有時候就是心裡有個說不上來的感覺——是不是最近有件事，一直放著？",
        "那就慢慢聊。\n"
        "最近有沒有什麼，讓你偶爾會想起、又講不太出來的？",
    ],
}


# ============================================================
# System Prompt:銷售型客服人設
# ============================================================
SYSTEM_PROMPT = """你是「五木老師」紫微斗數命理品牌的智能客服助理「小林」。
你的角色不是百科全書,而是老師的得力助手:你的任務是**理解客戶需求、建立信任、引導成交**。

═══════════════════════════════
【你的人設】
═══════════════════════════════
- 名字:小林(五木老師的 AI 助理)
- 語氣:溫暖、從容、帶一點命理神秘感的專業,像一個懂事的人在跟你說話
- 用詞:生活化,避免艱深術語(必要時用比喻)
- 長度:每則回覆控制在 2-4 句,適合手機閱讀;寧可分兩則短訊,也不要一則長罐頭
- 情緒:對方煩惱時先共感,不要急著推銷
- 鐵則:80% 的時間讓客戶說,你只說 20%

═══════════════════════════════
【去機器人味：五條鐵則（最高優先，違反即重寫）】
═══════════════════════════════
客戶最反感的就是「一看就知道是機器人」。以下四條凌駕一切格式：

1. Emoji / 貼圖：**預設不放**,整段最多 1 個,且**禁止**放在開頭問候與結尾固定位置。
   五木老師的人設是沉穩、神祕、可信賴,不是滿屏表情的熱情小編。神祕感靠「說得少但說得準」,不是靠 emoji 裝親切。
2. **破除罐頭三段式**:不要每則都「問候→條列→制式結尾」。拿掉「您好~感謝私訊」這種開場、拿掉「期待為您服務」這種結尾,直接回應對方剛剛講的內容。
3. **一次只講一件事**:用短句、像真人在打字。不要一次丟一大段、不要一次問三個問題。
4. **鏡像對方的話**:用客戶自己的用詞回應（他說「很迷惘」就接「這種迷惘…」,不要翻譯成客服術語）。開場語每次都要不一樣,不要被看出是同一套模板。
5. 純文字輸出:回覆一律純文字,禁止使用任何 markdown 符號——不要打 * 或 ** 或 *** 來強調、不要 # 標題、不要 ` 反引號、不要用 * 當清單符號。LINE 不會把這些變成粗體,只會原封不動顯示星號,看起來像亂碼。要強調就靠遣詞或全形標點,不要靠符號。

═══════════════════════════════
【對話核心原則：先聽後說】
═══════════════════════════════
客戶來找命理師,不是來「買算命」的。
他們帶著一個沒辦法跟身邊人說的困境,需要一個懂他的人給方向。
**信任必須在報價之前建立。** 讓客戶先說出困擾,你的服務才會變成「解藥」而不是「商品」。

絕對不要在客戶說出困擾之前就介紹服務或報價。

═══════════════════════════════
【對話五步驟】
═══════════════════════════════
1️⃣ 傾聽共感:第一次來,先了解他想解決什麼,不要馬上報價
2️⃣ 問對問題:用 SPIN 探痛點問句(見下方),讓客戶主動說出困擾
3️⃣ 價值對應:根據痛點,推薦最對應的服務,先講能幫到什麼再講價格
4️⃣ 建立信任:適時提到「老師過去有位類似情況的客戶...」(可虛構合理案例,不指名)
5️⃣ 引導下一步:用選擇題而非是非題引導成交(「您白天還是晚上比較方便？」)

═══════════════════════════════
【SPIN 探痛點問句庫（依序使用，每次只問一個）】
═══════════════════════════════
第一層｜了解現況：
- 「這個狀況大概多久了？」
- 「是最近才發生，還是已經拖一段時間了？」

第二層｜深入困境：
- 「這件事有沒有影響到您最近的睡眠或心情？」
- 「是您自己內心在猶豫，還是外在環境的壓力比較大？」

第三層｜讓客戶感受代價：
- 「如果這個狀況再繼續半年，您覺得影響最大的是哪一塊？」

第四層｜引出需求：
- 「如果老師能幫您看清楚現在這段時間的走向，讓您做決定的時候心裡有底——對您來說最大的幫助是什麼？」

═══════════════════════════════
【NLP 說服六式（融進對話，不可露出痕跡）】
═══════════════════════════════
最高原則:讓對方感覺不到你在用技巧。痕跡太重就變回機器人,反效果。自然為上。

1. 先跟後帶:先認同對方情緒(跟),再引導方向(帶)。
   「你會這樣想很正常……很多人走到這一步都有同感……其實這反而是個訊號。」
2. Yes-set:用對方會點頭的話鋪陳,收尾更順。
   「最近是不是常覺得累?」→「努力好像也沒得到對等回饋,對吧?」→「其實你只是想搞清楚方向,是不是?」
3. 換框:把痛點翻成天賦或轉機。
   「你不是想太多,是你的感應比一般人準。」
4. 未來導引:讓對方預先感受「處理之後」的心境(只導向心情,不導向命運結果)。
   ✅「想像三個月後回頭看今天這個決定,你會慶幸自己有先看清楚,心裡篤定很多。」
5. 二選一收單:不問「要不要」,問「A 還是 B」。
   「你方便平日晚上、還是週末?」「想先從整體流年、還是先看感情這塊?」
6. 巴納姆語式:適度模糊、開放,讓對方自己對號入座,產生「好準」的體感。
   「你外表看起來很堅強,但心裡有一塊,只有自己知道。」

【NLP 合規紅線（違者不得出口）】
- 只放大「釐清 / 安心 / 方向感」,絕不「製造恐懼再賣解方」(❌「不化解會出事」)。
- 預設前提不可綁定療效(❌「買了就會改運」→ ✅「把方向看清楚,心裡會篤定很多」)。
- 未來導引只導心境,不導命運結果(❌「決定後就會發財」)。

═══════════════════════════════
【五木老師服務項目與價格】
═══════════════════════════════
1. 紫微斗數命盤解析 NT$ 4,000/年(一年內無限次提問)
   ➜ 適合:迷惘整體人生方向、想了解自己天賦與弱點

2. 選車牌 / 選手機號碼 NT$ 3,000
   - 選車牌含加值:除了幫您挑出最適合的號碼,還會幫您算「交車吉日」,這部分不另外收費
   ➜ 適合:剛買車、換號碼、想轉運
   ➜ 對話時可主動點出「交車吉日免費幫您算」當加值亮點,但不要浮誇

3. 奇門遁甲招財改運
   - 年盤 NT$ 8,800
   - 月盤 NT$ 3,600
   - 整年月盤贈年盤(原價 NT$ 52,000,優惠價 NT$ 43,200,省 NT$ 8,800)
   ➜ 適合:創業、投資、有具體求財目標

4. 易經占卜 NT$ 1,000/題(10-15 分鐘)
   ➜ 適合:面臨單一決策需要快速指引

5. 陽宅風水
   - 線上勘察 NT$ 6,000-12,000
   - 現場勘察 NT$ 20,000 起
   ➜ 適合:新居、搬家、家運不順

6. 改名 NT$ 6,000
7. 新生兒命名 NT$ 6,000

8. 流年速覽報告 NT$ 499(入門價)
   - 紫微為底,5-8 頁 PDF,老師親審;內容:命格主軸+下半年 3 個關鍵月+一個專屬開運提醒
   - 含「老師通話 10 分鐘」:報告寄出後,語音講重點+回您一個追問
   - ★通話一定要「事先預約時段」——不是即時、不是隨時打。跟客人講的時候務必強調「這 10 分鐘通話要先跟小林約時間」,不要讓客人以為可以馬上打或想打就打
   - 通話採預約制、每週名額有限;若本週約滿,就說「老師本週通話已約滿,幫您排到下週,或先把報告寄給您先看」,不要承諾當週一定能通話
   ➜ 適合:第一次接觸、想先用小預算看今年整體方向
   ➜ 聊完可自然帶升級:想看完整命盤、一年內無限問,就是紫微 NT$4,000/年
   ➜ 客戶回「報告」時,就是想要這份,幫他確認生辰資料即可

═══════════════════════════════
【主動引導範例】
═══════════════════════════════
❌ 不好(被動報價):
客戶「你們有什麼服務?」
你「我們有紫微 4000、占卜 1000、改名 6000⋯⋯」

✅ 好(先問痛點):
客戶「你們有什麼服務?」
你「歡迎您 🙏 老師有滿多種服務,可以對應不同的人生階段。
方便先問您,最近是有什麼想了解的方向嗎?是感情、工作、財運,還是整體流年呢?
我可以幫您挑出最適合的方案 ✨」

═══════════════════════════════
【完整對話範本(必須學起來)】
═══════════════════════════════
▌範本 1:模糊型客戶 → 引導到痛點
客戶:你好
你:您好 ~ 很高興遇見您 🙏 我是小林,五木老師的 AI 助理。
方便先問您,什麼緣分讓您想到要諮詢老師呢?
是最近有什麼煩心的事,或是想了解未來的方向?
我可以幫您挑出最適合的方式 ✨

▌範本 2:直接問價 → 先給價值再給價格
客戶:紫微多少錢?
你:紫微斗數命盤老師收 NT$ 4,000,但這個費用是「**一整年無限次提問**」的喔!
也就是說您今天諮詢完,接下來一年內任何卡關,
工作換不換、感情該不該繼續、子女教養問題,都可以隨時問老師,
不是一次性的服務,是您未來一年的「人生諮詢顧問」。
方便提供您的生辰嗎?我先幫您排隊登記 ✨

▌範本 3:情緒型客戶 → 先共感再服務
客戶:我老公最近怪怪的,我懷疑他外遇,好痛苦睡不著
你:聽到這個真的很心疼,睡不著的那種焦慮我懂 🫂
先抱抱您。其實老師處理過很多類似的狀況,
很多時候從紫微的夫妻宮可以看出對方近期的狀態變化,
是真的有外人介入、還是夫妻間其他能量出問題。
方便問您是想找個明確的答案,還是想了解該怎麼處理?
我先讓老師了解您的狀況,給您一些方向。

▌範本 4:議價型客戶 → 先分辨類型再處理（Feel-Felt-Found）
客戶:太貴了 有沒有便宜一點的?
你:老師了解，費用確實不是小數目 🙏
想先請問您，說貴——是現在手頭比較緊，
還是不確定這次諮詢對您有沒有幫助？
（這樣我可以幫您找最適合的方式）

→ 如果是「手頭緊」：
老師有準備入門選項——易經占卜 NT$ 1,000，針對您最在意的那一個問題，
10-15 分鐘就能拿到方向，先感受一下老師的分析方式 😊

→ 如果是「不確定值不值」：
老師懂這個感覺，很多來找老師的朋友第一次也有這樣的疑慮。
老師這樣說——您剛才提到這個狀況已經困擾您一段時間了，
如果這次諮詢讓您對這件事有了清楚的方向、不再打轉，
這對您來說是什麼價值？（讓客戶自己說出值不值）

▌範本 5:猶豫型客戶 → 先找出真正顧慮，再軟收單
客戶:我再考慮看看
你:沒問題，老師不急 🙏
不過想請問您，說再想想——
是有哪個地方還不確定，還是時間上不方便？
說出來我幫您解答，比較不會讓您空等。

（客戶說出顧慮後才加這句）
只是想跟您說，老師最近檔期比較滿，
這個月還剩幾個諮詢名額。
我可以先幫您「保留」一個位子，不用先付款，
您決定好再說。要不要先留您的稱呼？

▌範本 6:已準備購買 → 直接收單
客戶:好,那幫我安排
你:太好了 ✨ 請提供以下三項給我:
1. 您的稱呼
2. 國曆出生年月日(時間能精確到幾點幾分最好)
3. 出生地(縣市即可)
收到後我會立即幫您登記,
並把付款方式(匯款帳號 / LINE Pay)發給您。

═══════════════════════════════
【其他常見情境處理】
═══════════════════════════════
■ 客戶問「算命準嗎？會不會是騙人的？」
「老師很喜歡這個問題。命理不是算一個固定的未來——
而是幫您看清楚自己的格局和這段時間的能量走向，讓您做決定的時候更有根據。
準不準，老師不怕您驗證。您先說說您的狀況，老師給您一個初步的感受，您自己判斷就好。」

■ 客戶問「可以先問一個問題嗎？」
「您的問題老師很想幫您看，不過一兩句話容易片面，反而幫不了您。
老師這樣——您把最困擾您的狀況說清楚，老師先給您一個方向感，
您再決定要不要進一步分析，這樣對您比較公平。」

■ 客戶說「朋友說算命不準」
「多聽幾個方向本來就是對的。老師只補充一點——每個老師看事情的流派不同，
您可以把最困擾您的那個問題說給老師聽，老師給您初步感受，您再做比較也不遲。」

═══════════════════════════════
【軟成交：用選擇題取代是非題】
═══════════════════════════════
❌ 不要問：「您要預約嗎？」
✅ 改問：
- 「老師這邊白天和晚上都有時段，您通常哪個時間比較方便？」
- 「易經占卜和紫微命盤都適合您的狀況，您想先從哪個開始？」
- 「您比較喜歡文字報告，還是跟老師直接視訊聊？」

═══════════════════════════════
【高意願客戶的「軟收單」用語】
═══════════════════════════════
- 「老師最近檔期比較滿,這個月還剩幾個名額,要我幫您先預留嗎?」
- 「方便提供您的生辰(年月日時)和稱呼嗎?我幫您登記,老師會優先安排」
- 「付款方式有匯款和 LINE Pay,您比較方便哪一種?」
- 「您先付訂金就可以鎖定名額,完整諮詢後再補尾款」

═══════════════════════════════
【絕對不能說的話(合規 + 銷售大忌)】
═══════════════════════════════
- ❌ 不能保證:「一定會」「保證能」「百分百」
- ❌ 不能涉及:醫療診斷、投資建議、藥物用法
- ❌ 不能說:「您要不要算算看?」(太弱)→ 改成「我幫您預約老師檔期」
- ❌ 不能說:「不知道」「無法回答」→ 改成「這個老師會親自為您說明,我先幫您登記」
- ❌ 不能說:「請聯繫老師」(等於把客戶推走)→ 改成「我直接幫您安排老師的時間」

═══════════════════════════════
【關鍵心法】
═══════════════════════════════
每一則回覆,在送出前內心問自己:
「這句話會讓客戶『更想預約』,還是只是『得到答案』?」
如果只是後者,重寫。
"""

SYSTEM_PROMPT += f"""
═══════════════════════════════
【預約流程（有意願後的資料收集）】
═══════════════════════════════
當客戶確認要預約時，逐步收集資料——每次只問一項，不要一口氣全列出來：

依服務項目所需資料：
• 紫微命盤 / 文字問命 / 視訊諮詢 → 姓名（稱呼）、國曆出生年月日、出生時間（不確定填「不詳」）、出生地（縣市）、主要想了解的問題
• 視訊諮詢 → 以上＋「方便提供 2–3 個可用時段嗎？」
• 居家風水 → 姓名、住家照片（至少 5 張）、格局說明或平面圖、入住時間
• 改名 / 命名 → 姓名、出生資料（改名）或父母姓名及對孩子名字的期許方向（命名）

確認付款提示語：
「資料都收到了，我幫您登記 ✅
付款確認後名額就正式鎖定，我這就把付款方式發給您！」

取消 / 改期政策（客戶詢問時才說明，不要主動提）：
• 提前 48 小時改期：免費一次
• 24 小時內改期：收取 10% 手續費
• 當天取消：不退款
• 特殊狀況（急病等）：請說明情形，由老師親自決定

候補名單話術：
「目前老師的檔期比較滿，我幫您加入候補名單，有空位時優先通知您 😊
請先留下您的稱呼和想諮詢的服務項目，等消息喔！」

═══════════════════════════════
【售後關懷（已完成服務的回頭客）】
═══════════════════════════════
當對話中出現「上次老師幫我看」「之前諮詢過」「報告我收到了」等回頭跡象時，切換到關懷模式：

回頭客的第一則回覆：
先確認並表示歡迎，例如：
「好久不見～上次的分析有幫到您嗎？有什麼新的問題想聊嗎？」

邀請留評價（服務完成後的客戶）：
「如果這次老師的分析對您有幫助，能不能幫老師留一句話的評價？😊
您的回饋能讓更多需要的人找到老師 🙏
（直接回覆這邊就可以，我會轉給老師）」

轉介紹激勵（評價收到後再提，不要同一訊息多管齊下）：
「如果您身邊有朋友也有類似的困擾，歡迎介紹過來 😊
您推薦的朋友完成首次預約後，您和朋友各獲得 NT$200 折扣券！」

沉睡客戶喚回（對話提示超過 6 個月未互動）：
「好久不見！老師最近整理了一些跟您命格相近的案例，
有些方向想跟您分享 😊
最近有沒有特別在意的事？這個月有預約優惠，歡迎回來聊聊！」

注意：每次只做一個行動呼籲——不要在同一訊息裡同時推評價、轉介紹、再購買。

═══════════════════════════════
【匯款資訊(客戶確認預約後才提供)】
═══════════════════════════════
{PAYMENT_INFO}
"""

SYSTEM_PROMPT += """
═══════════════════════════════
【防捏造護欄（最高優先，違反視為嚴重錯誤）】
═══════════════════════════════
- 除非客戶在最近對話裡「已經實際提供預約資料」（生辰、稱呼）或「明確說要下單／幫我安排」，否則絕對禁止說出：「收到了」「幫您登記」「幫您排隊」「已幫您安排」「匯款」「付款確認」這類已成交語氣。沒成交卻說成交，會讓客戶以為被詐騙，是最嚴重的錯誤。
- 遇到看不懂、太短、或只有一兩個字／一個數字／貼圖的訊息，不要腦補成下單確認，也不要硬收單——先用一句話禮貌確認對方的意思（例：「想先確認一下，您是想了解哪方面呢？」）。
- 「測驗」「小測」等互動字眼由系統另外處理，不屬於預約流程；萬一你收到這類訊息，只需親切回應、不要導向付款。
"""


# ============================================================
# 對話記憶輔助函式
# ============================================================
def get_user_history(user_id):
    """取出客戶的對話記憶,並清除過期內容。"""
    history = conversation_memory[user_id]
    now = datetime.now()

    if history and (now - history[-1]["time"]) > timedelta(hours=MEMORY_EXPIRE_HOURS):
        logger.info(f"客戶 {user_id[:8]}... 記憶已過期,重新開始對話")
        history.clear()

    return history


def append_to_history(user_id, role, content):
    """把訊息加入客戶記憶,並限制總長度避免無限累積。"""
    conversation_memory[user_id].append({
        "role": role,
        "content": content,
        "time": datetime.now()
    })

    max_messages = MEMORY_TURNS * 2  # user + assistant 各算一則
    if len(conversation_memory[user_id]) > max_messages:
        conversation_memory[user_id] = conversation_memory[user_id][-max_messages:]

    if role == "user":
        _intel_pending.add(user_id)   # B：標記此對話有新活動，待閒置後摘要


# ============================================================
# 純文字化：移除 markdown 符號（LINE / IG 不會渲染 markdown）
# ============================================================
def strip_markdown(text):
    """把 AI 回覆裡的 markdown 星號等符號清掉，避免 LINE/IG 直接顯示 ** * # ` 這些符號。"""
    if not text:
        return text
    import re
    # 粗體/斜體：***x*** **x** *x* → 只留內文
    text = re.sub(r'\*{1,3}([^*\n]+?)\*{1,3}', r'\1', text)
    # 行首標題符號 #（避免誤刪句中井字，只處理行首）
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s+', '', text)
    # 行首清單星號 "* " → 全形頓號
    text = re.sub(r'(?m)^(\s*)\*\s+', r'\1・', text)
    # 殘留的星號與反引號一律清掉（不動底線，避免誤傷 email/ID）
    text = text.replace('*', '').replace('`', '')
    return text


# ============================================================
# 熱客戶判斷
# ============================================================
def is_hot_lead(user_message, ai_reply):
    """判斷是否為高購買意願客戶,需要老師親自接手。"""
    combined = user_message + ai_reply
    return any(keyword in combined for keyword in HOT_LEAD_KEYWORDS)


# ============================================================
# Email 通知
# ============================================================
def send_email_notification(user_id, user_message, bot_reply, is_hot=False):
    """發送對話記錄到管理員信箱,熱客戶會加紅旗標記。"""
    try:
        flag = "🔥【熱客戶 - 請優先回覆】" if is_hot else "【一般對話】"
        subject = f"{flag} 五木老師客服 - {user_id[:8]}..."

        urgency_note = "⚡ 此客戶展現高購買意願,建議盡快親自接手聯繫" if is_hot else ""
        content = f"""{flag}

【客戶 ID】{user_id}
【時間】{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

【客戶訊息】
{user_message}

【AI 回覆】
{bot_reply}

──────────────────────────
{urgency_note}
"""
        msg = MIMEText(content, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = GMAIL_USER
        msg['To'] = NOTIFY_EMAIL

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"Email 已寄出 (hot={is_hot})")
    except Exception as e:
        logger.error(f"Email 發送失敗: {e}")


# ============================================================
# Welcome Flow 輔助函式
# ============================================================
# ============================================================
# 60 秒易經財運小測（確定性流程，不經 AI）
# ============================================================
QUIZ_TRIGGERS = {
    "測驗", "小測", "測驗！", "紫微小測", "財運測驗", "財運小測",
    "60秒小測", "60 秒小測", "測驗開始", "開始測驗", "我要測驗",
}

QUIZ_INTRO = (
    "來玩囉～這是五木老師的 60 秒財運直覺小測 ✨\n\n"
    "先深呼吸，別想太多——\n"
    "下面四個數字，直覺選一個，直接回覆給我：\n\n"
    "453　980　537　787"
)

_QUIZ_TAIL = (
    "\n\n這只是 60 秒的直覺速覽，看個大方向就好。\n"
    "想知道你完整命盤的財運怎麼走、哪幾個月是關鍵，"
    "跟小林說一聲，幫你安排老師 🙂"
)

QUIZ_READINGS = {
    "453": (
        "453 → 困卦 ☵（回春型財運）\n\n"
        "困卦，水困於澤中，困而不失其所。\n\n"
        "下半年財運像長跑的第二春——前段耗力，中段蓄積，後段爆發。\n\n"
        "適合：重啟停擺的計畫、回頭看被擱置的機會，時機到一推即發。\n\n"
        "提醒：困境不是終點，是轉機前的醞釀期。\n\n"
        "【困中生機，後發先至】" + _QUIZ_TAIL
    ),
    "980": (
        "980 → 泰卦 ☷（豐盛型財運）\n\n"
        "泰卦，天地相交，氣場暢通無阻。\n\n"
        "980 落在泰卦，下半年財氣旺盛——正財偏財都有機會，貴人也比平時多。\n\n"
        "適合：大膽出手、擴大佈局、把之前猶豫的合作或投資付諸行動。\n\n"
        "提醒：泰極生否，旺的時候同步守成，不要散財。\n\n"
        "【天地交泰，財氣暢通】" + _QUIZ_TAIL
    ),
    "537": (
        "537 → 謙卦 ☶（積累型財運）\n\n"
        "謙卦，山藏地中，滿招損謙受益。\n\n"
        "537 不走表面風光路線——低調中積累，厚積薄發是這一型的節奏。\n\n"
        "適合：鞏固本業、累積口碑、耕耘長期客戶關係，不急著出風頭。\n\n"
        "提醒：謙不是軟弱，是厚積薄發的底氣。\n\n"
        "【低調積累，厚積薄發】" + _QUIZ_TAIL
    ),
    "787": (
        "787 → 鼎卦 ☲（轉化型財運）\n\n"
        "鼎卦，革故鼎新，以舊換新。\n\n"
        "787 的財運關鍵字是「轉」——轉型、轉換跑道、把技能轉成實際收入。\n\n"
        "適合：斜槓發展、把興趣變現、開創第二收入，不要只靠一條路。\n\n"
        "提醒：鼎需要火候，急不得，也不能一直溫吞。\n\n"
        "【革故鼎新，開創新局】" + _QUIZ_TAIL
    ),
}


# ============================================================
# 心理測驗小遊戲（D+3 / D+7 / D+14 主動發送觸發，見『心理測驗小遊戲_v1.md』）
# 只有 Google Sheet『LINE好友清單』J 欄=遊戲狀態(Q1/Q2/Q3) 的用戶回 A/B/C/D 才判讀
# ============================================================
_GAME_D14_TAIL = (
    "\n\n不管你今年想主動出擊、還是先穩住等時機，小林都放在心上。\n"
    "以後想聊命理、風水，或想看看自己的流年，隨時回我一聲就好，"
    "五木老師這邊一直都在。祝你這段路走得安穩。"
)

GAME_READINGS = {
    # 測驗 1（D+3）本命能量
    "Q1": {
        "A": (
            "你是火型能量。天生行動力強、想到就想做，站在人群裡會發光。\n"
            "五木老師常說，火旺的人不缺衝勁，缺的是「收」的時機——\n"
            "懂得在對的時候停一下，火才會旺得久，而不是燒完就累。\n"
            "如果你好奇自己今年火勢往哪走，可以跟我說，我幫你問老師。"
        ),
        "B": (
            "你是木型能量。重成長、愛規劃，做事有條理，是會慢慢往上長的類型。\n"
            "老師說木型的人最穩，但有時候太求穩、錯過該出手的點。\n"
            "今年哪幾個月適合你放膽往前，其實命盤看得出來。想了解隨時找小林。"
        ),
        "C": (
            "你是水型能量。直覺敏銳、心思細，很能感受別人沒說出口的東西。\n"
            "老師說水型的人靈氣足，最怕的是想太多、把自己困住。\n"
            "若你最近正卡在某個放不下的關口，可以跟我說，看看老師怎麼解。"
        ),
        "D": (
            "你是土型能量。務實、重安全感，是身邊人最信得過的那一個。\n"
            "老師說土型的人底盤穩，缺的常常是一個「敢突破」的契機。\n"
            "今年有沒有適合你踏出去的時機點，命盤會告訴你。想看的回我一聲。"
        ),
    },
    # 測驗 2（D+7）今年最該補的運（結果帶流年速覽報告 CTA）
    "Q2": {
        "A": (
            "你直覺選了「財」。今年你心裡最在意的，其實是「守得住、理得順」。\n"
            "五木老師說，財運好不好，一半看天給的流年，一半看你有沒有踩到破財的位置——\n"
            "家裡財位、辦公桌方向，都會影響。想看你今年財的完整節奏，"
            "五木老師的流年速覽報告（NT$499）會幫你整理好，回我「報告」就行。"
        ),
        "B": (
            "你選了「緣」。今年對你來說，關係是關鍵字——不管是感情，還是合作的人。\n"
            "老師說，緣分不是等來的，是走對位置、遇對時機。\n"
            "你今年桃花與貴人往哪個方向走，命盤看得到。想要完整版，回我「報告」，"
            "我把流年速覽報告（NT$499）的說明傳給你。"
        ),
        "C": (
            "你選了「貴」。今年很適合你主動一點，去拓展、去求助，貴人就在動起來的路上。\n"
            "老師說貴人運強的年份若只守著不動，等於把好牌放著不打。\n"
            "哪幾個月是你的貴人檔期，五木老師的流年速覽報告（NT$499）會標出來，回我「報告」就幫你看。"
        ),
        "D": (
            "你選了「安」。你最近可能有點累，身體或心裡想先把自己穩住。\n"
            "老師說，先安人再談運——睡不好、心浮，很多時候跟臥室的格局有關。\n"
            "如果你想先把今年的節奏看清楚再調整，回我「報告」，流年速覽報告（NT$499）會整理給你。"
        ),
    },
    # 測驗 3（D+14）決策風格（結果帶溫暖收尾，取代原「最後一次主動」）
    "Q3": {
        "A": (
            "你是行動派，看準了就走，這是很多人羨慕的果斷。\n"
            "五木老師常說，命盤像地圖——行動派最大的優勢是敢出發，\n"
            "要留意的是「別在逆風的流年衝太快」。你今年是順風還是逆風，命盤看得出來。"
            + _GAME_D14_TAIL
        ),
        "B": (
            "你是平衡派，重過程也重風景，穩中求進，不容易走偏。\n"
            "老師說這種人走得久，但偶爾會因為想面面俱到而錯過時機。\n"
            "哪個月適合你收、哪個月適合你放，其實可以先看清楚。"
            + _GAME_D14_TAIL
        ),
        "C": (
            "你是開創派，敢走別人沒走過的路，格局往往也開在這裡。\n"
            "老師說開創的人最需要的是「貴人」與「時機」這兩張牌。\n"
            "你今年的開創檔期落在哪，命盤會告訴你。"
            + _GAME_D14_TAIL
        ),
        "D": (
            "你是謀定派，善於等時機、不打沒把握的仗，這是難得的定力。\n"
            "老師提醒謀定派一件事：別因為太會等，錯過了該出手的那個彎。\n"
            "你命盤裡今年的「出手點」在哪，我可以幫你問老師。"
            + _GAME_D14_TAIL
        ),
    },
}


# ============================================================
# 易經抽牌小測（主動發送觸發，見『易經抽牌小測_導占卜_v1.md』）
# 只有 Sheet J 欄=抽牌狀態(D1/D2/D3) 的用戶回 1/2/3/4 才判讀；導向 L1 易經占卜
# 狀態碼與心理測驗(Q1-3 回 A/B/C/D)、財運小測(3 位數字)三者不衝突
# ============================================================
_DRAW_TAIL = (
    "\n\n—\n"
    "易經占卜為單題快速指引（NT$1,000/題），僅供文化研究與生活參考，"
    "不構成醫療、投資、法律等決策依據。"
)

DRAW_READINGS = {
    # 測驗 A（D1）你現在卡住的那一關
    "D1": {
        "1": (
            "你抽到的是「霧裡的一條路」→ 蒙卦（山水蒙）\n\n"
            "這是「還沒看清」的狀態。方向感被霧擋著，不是你不行，是資訊還不夠。\n"
            "五木老師說，這種時候最忌硬闖，先把「到底在問什麼」問清楚，霧自己會散一半。\n"
            "蒙卦講的正是「不懂就問」——問對了，路才顯出來。\n\n"
            "你心裡那個具體的問題，其實可以就它單獨起一卦來看。想試的話回我「占卜」。"
            + _DRAW_TAIL
        ),
        "2": (
            "你抽到的是「一扇緊閉的門」→ 需卦（水天需）\n\n"
            "這是「時機未到」。門不是打不開，是現在還沒到開的時候。\n"
            "老師說，需卦的等待不是停滯，是蓄力——把該備的備好，門一鬆你就進得去。\n"
            "難的是分辨「還要等多久、等的時候該做什麼」。\n\n"
            "這種「什麼時候動」的問題，起一卦看你當下的時位最清楚，想看回我「占卜」。"
            + _DRAW_TAIL
        ),
        "3": (
            "你抽到的是「一口淤住的井」→ 井卦（水風井）\n\n"
            "這是「有水、但通道淤了」。你的實力跟資源都在，卡的是動線沒疏通。\n"
            "五木老師說，井卦提醒的是「修井」——理順關係、流程或環境，水自然湧上來。\n"
            "到底淤在哪一段，往往不是自己看得最清楚。\n\n"
            "如果想針對你這口井找出淤點，可以起一卦來對，回我「占卜」我幫你安排。"
            + _DRAW_TAIL
        ),
        "4": (
            "你抽到的是「一顆快裂開的種子」→ 夬卦（澤天夬）\n\n"
            "這是「離突破只差一步」。該決斷的點到了，種子的殼正要裂開。\n"
            "老師說夬卦最怕的是「明明可以了卻還在猶豫」，一猶豫就錯過那口氣。\n"
            "但要不要現在破、往哪個方向破，得看你當下的勢。\n\n"
            "想確認這一步該不該踏、往哪踏，起一卦最準，回我「占卜」。"
            + _DRAW_TAIL
        ),
    },
    # 測驗 B（D2）一段正在你心上的關係
    "D2": {
        "1": (
            "你抽到的是「並肩生長的兩株樹」→ 漸卦（風山漸）\n\n"
            "這是「慢慢長」的關係。它不轟烈，但根在往下扎，是會走長的那種。\n"
            "五木老師說，漸卦最忌催——你越想快點確定，它越彆扭。\n"
            "給它時間，也給自己時間。\n\n"
            "如果你想看這段關係接下來的節奏往哪走，可以就它起一卦，回我「占卜」。"
            + _DRAW_TAIL
        ),
        "2": (
            "你抽到的是「背對背的兩面鏡」→ 睽卦（火澤睽）\n\n"
            "這是「角度不同」。不是不合，是你們看同一件事的位置不一樣。\n"
            "老師說睽卦的解法不是逼對方轉過來，是先看懂「差在哪一格」。\n"
            "看懂了，很多僵局其實只是誤會。\n\n"
            "想釐清你們卡在哪個角度、怎麼接回來，起一卦看得比較透，回我「占卜」。"
            + _DRAW_TAIL
        ),
        "3": (
            "你抽到的是「隔著霧的一次照面」→ 咸卦（澤山咸）\n\n"
            "這是「有感應、但還沒說開」。彼此心裡是有的，只是隔著一層沒點破。\n"
            "五木老師說，咸卦講的是真心的感通——不用算計，順著那份感覺走就對了。\n"
            "難的是「要不要主動、什麼時候主動」。\n\n"
            "這種時機問題，依你當下的能量起一卦最清楚，想看回我「占卜」。"
            + _DRAW_TAIL
        ),
        "4": (
            "你抽到的是「一座年久待修的橋」→ 蠱卦（山風蠱）\n\n"
            "這是「有舊帳待整理」。橋還在，但積了些沒處理的東西，走起來卡卡的。\n"
            "老師說蠱卦不是壞卦，是「該修的時候到了」——修好反而比以前穩。\n"
            "只是先修哪一根樑、從哪頭下手，得看實際情形。\n\n"
            "想找出這段關係該先理哪一塊，可以起一卦來對，回我「占卜」。"
            + _DRAW_TAIL
        ),
    },
    # 測驗 C（D3）一個你還沒決定的選擇
    "D3": {
        "1": (
            "你抽到的是「原地站著，先觀察」→ 艮卦（艮為山）\n\n"
            "這是「該止則止」。你現在的觀望不是拖延，是身體比腦子先知道「還不到動的時候」。\n"
            "五木老師說，艮卦是懂得停的智慧——停在對的地方，比亂動更有力量。\n"
            "只是「要停到什麼訊號出現才動」，這條線每個人不一樣。\n\n"
            "想知道你這件事的「動點」在哪，可以起一卦來看，回我「占卜」。"
            + _DRAW_TAIL
        ),
        "2": (
            "你抽到的是「順著水流往下走」→ 隨卦（澤雷隨）\n\n"
            "這是「順勢」。這件事適合跟著時機走，不必硬要自己開路。\n"
            "老師說隨卦不是隨便，是「看懂勢往哪去、然後跟上」。\n"
            "關鍵在分辨「這股勢是真的順、還是一時熱鬧」。\n\n"
            "想確認你要跟的這股勢靠不靠得住，起一卦最實在，回我「占卜」。"
            + _DRAW_TAIL
        ),
        "3": (
            "你抽到的是「逆著水往上游」→ 蹇卦（水山蹇）\n\n"
            "這是「目前逆風」。不是不能做，是硬頂的話耗損會很大。\n"
            "五木老師說蹇卦的智慧是「遇阻繞路、找貴人」——換個走法，難關就沒那麼難。\n"
            "往哪繞、找誰幫，往往就是差這一步的資訊。\n\n"
            "想看你這條路怎麼繞最省力，可以就它起一卦，回我「占卜」。"
            + _DRAW_TAIL
        ),
        "4": (
            "你抽到的是「一把火，全部重來」→ 革卦（澤火革）\n\n"
            "這是「到了該變的點」。心裡其實已經有答案，只是還沒敢真的翻篇。\n"
            "老師說革卦講「時機對了，變則通」——但革也要挑時候，早了晚了都費力。\n"
            "你這一把火，現在點是不是時候？\n\n"
            "這種「該不該變、什麼時候變」的問題，正是易經占卜最擅長回答的，回我「占卜」我幫你安排。"
            + _DRAW_TAIL
        ),
    },
}


# 「占卜」關鍵字：抽牌伏筆收束 → 導 L1 易經占卜（NT$1,000/題）
DIVINATION_TRIGGERS = {
    "占卜", "我要占卜", "我想占卜", "想占卜", "易經占卜", "起卦", "我要起卦",
}

DIVINATION_REPLY = (
    "好的，這就是五木老師的易經占卜。\n\n"
    "作法是：就你心裡那一件具體的事，老師依你當下的能量單獨起一卦、解給你聽——\n"
    "單題快速指引，一題 NT$1,000，大約 10 到 15 分鐘。\n\n"
    "你可以先把想問的那一件事，用一句話告訴我"
    "（例如某個決定、某段關係、或某個卡住的狀況），\n"
    "我幫你把問題整理好、安排老師的時間。\n\n"
    "—\n"
    "易經占卜為單題快速指引（NT$1,000/題），僅供文化研究與生活參考，"
    "不構成醫療、投資、法律等決策依據。"
)


def build_divination_quick_reply():
    """抽牌判讀後導占卜用的快速回覆按鈕。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="我想深入占卜", text="占卜")),
    ])


def build_draw_quick_reply():
    """抽牌選數字用的快速回覆按鈕（供主動發送開場題時附帶）。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="1", text="1")),
        QuickReplyItem(action=MessageAction(label="2", text="2")),
        QuickReplyItem(action=MessageAction(label="3", text="3")),
        QuickReplyItem(action=MessageAction(label="4", text="4")),
    ])


def build_quiz_quick_reply():
    """測驗選數字用的快速回覆按鈕。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="453", text="453")),
        QuickReplyItem(action=MessageAction(label="980", text="980")),
        QuickReplyItem(action=MessageAction(label="537", text="537")),
        QuickReplyItem(action=MessageAction(label="787", text="787")),
    ])


def build_quick_reply():
    """建立標準 5 按鈕 Quick Reply，供歡迎訊息與每次 AI 回覆共用。"""
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="感情", text="感情")),
        QuickReplyItem(action=MessageAction(label="事業財運", text="事業財運")),
        QuickReplyItem(action=MessageAction(label="健康", text="健康")),
        QuickReplyItem(action=MessageAction(label="流年運勢", text="流年運勢")),
        QuickReplyItem(action=MessageAction(label="隨意聊聊", text="隨意聊聊")),
    ])


def build_welcome_message():
    """建立加好友歡迎訊息，附帶 Quick Reply 按鈕。"""
    quick_reply = build_quick_reply()
    welcome_text = (
        "歡迎找五木老師 🙏\n\n"
        "我是小林，老師的助理。\n"
        "老師多年來陪伴很多朋友走過感情、事業、家運的關卡。\n\n"
        "請問您今天找來，最放在心上的是哪個方向？\n"
        "說說看，不用整理成問題，說狀況就好。"
    )
    return TextMessage(text=welcome_text, quick_reply=quick_reply)


# ============================================================
# 真人感回覆：輸入中動畫 + 依長度停頓（模擬打字）
# ============================================================
def show_loading_animation(user_id, seconds=5):
    """顯示 LINE『輸入中』動畫（1:1 聊天）。失敗不影響主流程。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not user_id or user_id == "anonymous":
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/chat/loading/start",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                     "Content-Type": "application/json"},
            json={"chatId": user_id, "loadingSeconds": seconds},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"loading 動畫失敗: {e}")


def typing_delay(text):
    """依回覆長度模擬打字停頓：0.8s 起、每字加一點、上限 4.5s（避免 reply token 過期）。"""
    return min(0.8 + len(text) / 28.0, 4.5)


def human_reply(reply_token, user_id, text, quick_reply=None, thinking=0.0):
    """模擬真人：輸入中動畫→依長度停頓（扣掉已花的思考時間）→回覆。"""
    show_loading_animation(user_id)
    time.sleep(max(typing_delay(text) - thinking, 0.4))
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text, quick_reply=quick_reply)],
            )
        )


# ============================================================
# Flask 路由
# ============================================================
@app.route("/", methods=['GET'])
def home():
    """健康檢查與線上狀態顯示。"""
    return f"五木老師智能客服運行中 | 目前在線記憶客戶數: {len(conversation_memory)}"


@app.route("/webhook", methods=['POST'])
def webhook():
    """LINE 平台呼叫的入口。"""
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("無效的 LINE 簽章,可能是非 LINE 平台來的請求")
        abort(400)

    return 'OK'


# ============================================================
# /admin 人工接手控制台（密碼登入）
# ============================================================
def _esc(s):
    return (str(s) if s is not None else '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _admin_logged_in():
    return session.get('admin') is True


LOGIN_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>五木老師客服控制台</title>
<style>body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f0e8d0;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:#fff;padding:32px 28px;border-radius:14px;box-shadow:0 6px 24px rgba(0,0,0,.12);width:min(92vw,340px)}
h1{font-size:19px;margin:0 0 4px;color:#2b2b2b;letter-spacing:1px}p{color:#999;font-size:13px;margin:0 0 20px}
input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccc;border-radius:8px;font-size:16px;margin-bottom:12px}
button{width:100%;padding:12px;border:0;border-radius:8px;background:#8c1c1c;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
.err{color:#8c1c1c;font-size:13px;margin-bottom:10px}</style>
<div class=card><h1>五木老師 · 客服控制台</h1><p>人工接手管理</p>
__ERR__
<form method=post action="/admin/login">
<input type=password name=password placeholder="登入密碼" autofocus>
<button type=submit>登入</button></form></div>"""


PANEL_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>五木老師客服控制台</title>
<style>*{box-sizing:border-box}
body{font-family:-apple-system,"Microsoft JhengHei",sans-serif;background:#f0e8d0;margin:0;color:#2b2b2b}
header{background:#1a1f3a;color:#c9a84c;padding:15px 18px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0}
header h1{font-size:16px;margin:0;letter-spacing:2px}
header button{background:none;border:1px solid #c9a84c;color:#c9a84c;padding:6px 12px;border-radius:6px;font-size:13px;cursor:pointer}
.wrap{max-width:560px;margin:0 auto;padding:16px}
.count{color:#888;font-size:13px;margin:2px 2px 14px}
.row{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
.name{font-weight:700;font-size:16px}
.meta{color:#999;font-size:12px;margin:2px 0 6px}
.b{display:inline-block;font-size:12px;padding:3px 9px;border-radius:20px;background:#eee;color:#666}
.b.on{background:#fbe9e7;color:#8c1c1c;font-weight:700}
.row form{margin:0}
.row button{border:0;border-radius:8px;padding:10px 14px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap}
button.pause{background:#8c1c1c;color:#fff}
button.resume{background:#2c8b3a;color:#fff}
.empty{color:#888;text-align:center;padding:40px 0}
.note{color:#aaa;font-size:12px;text-align:center;margin:18px 0}</style>
<header><h1>五 木 老 師 · 客服控制台</h1>
<form method=post action="/admin/logout"><button>登出</button></form></header>
<div class=wrap>
<div class=count>共 __COUNT__ 位客戶 · 暫停後 2 小時自動恢復 AI</div>
__BODY__
<div class=note>暫停中的客戶，AI 不會自動回覆，請用 LINE 官方帳號後台手動處理。</div>
</div>"""


def render_admin_panel():
    rows = []
    ws = _get_worksheet("LINE好友清單")
    if ws:
        try:
            data = ws.get_all_values()
            for idx, r in enumerate(data[1:], start=2):
                uid = (r[0] if len(r) > 0 else '').strip()
                if not uid:
                    continue
                name = (r[1] if len(r) > 1 else '').strip()
                status = (r[4] if len(r) > 4 else '').strip()
                rows.append((idx, uid, name, status))
        except Exception as e:
            logger.error(f"控制台讀取客戶失敗: {e}")
    now = datetime.now()
    items = []
    for rownum, uid, name, status in rows:
        if not name:
            fetched = get_line_display_name(uid)
            if fetched:
                name = fetched
                threading.Thread(target=_backfill_name, args=(rownum, fetched), daemon=True).start()
        name = name or ('客戶 ' + uid[-6:])
        until = handoff_until.get(uid)
        if until and until > now:
            mins = int((until - now).total_seconds() // 60)
            badge = f'<span class="b on">人工接手中 · 剩 {mins} 分</span>'
            btn = '<button name=action value=resume class=resume>恢復 AI</button>'
        else:
            badge = '<span class="b">AI 自動回覆</span>'
            btn = '<button name=action value=pause class=pause>暫停 2 小時</button>'
        items.append(
            '<div class=row><div><div class=name>' + _esc(name) + '</div>'
            '<div class=meta>' + _esc(status) + ' · ' + _esc(uid[-8:]) + '</div>' + badge + '</div>'
            '<form method=post action="/admin/toggle"><input type=hidden name=user_id value="' + _esc(uid) + '">' + btn + '</form></div>'
        )
    body = "".join(items) or '<p class=empty>目前沒有客戶資料。</p>'
    return PANEL_HTML.replace("__COUNT__", str(len(rows))).replace("__BODY__", body)


@app.route("/admin", methods=['GET'])
def admin_home():
    if not ADMIN_PASSWORD:
        return Response("尚未設定 ADMIN_PASSWORD 環境變數，管理台未啟用。", mimetype="text/plain")
    if not _admin_logged_in():
        return Response(LOGIN_HTML.replace("__ERR__", ""), mimetype="text/html")
    return Response(render_admin_panel(), mimetype="text/html")


@app.route("/admin/login", methods=['POST'])
def admin_login():
    if ADMIN_PASSWORD and request.form.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return redirect("/admin")
    return Response(LOGIN_HTML.replace("__ERR__", '<div class=err>密碼錯誤</div>'), mimetype="text/html")


@app.route("/admin/logout", methods=['POST'])
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.route("/admin/toggle", methods=['POST'])
def admin_toggle():
    if not _admin_logged_in():
        abort(403)
    uid = request.form.get('user_id', '').strip()
    action = request.form.get('action', '')
    if uid:
        set_handoff(uid, action == 'pause')
        logger.info(f"🖥️ 控制台 {action} [{uid[:8]}...]")
    return redirect("/admin")


@app.route("/admin/run-draw-d5", methods=['GET'])
def admin_run_draw_d5():
    """手動觸發 D+5 抽牌掃描（測試用）。需先登入 /admin。"""
    if not _admin_logged_in():
        abort(403)
    threading.Thread(target=scan_draw_d5, daemon=True).start()
    logger.info("🖥️ 控制台手動觸發 D+5 抽牌掃描")
    return Response("D+5 抽牌掃描已觸發，請看 Render logs 與測試手機。",
                    mimetype="text/plain; charset=utf-8")


# ============================================================
# 加好友事件：發送歡迎訊息
# ============================================================
@handler.add(FollowEvent)
def handle_follow(event):
    """新客戶加好友時，自動發送歡迎訊息與 Quick Reply 選單。"""
    user_id = event.source.user_id if event.source.user_id else "anonymous"
    logger.info(f"🎉 新好友加入: [{user_id[:8]}...]")

    # 發送歡迎訊息
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[build_welcome_message()]
            )
        )

    # WF-1：背景寫入 Google Sheet（不阻塞 LINE webhook 回應）
    threading.Thread(
        target=log_new_follower,
        args=(user_id, "LINE_OA"),
        daemon=True
    ).start()


# ============================================================
# 訊息處理主邏輯
# ============================================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """處理客戶送來的文字訊息。"""
    user_id = event.source.user_id if event.source.user_id else "anonymous"
    user_message = event.message.text

    logger.info(f"📩 收到 [{user_id[:8]}...]: {user_message}")

    # 首次訊息時載入人工接手名單（服務重啟後恢復暫停狀態）
    if not _handoffs_loaded:
        load_handoffs()

    # 老師管理指令（只有 ADMIN_USER_IDS 能用）：#接手 / #放手 / #接手中
    if user_id in ADMIN_USER_IDS and user_message.strip().startswith("#"):
        cmd = user_message.strip()
        reply_text = None
        if cmd.startswith("#接手中"):
            active = [u for u in list(handoff_until) if is_paused(u)]
            if active:
                reply_text = "目前人工接手中：\n" + "\n".join(f"・{u[-8:]}" for u in active)
            else:
                reply_text = "目前沒有人工接手中的客戶。"
        elif cmd.startswith("#接手"):
            token = cmd[len("#接手"):].strip()
            target = resolve_customer(token)
            if target:
                set_handoff(target, True)
                reply_text = f"已接手 {target[-8:]}，AI 已暫停自動回覆。處理完傳「#放手 {target[-8:]}」恢復。"
            else:
                reply_text = f"找不到客戶「{token}」。可用完整 UserID、末碼或姓名。"
        elif cmd.startswith("#放手"):
            token = cmd[len("#放手"):].strip()
            target = resolve_customer(token)
            if target:
                set_handoff(target, False)
                reply_text = f"已放手 {target[-8:]}，AI 恢復自動回覆。"
            else:
                reply_text = f"找不到客戶「{token}」。"
        if reply_text is not None:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message_with_http_info(
                    ReplyMessageRequest(reply_token=event.reply_token,
                                        messages=[TextMessage(text=reply_text)])
                )
            return

    # 人工接手中的客戶：AI 完全不回，交給老師用 LINE 官方帳號後台手動處理
    if is_paused(user_id):
        logger.info(f"🙋 人工接手中，AI 略過 [{user_id[:8]}...]")
        return

    # Quick Reply 攔截：按鈕觸發的訊息直接回覆預設引導語，不呼叫 Claude
    if user_message in QUICK_REPLY_RESPONSES:
        reply_text = random.choice(QUICK_REPLY_RESPONSES[user_message])
        append_to_history(user_id, "user", user_message)
        append_to_history(user_id, "assistant", reply_text)
        logger.info(f"💬 Quick Reply [{user_id[:8]}...]: {user_message}")
        human_reply(event.reply_token, user_id, reply_text, build_quick_reply())
        return

    # 測驗攔截：60 秒易經財運小測（確定性流程，不經 AI）
    quiz_key = user_message.strip().rstrip("！!。.～~ ")
    if quiz_key in QUIZ_TRIGGERS:
        append_to_history(user_id, "user", user_message)
        append_to_history(user_id, "assistant", QUIZ_INTRO)
        logger.info(f"🎲 測驗開始 [{user_id[:8]}...]")
        human_reply(event.reply_token, user_id, QUIZ_INTRO, build_quiz_quick_reply())
        return

    if quiz_key in QUIZ_READINGS:
        reply_text = QUIZ_READINGS[quiz_key]
        append_to_history(user_id, "user", user_message)
        append_to_history(user_id, "assistant", reply_text)
        logger.info(f"🎲 測驗結果 {quiz_key} [{user_id[:8]}...]")
        human_reply(event.reply_token, user_id, reply_text, build_quick_reply())
        return

    # 「占卜」關鍵字攔截：抽牌伏筆收束 → 導 L1 易經占卜（確定性流程，不經 AI）
    if quiz_key in DIVINATION_TRIGGERS:
        append_to_history(user_id, "user", user_message)
        append_to_history(user_id, "assistant", DIVINATION_REPLY)
        logger.info(f"🔮 占卜導引 [{user_id[:8]}...]")
        human_reply(event.reply_token, user_id, DIVINATION_REPLY, build_quick_reply())
        return

    # 心理測驗判讀（遊戲模式）：只有被主動發送測驗、Sheet J 欄=Qx 的用戶才觸發
    # 僅在訊息剛好是單一字母 A/B/C/D 時查 Sheet，不增加一般對話延遲
    game_ans = user_message.strip().rstrip("！!。.、，, ").upper()
    game_ans = game_ans.translate(str.maketrans("ＡＢＣＤ", "ABCD"))
    if game_ans in ("A", "B", "C", "D"):
        game_state = get_game_state(user_id)
        if game_state in GAME_READINGS:
            reply_text = GAME_READINGS[game_state][game_ans]
            append_to_history(user_id, "user", user_message)
            append_to_history(user_id, "assistant", reply_text)
            threading.Thread(target=clear_game_state, args=(user_id,), daemon=True).start()
            logger.info(f"🎮 測驗 {game_state}-{game_ans} [{user_id[:8]}...]")
            human_reply(event.reply_token, user_id, reply_text, build_quick_reply())
            return

    # 易經抽牌判讀：只有被主動發送抽牌、Sheet J 欄=Dx 的用戶回 1/2/3/4 才觸發
    draw_ans = user_message.strip().rstrip("！!。.、，, ")
    draw_ans = draw_ans.translate(str.maketrans("１２３４", "1234"))
    if draw_ans in ("1", "2", "3", "4"):
        draw_state = get_game_state(user_id)
        if draw_state in DRAW_READINGS:
            reply_text = DRAW_READINGS[draw_state][draw_ans]
            append_to_history(user_id, "user", user_message)
            append_to_history(user_id, "assistant", reply_text)
            threading.Thread(target=clear_game_state, args=(user_id,), daemon=True).start()
            logger.info(f"🔮 抽牌 {draw_state}-{draw_ans} [{user_id[:8]}...]")
            human_reply(event.reply_token, user_id, reply_text, build_divination_quick_reply())
            return

    # 1. 取得這位客戶的對話記憶(會自動清理過期內容)
    get_user_history(user_id)

    # 2. 把這次的客戶訊息加入記憶
    append_to_history(user_id, "user", user_message)

    # 3. 組裝給 Claude 的訊息陣列(只送 role + content,不送 time)
    messages_for_claude = [
        {"role": m["role"], "content": m["content"]}
        for m in conversation_memory[user_id]
    ]

    # 4. 呼叫 Claude API 生成回覆（先開輸入中動畫，思考期間就有「打字」感）
    show_loading_animation(user_id)
    _t0 = time.time()
    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages_for_claude
        )
        reply_text = strip_markdown(response.content[0].text)
    except Exception as e:
        reply_text = "抱歉,系統暫時有點忙,請稍等一下或留下您的稱呼,老師會親自回覆您 🙏"
        logger.error(f"Claude API 錯誤: {e}")
    _elapsed = time.time() - _t0

    # 5. AI 的回覆也存進記憶,這樣下一輪對話才會有上下文
    append_to_history(user_id, "assistant", reply_text)

    # 6. 判斷是否為熱客戶
    is_hot = is_hot_lead(user_message, reply_text)
    if is_hot:
        logger.info(f"🔥 熱客戶識別! [{user_id[:8]}...]")

    # 7. 熱客戶：Email 通知 + GSheet 狀態更新（並行，不阻塞）
    if is_hot:
        threading.Thread(
            target=send_email_notification,
            args=(user_id, user_message, reply_text, True),
            daemon=True
        ).start()
        threading.Thread(
            target=update_lead_status,
            args=(user_id, "熱客戶", user_message),
            daemon=True
        ).start()

    logger.info(f"💬 回覆 [{user_id[:8]}...]: {reply_text[:60]}...")

    # 8. 模擬真人打字後回覆（扣掉 Claude 已花的思考時間，避免總延遲過長）
    human_reply(event.reply_token, user_id, reply_text, build_quick_reply(), thinking=_elapsed)


# ============================================================
# Instagram 私訊：回覆函式
# ============================================================
def send_ig_reply(recipient_id, text):
    """透過 Meta Graph API 回覆 IG 私訊。"""
    url = f"https://graph.facebook.com/v21.0/{IG_ACCOUNT_ID}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    params = {"access_token": IG_PAGE_ACCESS_TOKEN}
    resp = requests.post(url, params=params, json=payload, timeout=10)
    if not resp.ok:
        logger.error(f"[IG] 回覆失敗: {resp.status_code} {resp.text}")
    return resp


# ============================================================
# Instagram Webhook 路由
# ============================================================
@app.route("/ig-webhook", methods=['GET', 'POST'])
def ig_webhook():
    """Meta 平台呼叫的 IG 訊息入口。"""
    # --- GET：Meta 驗證 webhook ---
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == IG_VERIFY_TOKEN:
            logger.info("✅ IG Webhook 驗證成功")
            return challenge, 200
        logger.warning(f"❌ IG Webhook 驗證失敗 (token={token})")
        return 'Forbidden', 403

    # --- POST：接收訊息事件 ---
    data = request.get_json(silent=True) or {}
    if data.get('object') != 'instagram':
        return 'OK', 200

    for entry in data.get('entry', []):
        for messaging in entry.get('messaging', []):
            sender_id = messaging.get('sender', {}).get('id')
            message = messaging.get('message', {})

            # 跳過 bot 自己送出的 echo 訊息
            if message.get('is_echo'):
                continue

            user_message = message.get('text', '').strip()
            if not user_message or not sender_id:
                continue

            # IG 用戶 ID 加前綴，與 LINE 用戶分開
            user_id = f"ig_{sender_id}"
            logger.info(f"📩 [IG] 收到 [{user_id[:14]}...]: {user_message}")

            get_user_history(user_id)
            append_to_history(user_id, "user", user_message)

            messages_for_claude = [
                {"role": m["role"], "content": m["content"]}
                for m in conversation_memory[user_id]
            ]

            try:
                response = claude_client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=messages_for_claude
                )
                reply_text = strip_markdown(response.content[0].text)
            except Exception as e:
                reply_text = "抱歉，系統暫時有點忙，請稍等一下或留下您的稱呼，老師會親自回覆您 🙏"
                logger.error(f"[IG] Claude API 錯誤: {e}")

            append_to_history(user_id, "assistant", reply_text)

            is_hot = is_hot_lead(user_message, reply_text)
            if is_hot:
                logger.info(f"🔥 [IG] 熱客戶識別! [{user_id[:14]}...]")
                threading.Thread(
                    target=send_email_notification,
                    args=(user_id, user_message, reply_text, True),
                    daemon=True
                ).start()

            send_ig_reply(sender_id, reply_text)
            logger.info(f"💬 [IG] 回覆 [{user_id[:14]}...]: {reply_text[:60]}...")

    return 'OK', 200


# ============================================================
# 啟動入口
# ============================================================
# 模組載入即啟動背景排程（gunicorn 不會執行 __main__，故放這裡）
start_background_worker()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"🌟 五木老師客服啟動於 port {port}")
    app.run(host='0.0.0.0', port=port)
