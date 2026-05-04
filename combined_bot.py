"""
Combined Bot for Railway Deployment
รวม User Bot + Technician Bot ในแอปเดียว
"""

import os
import re
import json
import requests
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ============ CONFIGURATION ============

# User Bot
USER_LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
USER_LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GAS_URL = os.getenv('GOOGLE_APPS_SCRIPT_URL')

# Technician Bot
TECH_LINE_CHANNEL_ACCESS_TOKEN = os.getenv('TECH_LINE_CHANNEL_ACCESS_TOKEN')
TECH_LINE_CHANNEL_SECRET = os.getenv('TECH_LINE_CHANNEL_SECRET')
TECH_GROUP_ID = os.getenv('TECH_GROUP_ID')

# Initialize services
user_bot_api = LineBotApi(USER_LINE_CHANNEL_ACCESS_TOKEN)
user_handler = WebhookHandler(USER_LINE_CHANNEL_SECRET)

tech_bot_api = LineBotApi(TECH_LINE_CHANNEL_ACCESS_TOKEN)
tech_handler = WebhookHandler(TECH_LINE_CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)

# Constants
CATEGORY_OPTIONS = [
    "วัตถุดิบไม่พอ",
    "ขอเลื่อนแผน",
    "เครื่องจักรเสีย",
    "แทรกออเดอร์ด่วน",
    "สอบถามคิวงาน",
    "อื่นๆ"
]
URGENCY_OPTIONS = ["สูง", "ปานกลาง", "ต่ำ"]
DEFAULT_STATUS = "รอตรวจสอบ"

# AI Prompt
SYSTEM_PROMPT = """คุณคือผู้ช่วยจัดการงาน Planning หน้าที่ของคุณคือ:
1. แก้คำผิดและปรับปรุงข้อความให้ถูกต้อง
2. สกัดข้อมูลจากข้อความแจ้งปัญหา

ให้ตอบกลับเป็น JSON format เท่านั้น ห้ามมีคำอธิบายอื่น:
{
  "corrected_message": "ข้อความที่แก้คำผิดแล้ว",
  "category": "เลือกจาก: วัตถุดิบไม่พอ, ขอเลื่อนแผน, เครื่องจักรเสีย, แทรกออเดอร์ด่วน, สอบถามคิวงาน, อื่นๆ",
  "urgency": "เลือกจาก: สูง, ปานกลาง, ต่ำ",
  "extracted_keywords": "สกัดรหัสสินค้า ชื่อบริษัท หรือวันที่",
  "ai_suggested_action": "แนะนำวิธีแก้ไขสั้นๆ"
}"""

# Create FastAPI app
app = FastAPI(title="LINE Planner Bot - Combined")

# ============ HELPER FUNCTIONS ============

def generate_ticket_id() -> str:
    import random
    import string
    date_str = datetime.now(timezone.utc).strftime('%Y%m%d')
    random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"JOB-{date_str}-{random_suffix}"

def parse_ai_response(response_text: str) -> Dict[str, str]:
    try:
        cleaned = response_text.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.split('```')[1] if '```json' in cleaned else cleaned.replace('```', '')
            cleaned = cleaned.replace('json', '', 1).strip()
        
        data = json.loads(cleaned)
        
        required_fields = ['corrected_message', 'category', 'urgency', 'extracted_keywords', 'ai_suggested_action']
        for field in required_fields:
            if field not in data or not data[field]:
                data[field] = "ไม่ระบุ"
        
        if data['category'] not in CATEGORY_OPTIONS:
            data['category'] = "อื่นๆ"
        
        if data['urgency'] not in URGENCY_OPTIONS:
            data['urgency'] = "ปานกลาง"
            
        return data
    except:
        return {
            "corrected_message": "",
            "category": "อื่นๆ",
            "urgency": "ปานกลาง",
            "extracted_keywords": "ไม่สามารถสกัดข้อมูลได้",
            "ai_suggested_action": "ตรวจสอบข้อความต้นฉบับ"
        }

def process_with_ai(user_message: str) -> Dict[str, str]:
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(f"{SYSTEM_PROMPT}\n\nข้อความ: {user_message}")
        return parse_ai_response(response.text)
    except Exception as e:
        logger.error(f"AI error: {e}")
        return {
            "corrected_message": user_message,
            "category": "อื่นๆ",
            "urgency": "ปานกลาง",
            "extracted_keywords": "AI ประมวลผลไม่สำเร็จ",
            "ai_suggested_action": "ตรวจสอบด้วยตนเอง"
        }

def save_to_google_sheets(payload: Dict[str, Any]) -> bool:
    try:
        response = requests.post(GAS_URL, json=payload, headers={'Content-Type': 'application/json'}, timeout=15)
        return response.json().get('success', False)
    except Exception as e:
        logger.error(f"Error saving to sheets: {e}")
        return False

