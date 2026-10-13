"""Read the new bot's chat IDs without putting its token in CLI args or URLs shown to users."""
import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("Mở chat với bot của bạn, nhấn Start rồi gửi một tin nhắn trước khi tiếp tục.")
    token = getpass.getpass("Bot token (ẩn khi nhập): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise SystemExit("Token chưa đúng định dạng. Lấy token từ BotFather; không gửi token vào chat.")
    payload = urllib.parse.urlencode({"timeout": 0, "limit": 100,
                                     "allowed_updates": json.dumps(["message"])}).encode()
    request = urllib.request.Request("https://api.telegram.org/bot"+token+"/getUpdates", data=payload)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Telegram HTTP {exc.code}. Kiểm tra token; lỗi 409 có thể do webhook/bot khác đang đọc updates.") from None
    except (OSError, ValueError):
        raise SystemExit("Không đọc được phản hồi Telegram. Kiểm tra Internet rồi thử lại.") from None
    if not data.get("ok"):
        raise SystemExit("Telegram từ chối yêu cầu; kiểm tra token hoặc bot đang được sử dụng nơi khác.")
    chats = {}
    for update in data.get("result", []):
        chat = update.get("message", {}).get("chat")
        if chat:
            chats[chat["id"]] = chat
    if not chats:
        print("Chưa có chat. Gửi một tin nhắn mới trực tiếp cho bot, sau đó chạy lại.")
        return
    print("Các chat có tin nhắn gần đây (chỉ đọc, chưa gửi cảnh báo):")
    for chat_id, chat in chats.items():
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or "Chat"
        print(f"  TELEGRAM_CHAT_ID={chat_id} | {chat.get('type')} | {name}")
    print("Chọn ID của chat bạn muốn nhận cảnh báo và điền vào Environment trên Render.")


if __name__ == "__main__":
    main()
