"""Bounded background alert delivery. No external calls on the inference path."""
import copy
import io
import json
import math
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageOps

VIOLATIONS = {"without_mask", "mask_weared_incorrect"}
VN_NAMES = {"with_mask": "Đeo đúng", "without_mask": "Không đeo",
            "mask_weared_incorrect": "Đeo sai"}
COLORS = {"with_mask": "#047857", "without_mask": "#c62828", "mask_weared_incorrect": "#b45309"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AlertConfig:
    enabled: bool = False
    error: str = ""
    supabase_url: str = ""
    supabase_key: str = field(default="", repr=False)
    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    bucket: str = "mask-alerts"
    cooldown: float = 30
    window_seconds: float = 8
    timeout: float = 10

    @classmethod
    def from_env(cls):
        config = cls()
        if os.getenv("ALERTS_ENABLED", "0").lower() not in {"1", "true", "yes"}:
            return config
        required = ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "API_KEY"]
        missing = [key for key in required if not os.getenv(key, "").strip()]
        if missing:
            config.error = "Missing: " + ", ".join(missing)
            return config
        config.supabase_url = os.environ["SUPABASE_URL"].strip().rstrip("/")
        config.supabase_key = os.environ["SUPABASE_SECRET_KEY"].strip()
        config.bot_token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        config.chat_id = os.environ["TELEGRAM_CHAT_ID"].strip()
        config.bucket = os.getenv("SUPABASE_ALERT_BUCKET", "mask-alerts")
        try:
            parsed = urllib.parse.urlsplit(config.supabase_url)
            port = parsed.port
        except ValueError:
            config.error = "Invalid SUPABASE_URL"
            return config
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or port not in (None, 443)):
            config.error = "SUPABASE_URL must be an HTTPS project origin without a path"
        elif not config.supabase_key.startswith(("sb_secret_", "eyJ")):
            config.error = "SUPABASE_SECRET_KEY must be a backend secret or legacy service_role key"
        elif len(os.environ["API_KEY"]) < 16:
            config.error = "API_KEY must have at least 16 characters when alerts are enabled"
        elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", config.bot_token):
            config.error = "Invalid TELEGRAM_BOT_TOKEN format"
        elif not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", config.chat_id):
            config.error = "Invalid TELEGRAM_CHAT_ID format"
        elif not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", config.bucket):
            config.error = "Invalid SUPABASE_ALERT_BUCKET"
        try:
            config.cooldown = float(os.getenv("ALERT_COOLDOWN_SECONDS", "30"))
            if not math.isfinite(config.cooldown) or not 10 <= config.cooldown <= 3600:
                raise ValueError()
        except ValueError:
            config.error = "ALERT_COOLDOWN_SECONDS must be between 10 and 3600"
        config.enabled = not config.error
        return config


class DeliveryError(Exception):
    def __init__(self, code, ambiguous=False):
        super().__init__(code)
        self.code, self.ambiguous = code, ambiguous


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Prevent credential-bearing requests being redirected to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_bytes(method, url, headers, body, timeout, service):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise DeliveryError(service + "_response_too_large", ambiguous=True)
        return payload
    except urllib.error.HTTPError as exc:
        # Do not include raw URLs, response bodies, headers or tokens in errors.
        raise DeliveryError(f"{service}_http_{exc.code}", ambiguous=exc.code >= 500) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise DeliveryError(service + "_network_error", ambiguous=True) from None


def annotate_photo(raw, result):
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    # Fixed padded output also avoids Telegram's extreme aspect-ratio limit.
    original_width, original_height = image.size
    image.thumbnail((640, 480))
    left, top = (640-image.width)//2, (480-image.height)//2
    scale_x, scale_y = image.width/original_width, image.height/original_height
    canvas = Image.new("RGB", (640, 480), "#14262d")
    canvas.paste(image, (left, top))
    draw = ImageDraw.Draw(canvas)
    for detection in result["detections"]:
        x1, y1, x2, y2 = detection["box"]
        box = [round(x1*scale_x+left), round(y1*scale_y+top),
               round(x2*scale_x+left), round(y2*scale_y+top)]
        color = COLORS[detection["label"]]
        draw.rectangle(box, outline=color, width=3)
        # ASCII labels are portable even in a minimal Docker image without VN fonts.
        label = f"{detection['label']} {detection['confidence']:.0%}"
        text_box = draw.textbbox((0, 0), label)
        width, height = text_box[2]-text_box[0]+8, text_box[3]-text_box[1]+8
        tx, ty = min(max(0, box[0]), max(0, 640-width)), max(0, box[1]-height)
        draw.rectangle((tx, ty, tx+width, ty+height), fill=color)
        draw.text((tx+4, ty+2), label, fill="white")
    target = io.BytesIO()
    canvas.save(target, format="JPEG", quality=85)
    return target.getvalue()