def notify_tech_group(ticket_data: Dict[str, Any]):
    """แจ้งเตือนไปยัง LINE Group ของช่าง"""
    try:
        message = (
            f"🚨 มีงานใหม่เข้ามา!\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🎫 {ticket_data['ticket_id']}\n"
            f"📂 {ticket_data['category']}\n"
            f"⚡ {ticket_data['urgency']}\n"
            f"📝 {ticket_data['corrected_message'][:50]}...\n"
            f"━━━━━━━━━━━━━━━\n"
            f"พิมพ์: take {ticket_data['ticket_id']}"
        )
        tech_bot_api.push_message(TECH_GROUP_ID, TextSendMessage(text=message))
        logger.info(f"Notified tech group about {ticket_data['ticket_id']}")
    except Exception as e:
        logger.error(f"Failed to notify tech group: {e}")

def get_ticket_status(ticket_id: str) -> dict:
    try:
        response = requests.get(f"{GAS_URL}?action=get_ticket&ticket_id={ticket_id}", timeout=10)
        return response.json()
    except Exception as e:
        logger.error(f"Error getting ticket: {e}")
        return None

def get_user_tickets(user_id: str) -> list:
    try:
        response = requests.get(f"{GAS_URL}?action=get_user_tickets&user_id={user_id}", timeout=10)
        return response.json().get('tickets', [])
    except Exception as e:
        logger.error(f"Error getting user tickets: {e}")
        return []

def get_pending_tickets():
    try:
        response = requests.get(f"{GAS_URL}?action=get_pending", timeout=10)
        return response.json().get('tickets', [])
    except Exception as e:
        logger.error(f"Error fetching tickets: {e}")
        return []

def update_ticket_status(ticket_id: str, status: str, technician_id: str, notes: str = ""):
    try:
        payload = {
            "action": "update_status",
            "ticket_id": ticket_id,
            "status": status,
            "technician_id": technician_id,
            "notes": notes,
            "updated_at": datetime.now().isoformat()
        }
        response = requests.post(GAS_URL, json=payload, timeout=10)
        return response.json().get('success', False)
    except Exception as e:
        logger.error(f"Error updating ticket: {e}")
        return False

# ============ USER BOT HANDLERS ============

@user_handler.add(MessageEvent, message=TextMessage)
def handle_user_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    
    logger.info(f"[User Bot] {user_id}: {user_message[:50]}...")
    
    # คำสั่ง: status [ticket_id]
    if user_message.lower().startswith(('status ', 'สถานะ ')):
        ticket_id = user_message.split(' ', 1)[1].strip()
        ticket = get_ticket_status(ticket_id)
        
        if ticket and 'error' not in ticket:
            status_emoji = {'รอตรวจสอบ': '⏳', 'กำลังดำเนินการ': '🔧', 'เสร็จแล้ว': '✅', 'ยกเลิก': '❌'}.get(ticket['status'], '📋')
            reply_text = f"🎫 {ticket_id}\n━━━━━━━━━━━━━━━\n{status_emoji} สถานะ: {ticket['status']}\n📂 {ticket['category']}\n⚡ {ticket['urgency']}\n"
            if ticket.get('assigned_to'):
                reply_text += f"👤 ผู้รับผิดชอบ: {ticket['assigned_to'][:15]}...\n"
            reply_text += "━━━━━━━━━━━━━━━"
        else:
            reply_text = f"❌ ไม่พบงาน {ticket_id}"
        
        user_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    
    # คำสั่ง: mytickets
    if user_message.lower() in ['mytickets', 'myticket', 'งานของฉัน']:
        tickets = get_user_tickets(user_id)
        if not tickets:
            reply_text = "📋 คุณยังไม่มีงานในระบบ"
        else:
            reply_text = "📋 งานของคุณ:\n━━━━━━━━━━━━━━━\n"
            for ticket in tickets[:10]:
                status_emoji = {'รอตรวจสอบ': '⏳', 'กำลังดำเนินการ': '🔧', 'เสร็จแล้ว': '✅', 'ยกเลิก': '❌'}.get(ticket['status'], '📋')
                reply_text += f"🎫 {ticket['ticket_id']}\n{status_emoji} {ticket['status']} | {ticket['category']}\n━━━━━━━━━━━━━━━\n"
            reply_text += "\nพิมพ์ 'status [รหัส]' เพื่อดูรายละเอียด"
        
        user_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    
    # คำสั่ง: help
    if user_message.lower() in ['help', 'ช่วยเหลือ']:
        reply_text = """📋 วิธีใช้:
━━━━━━━━━━━━━━━
📝 แจ้งปัญหา: พิมพ์ข้อความปกติ
📊 เช็คสถานะ: status [รหัสงาน]
📋 ดูงานทั้งหมด: mytickets
━━━━━━━━━━━━━━━"""
        user_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        return
    
    # แจ้งปัญหาปกติ
    try:
        ai_data = process_with_ai(user_message)
        ticket_id = generate_ticket_id()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        
        payload = {
            "ticket_id": ticket_id,
            "timestamp": timestamp,
            "reporter_line_id": user_id,
            "original_message": user_message,
            "corrected_message": ai_data['corrected_message'],
            "category": ai_data['category'],
            "urgency": ai_data['urgency'],
            "extracted_keywords": ai_data['extracted_keywords'],
            "ai_suggested_action": ai_data['ai_suggested_action'],
            "status": DEFAULT_STATUS
        }
        
        save_success = save_to_google_sheets(payload)
        
        if save_success:
            reply_text = (
                f"✅ แจ้งปัญหาเรียบร้อย\n"
                f"━━━━━━━━━━━━━━━\n"
                f"📌 {ticket_id}\n"
                f"📂 {ai_data['category']}\n"
                f"⚡ {ai_data['urgency']}\n"
                f"💡 {ai_data['ai_suggested_action']}\n"
                f"━━━━━━━━━━━━━━━"
            )
            # แจ้งเตือนช่าง
            notify_tech_group(payload)
        else:
            reply_text = f"⚠️ บันทึกไม่สำเร็จ\nรหัสอ้างอิง: {ticket_id}"
        
        user_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        logger.error(f"Error: {e}")
        user_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ ระบบขัดข้อง กรุณาลองใหม่"))


