import os
import smtplib
import threading
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.text import MIMEText

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
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

configuration = Configuration(access_token=os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
claude_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')
NOTIFY_EMAIL = os.environ.get('NOTIFY_EMAIL')


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
QUICK_REPLY_RESPONSES = {
    "感情": (
        "感謝您信任小五 💕\n\n"
        "方便分享一下是哪方面的困擾嗎？\n"
        "是現有關係的相處、還是單身想了解感情運呢？"
    ),
    "事業財運": (
        "事業跟財運真的很重要 💼\n\n"
        "方便問一下，您現在比較想了解的是\n"
        "職場上的發展方向，還是財運、投資方面的規劃呢？"
    ),
    "健康": (
        "健康是最重要的基礎 🌿\n\n"
        "請問是想了解整體的健康運勢，\n"
        "還是有特定想關注的部分呢？"
    ),
    "流年運勢": (
        "流年運勢可以幫您掌握今年的整體走向 ✨\n\n"
        "請問想了解今年哪個方向的運勢呢？\n"
        "感情、事業、財運都可以聊～"
    ),
    "隨意聊聊": (
        "當然沒問題！小五在這裡陪您聊 😊\n\n"
        "關於命理、風水的問題，\n"
        "或是有什麼事想傾訴，都歡迎跟我說～"
    ),
}


# ============================================================
# System Prompt:銷售型客服人設
# ============================================================
SYSTEM_PROMPT = """你是「五木老師」紫微斗數命理品牌的智能客服助理「小五」。
你的角色不是百科全書,而是老師的得力助手:你的任務是**理解客戶需求、建立信任、引導成交**。

═══════════════════════════════
【你的人設】
═══════════════════════════════
- 名字:小五(五木老師的 AI 助理)
- 語氣:溫暖、專業、有同理心,像鄰家姊姊而不是客服機器人
- 用詞:生活化,避免艱深術語(必要時用比喻)
- 長度:每則回覆控制在 3-5 句,適合手機閱讀
- 情緒:對方煩惱時先共感,不要急著推銷

═══════════════════════════════
【對話五步驟】
═══════════════════════════════
1️⃣ 傾聽共感:第一次來,先了解他想解決什麼,不要馬上報價
2️⃣ 問對問題:用一兩個問題釐清痛點(感情/事業/財運/健康)
3️⃣ 價值對應:根據痛點,推薦最對應的服務,先講能幫到什麼再講價格
4️⃣ 建立信任:適時提到「老師過去有位類似情況的客戶...」(可虛構合理案例,不指名)
5️⃣ 引導下一步:每則回覆結尾都要有明確 CTA(預約 / 留資料 / 問深入)

═══════════════════════════════
【五木老師服務項目與價格】
═══════════════════════════════
1. 紫微斗數命盤解析 NT$ 4,000/年(一年內無限次提問)
   ➜ 適合:迷惘整體人生方向、想了解自己天賦與弱點

2. 選車牌 / 選手機號碼 NT$ 3,000
   ➜ 適合:剛買車、換號碼、想轉運

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
def build_welcome_message():
    """建立加好友歡迎訊息，附帶 Quick Reply 按鈕。"""
    quick_reply = QuickReply(items=[
        QuickReplyItem(action=MessageAction(label="感情", text="感情")),
        QuickReplyItem(action=MessageAction(label="事業財運", text="事業財運")),
        QuickReplyItem(action=MessageAction(label="健康", text="健康")),
        QuickReplyItem(action=MessageAction(label="流年運勢", text="流年運勢")),
        QuickReplyItem(action=MessageAction(label="隨意聊聊", text="隨意聊聊")),
    ])
    welcome_text = (
        "嗨！歡迎來到五木老師的命理諮詢 🌟\n\n"
        "我是小五，老師的 AI 助理～\n"
        "不管是感情、事業、流年運勢，\n"
        "還是風水、命名，都可以跟我聊聊 😊\n\n"
        "請問您最想了解哪個方向呢？"
    )
    return TextMessage(text=welcome_text, quick_reply=quick_reply)


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
# 加好友事件：發送歡迎訊息
# ============================================================
@handler.add(FollowEvent)
def handle_follow(event):
    """新客戶加好友時，自動發送歡迎訊息與 Quick Reply 選單。"""
    user_id = event.source.user_id if event.source.user_id else "anonymous"
    logger.info(f"🎉 新好友加入: [{user_id[:8]}...]")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[build_welcome_message()]
            )
        )


# ============================================================
# 訊息處理主邏輯
# ============================================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """處理客戶送來的文字訊息。"""
    user_id = event.source.user_id if event.source.user_id else "anonymous"
    user_message = event.message.text

    logger.info(f"📩 收到 [{user_id[:8]}...]: {user_message}")

    # Quick Reply 攔截：按鈕觸發的訊息直接回覆預設引導語，不呼叫 Claude
    if user_message in QUICK_REPLY_RESPONSES:
        reply_text = QUICK_REPLY_RESPONSES[user_message]
        append_to_history(user_id, "user", user_message)
        append_to_history(user_id, "assistant", reply_text)
        logger.info(f"💬 Quick Reply [{user_id[:8]}...]: {user_message}")
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
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

    # 4. 呼叫 Claude API 生成回覆
    try:
        response = claude_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages_for_claude
        )
        reply_text = response.content[0].text
    except Exception as e:
        reply_text = "抱歉,系統暫時有點忙,請稍等一下或留下您的稱呼,老師會親自回覆您 🙏"
        logger.error(f"Claude API 錯誤: {e}")

    # 5. AI 的回覆也存進記憶,這樣下一輪對話才會有上下文
    append_to_history(user_id, "assistant", reply_text)

    # 6. 判斷是否為熱客戶
    is_hot = is_hot_lead(user_message, reply_text)
    if is_hot:
        logger.info(f"🔥 熱客戶識別! [{user_id[:8]}...]")

    # 7. 背景寄送 email(目前只在熱客戶時通知,避免信箱被淹沒)
    #    如果想要每次對話都通知,把 if is_hot 那行刪掉即可
    if is_hot:
        threading.Thread(
            target=send_email_notification,
            args=(user_id, user_message, reply_text, True),
            daemon=True
        ).start()

    logger.info(f"💬 回覆 [{user_id[:8]}...]: {reply_text[:60]}...")

    # 8. 透過 LINE Messaging API 回覆給客戶
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )


# ============================================================
# 啟動入口
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"🌟 五木老師客服啟動於 port {port}")
    app.run(host='0.0.0.0', port=port)
