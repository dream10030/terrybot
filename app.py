# ============================================================
# TerryBot - LINE AI 助理系統
# 功能：
#   1. 記錄群組訊息
#   2. #TERRYBOT 指令觸發 AI 回覆
#   3. 每日營運摘要自動整理（每天晚上 9 點）
# ============================================================

import os
import json
import sqlite3
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, request, abort

# LINE SDK
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    JoinEvent,
)
from linebot.v3.exceptions import InvalidSignatureError

# Google Gemini AI
import google.generativeai as genai

# ============================================================
# 設定區（從環境變數讀取，保護金鑰安全）
# ============================================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Terry 的 LINE User ID（Bot 會私訊摘要給你，部署後自動取得）
TERRY_USER_ID = os.environ.get("TERRY_USER_ID", "")

# 每日摘要發送時間（24 小時制，預設晚上 9 點）
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "21"))

# ============================================================
# 初始化
# ============================================================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TerryBot")

# LINE API 初始化
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Gemini AI 初始化
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ============================================================
# 資料庫設定（SQLite，用來儲存群組訊息）
# ============================================================
DB_PATH = os.environ.get("DB_PATH", "terrybot.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            group_name TEXT DEFAULT '',
            user_id TEXT NOT NULL,
            user_name TEXT DEFAULT '',
            message TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            group_name TEXT DEFAULT '',
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


init_db()


def save_message(group_id, user_id, user_name, message, group_name=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (group_id, group_name, user_id, user_name, message) VALUES (?, ?, ?, ?, ?)",
        (group_id, group_name, user_id, user_name, message),
    )
    conn.commit()
    conn.close()


def save_group(group_id, group_name=""):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO groups (group_id, group_name) VALUES (?, ?)",
        (group_id, group_name),
    )
    conn.commit()
    conn.close()