# ============ TECHNICIAN BOT HANDLERS ============

@tech_handler.add(MessageEvent, message=TextMessage)
def handle_tech_message(event):
    message = event.message.text.strip()
    user_id = event.source.user_id
    
    logger.info(f"[Tech Bot] {user_id}: {message[:50]}...")
    
    # Debug: หา Group ID
    source_type = event.source.type
    if source_type == 'group' and message.lower() in ['id', 'ไอดี']:
        group_id = event.source.group_id
        tech_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🆔 Group ID:\n{group_id}"))
        return
    
    # คำสั่ง: list
    if message.lower() == 'list' or message == 'รายการ':
        tickets = get_pending_tickets()
        if not tickets:
            reply = "📋 ไม่มีงานที่รอดำเนินการ"
        else:
            reply = "📋 งานที่รอดำเนินการ:\n" + "━"*20 + "\n"
            for ticket in tickets[:5]:
                reply += f"🎫 {ticket['ticket_id']}\n📂 {ticket['category']}\n⚡ {ticket['urgency']}\n📝 {ticket['corrected_message'][:30]}...\n━"*20 + "\n"
            reply += "\nพิมพ์: take [รหัสงาน]"
        
        tech_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    
    # คำสั่ง: take
    if message.lower().startswith('take '):
        ticket_id = message[5:].strip()
        success = update_ticket_status(ticket_id, "กำลังดำเนินการ", user_id, f"รับงานโดย {user_id}")
        reply = f"✅ รับงาน {ticket_id} เรียบร้อย" if success else f"❌ ไม่พบงาน {ticket_id}"
        tech_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    
    # คำสั่ง: done
    if message.lower().startswith('done '):
        parts = message[5:].strip().split(' ', 1)
        ticket_id = parts[0]
        notes = parts[1] if len(parts) > 1 else "ทำเสร็จ"
        success = update_ticket_status(ticket_id, "เสร็จแล้ว", user_id, notes)
        reply = f"✅ อัปเดต {ticket_id} เป็น 'เสร็จแล้ว'\n📝 {notes}" if success else f"❌ อัปเดตไม่สำเร็จ"
        tech_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    
    # Default help
    reply = """📋 คำสั่งช่าง:
━━━━━━━━━━━━━━━
• list - ดูงานที่รออยู่
• take [รหัส] - รับงาน
• done [รหัส] [หมายเหตุ] - ทำเสร็จ
• status [รหัส] - เช็คสถานะ

ตัวอย่าง:
take JOB-20260503-ABCD
done JOB-20260503-ABCD ซ่อมเสร็จ"""
    tech_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ============ FASTAPI ROUTES ============

@app.get("/")
async def root():
    return {"status": "LINE Planner Bot is running", "version": "2.0"}

@app.post("/webhook")
async def user_webhook(request: Request):
    """Webhook สำหรับ User Bot"""
    signature = request.headers.get('X-Line-Signature')
    body = await request.body()
    
    try:
        user_handler.handle(body.decode(), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"User webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")
    
    return JSONResponse(content={"status": "ok"})

@app.post("/webhook/tech")
async def tech_webhook(request: Request):
    """Webhook สำหรับ Technician Bot"""
    signature = request.headers.get('X-Line-Signature')
    body = await request.body()
    
    try:
        tech_handler.handle(body.decode(), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    except Exception as e:
        logger.error(f"Tech webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal error")
    
    return JSONResponse(content={"status": "ok"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
