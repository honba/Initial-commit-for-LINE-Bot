import json
import os
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import google.generativeai as genai

app = FastAPI()

# ---------------------------------------------------------------------------
# 1. 環境變數與基本設定
# ---------------------------------------------------------------------------
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
genai.configure(api_key=GEMINI_API_KEY)

PREF_FILE = "user_language_pref.json"

# ---------------------------------------------------------------------------
# 2. JSON 檔案讀寫機制（記錄對話偏好語言與開關狀態）
# 結構：{ "source_id": { "lang": "日文", "active": True } }
# ---------------------------------------------------------------------------
def load_preferences() -> dict:
    if os.path.exists(PREF_FILE):
        try:
            with open(PREF_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Warning] Failed to load {PREF_FILE}: {e}")
            return {}
    return {}

def save_preferences(data: dict):
    try:
        with open(PREF_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Error] Failed to save {PREF_FILE}: {e}")

# 初始化偏好設定字典
user_language_pref = load_preferences()

# ---------------------------------------------------------------------------
# 3. Gemini 模型初始化
# ---------------------------------------------------------------------------
# 語言偵測模型：專門判斷輸入文字的語系
detect_model = genai.GenerativeModel(
    'gemini-3.5-flash-lite',
    system_instruction=(
        "你是一個語系辨識專家。請判斷輸入文字主要使用的語言。"
        "如果是中文（繁體或簡體），請僅回應 '中文'；"
        "如果是其他語言，請僅回應其常用語言名稱（例如：英文、日文、韓文、法文、德文、西班牙文等）。"
        "不要包含任何標點符號或額外說明。"
    )
)

# 精準翻譯模型：專門執行高質量的翻譯
translate_model = genai.GenerativeModel(
    'gemini-3.5-flash-lite',
    system_instruction=(
        "你是一個精準的專業翻譯員。"
        "只需輸出翻譯結果，絕對不要包含任何額外問候、解釋、引號或標點修飾。"
        "請保持原始語意、專有名詞與排版格式。"
    )
)
#model = genai.GenerativeModel('gemini-3-flash-live', system_instruction=system_instruction)

# ---------------------------------------------------------------------------
# 4. FastAPI 路由與 LINE Webhook 處理
# ---------------------------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "ok", "message": "LINE Translator Bot is running."}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    source_id = getattr(event.source, 'group_id', None) or event.source.user_id

    # 取得當前對話的設定（若無設定則預設啟用，偏好語言預設英文）
    session = user_language_pref.get(source_id, {"lang": "英文", "active": True})

    # 輔助函式：發送訊息
    def reply(text):
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=text)]
                )
            )

    # -----------------------------------------------------------------------
    # 指令處理：開關控制
    # -----------------------------------------------------------------------
    if user_text in ["關閉翻譯", "停止翻譯", "off", "OFF"]:
        session["active"] = False
        user_language_pref[source_id] = session
        save_preferences(user_language_pref)
        reply("🔴 翻譯功能已關閉。需要時請輸入「開啟翻譯」。")
        return

    if user_text in ["開啟翻譯", "啟動翻譯", "on", "ON"]:
        session["active"] = True
        user_language_pref[source_id] = session
        save_preferences(user_language_pref)
        reply(f"🟢 翻譯功能已開啟。（目前目標外語：{session.get('lang', '英文')}）")
        return

    # 若開關為關閉狀態，直接跳出不處理（不回應任何訊息）
    if not session.get("active", True):
        return

    # -----------------------------------------------------------------------
    # 翻譯處理邏輯
    # -----------------------------------------------------------------------
    try:
        detect_res = detect_model.generate_content(user_text)
        detected_lang = detect_res.text.strip()
        
        updated = False
        if detected_lang != "中文":
            # 收到外文：更新偏好外語，並翻譯成中文
            if session.get("lang") != detected_lang:
                session["lang"] = detected_lang
                updated = True
            prompt = f"請將以下文字翻譯成【台灣常用繁體中文】：\n{user_text}"
        else:
            # 收到中文：使用記錄的偏好外語進行翻譯
            target_lang = session.get("lang", "英文")
            prompt = f"請將以下文字翻譯成【{target_lang}】：\n{user_text}"

        if updated:
            user_language_pref[source_id] = session
            save_preferences(user_language_pref)

        response = translate_model.generate_content(prompt)
        translated_text = response.text.strip()

        reply(translated_text)

    except Exception as e:
        print(f"[Error Details] Failed to handle message: {str(e)}")

# ---------------------------------------------------------------------------
# 5. 啟動 Uvicorn 伺服器
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)