def get_today_messages(group_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    if group_id:
        c.execute(
            "SELECT group_name, user_name, message, timestamp FROM messages WHERE group_id = ? AND date(timestamp) = ? ORDER BY timestamp",
            (group_id, today),
        )
    else:
        c.execute(
            "SELECT group_name, user_name, message, timestamp FROM messages WHERE date(timestamp) = ? ORDER BY group_id, timestamp",
            (today,),
        )
    rows = c.fetchall()
    conn.close()
    return rows


def get_all_group_ids():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT group_id, group_name FROM groups")
    rows = c.fetchall()
    conn.close()
    return rows


def ask_ai(prompt, system_instruction=""):
    try:
        full_prompt = ""
        if system_instruction:
            full_prompt = f"[系統指令] {system_instruction}\n\n"
        full_prompt += prompt
        response = gemini_model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        logger.error(f"AI 回覆錯誤: {e}")
        return f"抱歉，AI 暫時無法回應，請稍後再試。"


def generate_daily_summary(messages):
    if not messages:
        return "今天各群組沒有新的訊息記錄。"
    formatted = ""
    current_group = ""
    for group_name, user_name, message, timestamp in messages:
        gname = group_name or "未知群組"
        if gname != current_group:
            formatted += f"\n【{gname}】\n"
            current_group = gname
        time_str = timestamp.split(" ")[1][:5] if " " in timestamp else ""
        formatted += f"  {time_str} {user_name}: {message}\n"
    prompt = f"""你是大有運動公司的 AI 助理 TerryBot。
請根據以下今天所有 LINE 群組的訊息記錄，幫 CEO Terry 整理出：

1. **今日營運重點摘要**（依群組分類，每個群組列出 3-5 個重點）
2. **Terry 需要親自處理/回覆的項目**（標註緊急程度）
3. **明日待辦建議**

請用繁體中文、簡潔有力的方式呈現。

---
今日群組訊息記錄：
{formatted}
"""
    return ask_ai(prompt)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info("收到 Webhook 請求")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("簽章驗證失敗")
        abort(400)
    return "OK"


@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id
    try:
        with ApiClient(configuration) as api_client:
            line_api = MessagingApi(api_client)
            group_summary = line_api.get_group_summary(group_id)
            group_name = group_summary.group_name
    except Exception:
        group_name = "未知群組"
    save_group(group_id, group_name)
    logger.info(f"已加入群組: {group_name} ({group_id})")
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text="哈囉！我是 TerryBot\n\n我會默默記錄群組訊息，每天幫 Terry 整理營運摘要。\n\n需要 AI 幫忙時，請輸入：\n#TERRYBOT 你的問題"
                    )
                ],
            )
        )


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    message_text = event.message.text.strip()
    user_id = event.source.user_id
    user_name = "未知使用者"
    try:
        with ApiClient(configuration) as api_client:
            line_api = MessagingApi(api_client)
            if hasattr(event.source, "group_id") and event.source.group_id:
                profile = line_api.get_group_member_profile(event.source.group_id, user_id)
            else:
                profile = line_api.get_profile(user_id)
            user_name = profile.display_name
    except Exception as e:
        logger.warning(f"無法取得使用者名稱: {e}")

    if hasattr(event.source, "group_id") and event.source.group_id:
        group_id = event.source.group_id
        group_name = ""
        try:
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                group_summary = line_api.get_group_summary(group_id)
                group_name = group_summary.group_name
        except Exception:
            pass
        save_message(group_id, user_id, user_name, message_text, group_name)
        save_group(group_id, group_name)
        logger.info(f"[{group_name}] {user_name}: {message_text}")

        if message_text.upper().startswith("#TERRYBOT"):
            question = message_text[len("#TERRYBOT"):].strip()
            if not question:
                reply_text = "請在 #TERRYBOT 後面輸入你的問題喔！"
            else:
                system_msg = "你是大有運動公司的 AI 助理 TerryBot，請用繁體中文回答，語氣專業但親切，回答盡量簡潔實用。如果問題涉及公司內部資料你無法確定，請誠實說明。"
                reply_text = ask_ai(question, system_msg)
                if len(reply_text) > 4500:
                    reply_text = reply_text[:4500] + "\n\n...（回覆過長，已截斷）"
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                line_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)],
                    )
                )
    else:
        if message_text == "我是Terry":
            os.environ["TERRY_USER_ID"] = user_id
            global TERRY_USER_ID
            TERRY_USER_ID = user_id
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                line_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"已記住你的身份！每天晚上 {DAILY_SUMMARY_HOUR} 點我會私訊你今日營運摘要。\n\n你也可以隨時私訊我「今日摘要」來手動取得。")],
                    )
                )
        elif message_text == "今日摘要":
            messages = get_today_messages()
            summary = generate_daily_summary(messages)
            if len(summary) > 4500:
                summary = summary[:4500] + "\n\n...（內容過長，已截斷）"
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                line_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"今日營運摘要\n\n{summary}")],
                    )
                )
        else:
            system_msg = "你是大有運動公司的 AI 助理 TerryBot，請用繁體中文回答，語氣專業但親切。"
            reply_text = ask_ai(message_text, system_msg)
            if len(reply_text) > 4500:
                reply_text = reply_text[:4500] + "\n\n...（回覆過長，已截斷）"
            with ApiClient(configuration) as api_client:
                line_api = MessagingApi(api_client)
                line_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=reply_text)],
                    )
                )


def daily_summary_scheduler():
    while True:
        now = datetime.now()
        target = now.replace(hour=DAILY_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"下次摘要時間: {target.strftime('%Y-%m-%d %H:%M')}")
        time.sleep(wait_seconds)
        if TERRY_USER_ID:
            try:
                messages = get_today_messages()
                summary = generate_daily_summary(messages)
                if len(summary) > 4500:
                    summary = summary[:4500] + "\n\n...（內容過長，已截斷）"
                with ApiClient(configuration) as api_client:
                    line_api = MessagingApi(api_client)
                    line_api.push_message(
                        PushMessageRequest(
                            to=TERRY_USER_ID,
                            messages=[TextMessage(text=f"TerryBot 每日營運摘要\n{datetime.now().strftime('%Y/%m/%d')}\n\n{summary}")],
                        )
                    )
                logger.info("每日摘要已發送！")
            except Exception as e:
                logger.error(f"發送每日摘要失敗: {e}")
        else:
            logger.warning("尚未設定 Terry 的 User ID，請私訊 Bot「我是Terry」來設定。")


@app.route("/", methods=["GET"])
def health_check():
    return "TerryBot is running!"


if __name__ == "__main__":
    scheduler_thread = threading.Thread(target=daily_summary_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("TerryBot 啟動完成！每日摘要排程已開始。")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