def photo_form(chat_id, caption, photo):
    boundary = "masklab-" + uuid.uuid4().hex
    parts = []
    for name, value in [("chat_id", chat_id), ("caption", caption)]:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                      f"{value}\r\n").encode("utf-8"))
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"alert.jpg\"\r\n"
                  "Content-Type: image/jpeg\r\n\r\n").encode())
    parts.extend([photo, f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


class AlertService:
    def __init__(self, config, transport=request_bytes, clock=time.monotonic):
        self.config, self.transport, self.clock = config, transport, clock
        self.lock = threading.Lock()
        self.history = OrderedDict()
        self.events = OrderedDict()
        self.jobs = queue.Queue(maxsize=2)
        self.last_accepted = -math.inf
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        if self.config.enabled:
            self.thread = threading.Thread(target=self._worker, name="mask-alerts", daemon=True)
            self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)

    def snapshot(self, session_id=None):
        with self.lock:
            recent = [copy.deepcopy({k: v for k, v in row.items() if k != "detections"})
                      for row in reversed(list(self.events.values()))
                      if session_id is None or row["session_id"] == session_id][:10]
            return {"enabled": self.config.enabled, "configuration_error": self.config.error,
                    "cooldown_seconds": self.config.cooldown,
                    "cooldown_remaining_seconds": round(max(0, self.config.cooldown-(self.clock()-self.last_accepted)), 1),
                    "queue_size": self.jobs.qsize(), "recent": recent}

    def observe(self, session_id, result, raw, notify=False):
        if not self.config.enabled:
            return {"state": "not_configured" if self.config.error else "disabled"}
        if not session_id:
            return {"state": "missing_session"}
        with self.lock:
            if not notify:
                self.history.pop(session_id, None)
                return {"state": "paused"}
            now = self.clock()
            observations = self.history.setdefault(session_id, deque(maxlen=3))
            if observations and now-observations[-1][0] > self.config.window_seconds:
                observations.clear()
            # Strict time window: stale results must not confirm a current violation.
            while observations and now-observations[0][0] > self.config.window_seconds:
                observations.popleft()
            current = {d["label"] for d in result["detections"] if d["label"] in VIOLATIONS}
            observations.append((now, current))
            self.history.move_to_end(session_id)
            while len(self.history) > 128:
                self.history.popitem(last=False)
            votes = Counter(label for _, labels in observations for label in labels)
            confirmed = sorted(label for label in current if votes[label] >= 2)
            if len(observations) < 3 or not confirmed:
                return {"state": "observing" if current else "no_violation"}
            remaining = self.config.cooldown-(now-self.last_accepted)
            if remaining > 0:
                return {"state": "cooldown", "remaining_seconds": round(remaining, 1)}
            event_id = str(uuid.uuid4())
            counts = dict(Counter(d["label"] for d in result["detections"]))
            row = {"id": event_id, "occurred_at": utc_now(), "session_id": session_id,
                   "violation_labels": confirmed, "counts": counts, "detections": copy.deepcopy(result["detections"]),
                   "model_sha256": result["model_sha256"], "inference_ms": result["timing"]["inference_ms"],
                   "cloud_pipeline_ms": result["timing"]["cloud_pipeline_ms"], "image_path": None,
                   "storage_status": "pending", "telegram_status": "pending", "telegram_message_id": None,
                   "telegram_ack_ms": None, "errors": [], "completed_at": None, "log_status": "pending"}
            try:
                self.jobs.put_nowait((copy.deepcopy(row), bytes(raw), copy.deepcopy(result), now))
            except queue.Full:
                return {"state": "queue_full"}
            self.events[event_id] = row
            while len(self.events) > 20:
                self.events.popitem(last=False)
            self.last_accepted = now
            return {"state": "queued", "event_id": event_id}

    def _supabase(self, endpoint, data, content_type="application/json", extra=None):
        headers = {"apikey": self.config.supabase_key, "Content-Type": content_type}
        if self.config.supabase_key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + self.config.supabase_key
        headers.update(extra or {})
        # DB UUID and object path are fixed, making retries idempotent.
        for attempt in range(2):
            try:
                return self.transport("POST", self.config.supabase_url + endpoint, headers,
                                      data, self.config.timeout, "supabase")
            except DeliveryError as exc:
                if attempt or not (exc.ambiguous or exc.code == "supabase_http_429"):
                    raise
                time.sleep(.2)

    def _save_row(self, row):
        payload = {k: v for k, v in row.items() if k != "log_status"}
        return self._supabase("/rest/v1/mask_alerts?on_conflict=id", json.dumps(payload).encode(),
                              extra={"Prefer": "resolution=merge-duplicates,return=minimal"})

    def _publish(self, row):
        with self.lock:
            self.events[row["id"]] = copy.deepcopy(row)
            while len(self.events) > 20:
                self.events.popitem(last=False)

    def _process(self, row, raw, result, accepted_at):
        try:
            self._save_row(row)
            row["log_status"] = "saved"
        except DeliveryError as exc:
            row["log_status"] = "failed"
            row["errors"].append(exc.code)
        self._publish(row)
        try:
            photo = annotate_photo(raw, result)
        except Exception:
            row.update(storage_status="failed", telegram_status="failed", completed_at=utc_now())
            row["errors"].append("photo_encoding_failed")
            self._finish(row)
            return
        try:
            image_path = row["occurred_at"][:10] + "/" + row["id"] + ".jpg"
            endpoint = "/storage/v1/object/" + self.config.bucket + "/" + image_path
            self._supabase(endpoint, photo, "image/jpeg", {"x-upsert": "true"})
            row.update(image_path=image_path, storage_status="saved")
        except DeliveryError as exc:
            row["storage_status"] = "failed"
            row["errors"].append(exc.code)
        self._publish(row)
        try:
            occurred = datetime.fromisoformat(row["occurred_at"]).astimezone(timezone(timedelta(hours=7)))
            caption = ("CẢNH BÁO KHẨU TRANG\n"
                       + ", ".join(VN_NAMES[label] for label in row["violation_labels"])
                       + "\nThời gian: " + occurred.strftime("%d/%m/%Y %H:%M:%S") + " (UTC+7)"
                       + "\nKhuôn mặt: " + str(sum(row["counts"].values()))
                       + f"\nSuy luận: {row['inference_ms']:.2f} ms"
                       + "\nMã sự kiện: " + row["id"])
            body, content_type = photo_form(self.config.chat_id, caption, photo)
            response = self.transport("POST", "https://api.telegram.org/bot" + self.config.bot_token + "/sendPhoto",
                                      {"Content-Type": content_type}, body, self.config.timeout, "telegram")
            data = json.loads(response)
            if not data.get("ok"):
                code = data.get("error_code", 0)
                raise DeliveryError(f"telegram_api_{code}", ambiguous=not isinstance(code, int) or code >= 500)
            message_id = data.get("result", {}).get("message_id")
            if not isinstance(message_id, int):
                raise DeliveryError("telegram_invalid_response", ambiguous=True)
            row.update(telegram_status="sent", telegram_message_id=message_id,
                       telegram_ack_ms=round((self.clock()-accepted_at)*1000, 2))
        except DeliveryError as exc:
            row["telegram_status"] = "unknown" if exc.ambiguous else "failed"
            row["errors"].append(exc.code)
        except (ValueError, TypeError, AttributeError):
            row["telegram_status"] = "unknown"
            row["errors"].append("telegram_invalid_response")
        # Do not retry an ambiguous Telegram timeout: the photo might already have arrived.
        row["completed_at"] = utc_now()
        self._finish(row)

    def _finish(self, row):
        try:
            self._save_row(row)
            row["log_status"] = "saved"
        except DeliveryError as exc:
            row["log_status"] = "failed"
            row["errors"].append(exc.code)
        self._publish(row)

    def _worker(self):
        while not self.stopping.is_set():
            try:
                job = self.jobs.get(timeout=.3)
            except queue.Empty:
                continue
            row = job[0]
            try:
                self._process(*job)
            except Exception:
                # Keep worker alive; never expose a traceback that may contain credentials.
                row.update(telegram_status="unknown", completed_at=utc_now())
                row["errors"].append("background_processing_error")
                self._publish(row)
            finally:
                self.jobs.task_done()
