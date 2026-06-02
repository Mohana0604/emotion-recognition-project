import os, json, time, threading, hashlib, smtplib, datetime, random, string
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

from flask import Flask, Response, jsonify, request, redirect, url_for, session, render_template
from flask_cors import CORS
import cv2, numpy as np
from scipy.spatial import distance as dist
import dlib
from imutils import face_utils
import pyttsx3, atexit
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import mysql.connector
from mysql.connector import pooling

app = Flask(__name__)
app.secret_key = "blink_emosense_unified_2024"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "communication_log.jsonl")

# ── MYSQL CONFIG ─────────────────────────────────────────────
DB_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "user":     "root",          # ← Change to your MySQL username
    "password": "userhost@123",  # ← Change to your MySQL password
    "database": "emosense_db",
    "charset":  "utf8mb4",
    "autocommit": True,
}

# Connection pool
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="emosense_pool",
        pool_size=10,
        pool_reset_session=True,
        **DB_CONFIG
    )
    print("[DB] MySQL connection pool created")
except Exception as e:
    print(f"[DB] Pool creation failed: {e}")
    db_pool = None


def get_db():
    """Get a connection from the pool."""
    if db_pool:
        return db_pool.get_connection()
    return mysql.connector.connect(**DB_CONFIG)


def init_db():
    """Create all tables if they do not exist."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          VARCHAR(64)  PRIMARY KEY,
            username    VARCHAR(80)  UNIQUE NOT NULL,
            email       VARCHAR(120) UNIQUE NOT NULL,
            password    VARCHAR(64)  NOT NULL,
            age         INT,
            gender      VARCHAR(20),
            phone       VARCHAR(20),
            joined      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            user_id     VARCHAR(64) NOT NULL,
            type        ENUM('blink','emotion') NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            duration    INT DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_emotions (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            session_id  INT NOT NULL,
            emotion     VARCHAR(30) NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_words (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            session_id  INT NOT NULL,
            word        VARCHAR(40) NOT NULL,
            selected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blink_log (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            user_id     VARCHAR(64) NOT NULL,
            word        VARCHAR(40) NOT NULL,
            logged_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("[DB] Tables initialised")


# ── EMAIL CONFIG ─────────────────────────────────────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "pmohana2004@gmail.com"
SMTP_PASSWORD = "yofrlamsnwhhccxx"

# ── OTP STORE (in-memory) ─────────────────────────────────────
_otp_store = {}
_otp_lock  = threading.Lock()
OTP_EXPIRY = 300


def generate_otp():
    return ''.join(random.choices(string.digits, k=6))


def store_otp(email, otp, pending_data=None):
    with _otp_lock:
        _otp_store[email.lower()] = {
            "otp":     otp,
            "expires": time.time() + OTP_EXPIRY,
            "data":    pending_data or {}
        }


def verify_otp(email, otp_input):
    with _otp_lock:
        entry = _otp_store.get(email.lower())
        if not entry:
            return False, "No OTP found for this email. Please request a new one."
        if time.time() > entry["expires"]:
            del _otp_store[email.lower()]
            return False, "OTP has expired. Please request a new one."
        if entry["otp"] != otp_input.strip():
            return False, "Incorrect OTP. Please try again."
        data = entry["data"]
        del _otp_store[email.lower()]
        return True, data


# ── USER DB HELPERS (MySQL) ───────────────────────────────────

def hash_pw(p):
    return hashlib.sha256(str(p).strip().encode()).hexdigest()


def db_get_user_by_email(email):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE LOWER(email)=%s", (email.lower(),))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_get_user_by_id(uid):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_username_exists(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE LOWER(username)=%s", (username.lower(),))
    exists = cur.fetchone() is not None
    cur.close(); conn.close()
    return exists


def db_email_exists(email):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE LOWER(email)=%s", (email.lower(),))
    exists = cur.fetchone() is not None
    cur.close(); conn.close()
    return exists


def db_create_user(uid, data):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (id, username, email, password, age, gender, phone)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (uid, data["username"], data["email"], data["password"],
          data.get("age"), data.get("gender"), data.get("phone")))
    conn.commit()
    cur.close(); conn.close()


def db_save_emotion_session(uid, emotions, duration):
    if not emotions:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (user_id, type, duration) VALUES (%s,'emotion',%s)",
                (uid, duration))
    sess_id = cur.lastrowid
    if emotions:
        cur.executemany("INSERT INTO session_emotions (session_id, emotion) VALUES (%s,%s)",
                        [(sess_id, e) for e in emotions[-100:]])
    conn.commit()
    cur.close(); conn.close()


def db_save_blink_session(uid, words):
    if not words:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (user_id, type, duration) VALUES (%s,'blink',%s)",
                (uid, len(words)))
    sess_id = cur.lastrowid
    cur.executemany("INSERT INTO session_words (session_id, word) VALUES (%s,%s)",
                    [(sess_id, w) for w in words])
    conn.commit()
    cur.close(); conn.close()


def db_log_blink_word(uid, word):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO blink_log (user_id, word) VALUES (%s,%s)", (uid, word))
    conn.commit()
    cur.close(); conn.close()


def db_get_user_stats(uid):
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) as total FROM sessions WHERE user_id=%s", (uid,))
    total = cur.fetchone()["total"]

    cur.execute("""
        SELECT se.emotion, COUNT(*) as cnt
        FROM session_emotions se
        JOIN sessions s ON s.id=se.session_id
        WHERE s.user_id=%s
        GROUP BY se.emotion
    """, (uid,))
    emotions = {r["emotion"]: r["cnt"] for r in cur.fetchall()}

    cur.execute("""
        SELECT s.id, s.type, s.created_at, s.duration,
               GROUP_CONCAT(se.emotion ORDER BY se.id SEPARATOR ',') as emotions,
               GROUP_CONCAT(sw.word ORDER BY sw.id SEPARATOR ',') as words
        FROM sessions s
        LEFT JOIN session_emotions se ON se.session_id=s.id
        LEFT JOIN session_words sw ON sw.session_id=s.id
        WHERE s.user_id=%s
        GROUP BY s.id
        ORDER BY s.created_at DESC
        LIMIT 5
    """, (uid,))
    rows = cur.fetchall()
    recent = []
    for r in rows:
        emotions_list = r["emotions"].split(",") if r["emotions"] else []
        words_list    = r["words"].split(",") if r["words"] else []
        recent.append({
            "date":     r["created_at"].isoformat() if r["created_at"] else "",
            "type":     r["type"],
            "emotions": emotions_list,
            "words":    words_list,
            "duration": r["duration"],
        })
    cur.close(); conn.close()
    return {"total_sessions": total, "emotion_counts": emotions, "recent_sessions": recent}


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None, None, None
    u = db_get_user_by_id(uid)
    if not u:
        return None, None, None
    return uid, u.get("email", ""), u.get("username", "User")


# ── EMAIL CORE ────────────────────────────────────────────────
def _send_email(to, subject, html):
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, [to], msg.as_string())
        print(f"[EMAIL] ✓ delivered to={to}")
    except Exception as e:
        print(f"[EMAIL] ✗ {e}")


def send_otp_email(to, username, otp):
    subject = "EmoSense — Your Verification Code"
    html = (
        "<div style='font-family:Arial;background:#080c10;color:#e8f0f5;padding:32px;"
        "border-radius:12px;max-width:540px;margin:auto'>"
        "<div style='background:linear-gradient(135deg,#7b5cfa,#00e5c0);padding:16px 24px;"
        "border-radius:10px;margin-bottom:24px'>"
        "<h2 style='margin:0;color:#fff'>🧠 EmoSense Verification</h2></div>"
        f"<p>Hi <strong>{username}</strong>,</p>"
        "<p style='color:#a0b4cc;line-height:1.7'>Use the OTP below to complete your registration. "
        "It expires in <strong>5 minutes</strong>.</p>"
        "<div style='background:#131b22;border:2px dashed #7b5cfa;border-radius:14px;"
        "padding:24px;text-align:center;margin:24px 0'>"
        f"<div style='font-size:42px;font-weight:900;letter-spacing:12px;color:#00e5c0'>{otp}</div>"
        "<div style='font-size:12px;color:#4a6070;margin-top:8px'>One-Time Password · Valid for 5 minutes</div>"
        "</div>"
        f"<p style='font-size:12px;color:#4a6070'>Sent at: "
        f"{datetime.datetime.now().strftime('%d %b %Y, %I:%M:%S %p')}</p></div>"
    )
    threading.Thread(target=_send_email, args=(to, subject, html), daemon=True).start()


def send_blink_alert(to, username, word, sentence):
    print(f"[ALERT] blink to={to!r} word={word!r}")
    if not to or "@" not in to or "." not in to.split("@")[-1]:
        return
    subject = f"URGENT — Patient selected {word}"
    html = (
        "<div style='font-family:Arial;background:#080c10;color:#e8f0f5;padding:32px;"
        "border-radius:12px;max-width:540px;margin:auto'>"
        "<div style='background:linear-gradient(135deg,#ff6b35,#ff3b3b);padding:16px 24px;"
        "border-radius:10px;margin-bottom:24px'>"
        "<h2 style='margin:0;color:#fff'>URGENT Caretaker Alert</h2></div>"
        f"<p>Patient <strong>{username}</strong> needs immediate attention.</p>"
        "<div style='background:#131b22;border-left:4px solid #ff6b35;padding:16px 20px;"
        "margin:18px 0;border-radius:0 8px 8px 0'>"
        "<div style='font-size:12px;color:#4a6070;margin-bottom:6px'>CRITICAL WORD SELECTED</div>"
        f"<div style='font-size:28px;font-weight:800;color:#ff6b35'>{word}</div></div>"
        "<div style='background:#131b22;border-left:4px solid #00e8a2;padding:14px 20px;"
        "border-radius:0 8px 8px 0;margin-bottom:20px'>"
        "<div style='font-size:12px;color:#4a6070;margin-bottom:4px'>FULL SENTENCE</div>"
        f"<div style='font-size:16px;color:#00e8a2'>{sentence.strip() or 'N/A'}</div></div>"
        f"<p style='font-size:12px;color:#4a6070'>Time: "
        f"{datetime.datetime.now().strftime('%d %b %Y, %I:%M:%S %p')}</p></div>"
    )
    threading.Thread(target=_send_email, args=(to, subject, html), daemon=True).start()


def send_emotion_alert(to, username, emotion, confidence, elapsed):
    print(f"[ALERT] emotion to={to!r} emotion={emotion!r}")
    if not to or "@" not in to or "." not in to.split("@")[-1]:
        return
    subject = f"EmoSense Wellbeing Alert — {emotion.title()} Detected"
    tips = {
        "fear":    "5-4-3-2-1 grounding. Box breathe 4s. iCall: 9152987821",
        "sadness": "10-min walk. Call someone. iCall: 9152987821"
    }
    html = (
        "<div style='font-family:Arial;background:#080c12;color:#e8edf5;padding:32px;"
        "border-radius:12px;max-width:540px;margin:auto'>"
        "<div style='background:linear-gradient(135deg,#7b5cfa,#00e5c0);padding:16px 24px;"
        "border-radius:10px;margin-bottom:24px'>"
        "<h2 style='margin:0;color:#fff'>EmoSense Wellbeing Alert</h2></div>"
        f"<p>Hi <strong>{username}</strong>,</p>"
        f"<p style='color:#a0b4cc;line-height:1.7'>We detected "
        f"<strong style='color:#ff8fa3'>{emotion.upper()}</strong>"
        f" ({confidence}% confidence) persisting for <strong>{int(elapsed)}s</strong>.</p>"
        "<div style='background:#0e1420;border-left:4px solid #b47efa;padding:14px 20px;"
        "border-radius:0 8px 8px 0;margin:16px 0'>"
        f"<div style='color:#a0b4cc'>{tips.get(emotion, 'Please reach out for support.')}</div></div>"
        "<p style='font-size:12px;color:#5a7090'>Vandrevala: 1860-2662-345 | iCall: 9152987821</p></div>"
    )
    threading.Thread(target=_send_email, args=(to, subject, html), daemon=True).start()


# ── BLINK MODEL ───────────────────────────────────────────────
MODEL_PATH = os.path.join(BASE_DIR, "shape_predictor_68_face_landmarks.dat")
BLINK_READY = os.path.exists(MODEL_PATH)
if BLINK_READY:
    blink_det  = dlib.get_frontal_face_detector()
    blink_pred = dlib.shape_predictor(MODEL_PATH)
    (lS, lE)   = face_utils.FACIAL_LANDMARKS_IDXS["left_eye"]
    (rS, rE)   = face_utils.FACIAL_LANDMARKS_IDXS["right_eye"]
    print("[BLINK] dlib ready")
else:
    print("[BLINK] model missing — demo mode")


def ear(eye):
    A = dist.euclidean(eye[1], eye[5])
    B = dist.euclidean(eye[2], eye[4])
    C = dist.euclidean(eye[0], eye[3])
    return (A + B) / (2.0 * C)


# ── EMOTION ML ────────────────────────────────────────────────
ML_READY = False
try:
    from tensorflow.keras.applications import MobileNetV2  # type: ignore
    from tensorflow.keras.layers import Dense, GlobalAveragePooling2D  # type: ignore
    from tensorflow.keras.models import Model  # type: ignore
    from tensorflow.keras.applications.mobilenet_v2 import preprocess_input  # type: ignore
    from mtcnn import MTCNN
    _emo_labels = np.load(os.path.join(BASE_DIR, "emotion_labels.npy"), allow_pickle=True)
    _base = MobileNetV2(weights=None, include_top=False, input_shape=(224, 224, 3))
    _x    = GlobalAveragePooling2D()(_base.output)
    _x    = Dense(128, activation="relu")(_x)
    _out  = Dense(len(_emo_labels), activation="softmax")(_x)
    emotion_model = Model(inputs=_base.input, outputs=_out)
    emotion_model.load_weights(os.path.join(BASE_DIR, "face_emotion_model.h5"))
    mtcnn_det = MTCNN()
    ML_READY  = True
    print("[EMOTION] ML ready")
except Exception as e:
    print(f"[EMOTION] demo mode: {e}")

DEMO_EMOTIONS = ["happy", "surprise", "sadness", "anger", "fear", "disgust", "neutral"]

BLINK_OPTIONS = [
    "YES", "NO", "HELP", "WATER", "FOOD", "PAIN", "MEDICINE", "TOILET",
    "HAPPY", "SAD", "SCARED", "TIRED", "OKAY", "UNCOMFORTABLE",
    "SPEAK", "CLEAR", "REPEAT", "URGENT", "STOP"
]
CRITICAL_WORDS = {"HELP", "PAIN", "MEDICINE", "TOILET", "UNCOMFORTABLE", "SCARED", "URGENT"}

# ── TTS ───────────────────────────────────────────────────────
_tts_lock = threading.Lock()


def speak_async(text):
    def _s():
        with _tts_lock:
            e = pyttsx3.init()
            e.setProperty("rate", 145)
            e.say(text)
            e.runAndWait()
    threading.Thread(target=_s, daemon=True).start()


# ── PER-USER EMAIL/NAME CACHE ─────────────────────────────────
_uid_cache  = {}
_cache_lock = threading.Lock()


def cache_user(uid, email, username):
    with _cache_lock:
        _uid_cache[uid] = {"email": email, "username": username}


def get_cached_user(uid):
    with _cache_lock:
        c = _uid_cache.get(uid, {})
    return c.get("email", ""), c.get("username", "User")


# ── PER-USER STATE ────────────────────────────────────────────
_ustates = {}
_ulock   = threading.Lock()


def get_state(uid):
    with _ulock:
        if uid not in _ustates:
            _ustates[uid] = {
                "sel": 0, "sentence": "", "blink_count": 0,
                "last_switch": time.time(), "blink_det": False,
                "fcounter": 0, "ear_val": 0.0, "face_ok": False,
                "scan_speed": 2.0, "ear_thresh": 0.23,
                "last_blink_t": 0, "cooldown": 1.2, "min_frames": 3,
                "paused": False, "log": [],
                "last_alert_t": 0, "alert_cd": 15,
                "emotion": None, "emo_conf": 0,
                "emo_buf": [], "emo_start": {}, "last_emo_alert": {},
                "sess_emotions": [],
                "last_sess_save": 0,
                "word_buffer": [],   # for DB blink session
            }
        return _ustates[uid]


# ── CAMERA ────────────────────────────────────────────────────
_camera = None
_latest = None
_flock  = threading.Lock()


def cam_loop():
    global _latest, _camera
    _camera = cv2.VideoCapture(0)
    _camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    _camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    while True:
        ok, fr = _camera.read()
        if ok:
            with _flock:
                _latest = fr.copy()
        else:
            time.sleep(0.05)


threading.Thread(target=cam_loop, daemon=True).start()


# ── FRAME PROCESSORS ─────────────────────────────────────────

def proc_blink(frame, uid, email, username):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    s = get_state(uid)
    word_speak = None
    alert_word = None

    if BLINK_READY:
        faces = blink_det(gray, 0)
        s["face_ok"] = len(faces) > 0
        for face in faces:
            sh    = face_utils.shape_to_np(blink_pred(gray, face))
            le    = sh[lS:lE]
            re    = sh[rS:rE]
            e_val = (ear(le) + ear(re)) / 2.0
            s["ear_val"] = round(e_val, 3)
            if e_val < s["ear_thresh"]:
                s["fcounter"] += 1
            else:
                if s["fcounter"] >= s["min_frames"]:
                    now = time.time()
                    if now - s["last_blink_t"] > s["cooldown"]:
                        s["blink_det"]    = True
                        s["blink_count"] += 1
                        s["last_blink_t"] = now
                s["fcounter"] = 0
            for eye in [le, re]:
                cv2.drawContours(frame, [cv2.convexHull(eye)], -1, (0, 255, 180), 1)
            x, y, w, h = face.left(), face.top(), face.width(), face.height()
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 230, 120), 2)
            break
    else:
        s["face_ok"] = False
        s["ear_val"] = 0.0

    if not s["paused"]:
        now = time.time()
        if now - s["last_switch"] > s["scan_speed"]:
            s["sel"]         = (s["sel"] + 1) % len(BLINK_OPTIONS)
            s["last_switch"] = now

    if s["blink_det"] and not s["paused"]:
        s["blink_det"] = False
        word = BLINK_OPTIONS[s["sel"]]
        if word == "CLEAR":
            s["sentence"] = ""
            word_speak    = "Cleared"
        elif word in ("SPEAK", "REPEAT"):
            word_speak = s["sentence"].strip() or "Nothing to say"
        elif word == "STOP":
            s["paused"] = True
            word_speak  = "Paused"
        else:
            s["sentence"] += word + " "
            word_speak     = word
            entry = {"time": datetime.datetime.now().isoformat(), "word": word}
            s["log"].append(entry)
            s["word_buffer"].append(word)
            # Log to DB
            threading.Thread(target=db_log_blink_word, args=(uid, word), daemon=True).start()
            # Save blink session every 5 words
            if len(s["word_buffer"]) % 5 == 0:
                words_copy = list(s["word_buffer"])
                threading.Thread(target=db_save_blink_session,
                                 args=(uid, words_copy), daemon=True).start()
            if word in CRITICAL_WORDS:
                now = time.time()
                if now - s["last_alert_t"] > s["alert_cd"]:
                    s["last_alert_t"] = now
                    alert_word = word
    if word_speak:
        speak_async(word_speak)
    if alert_word:
        cached_email, cached_username = get_cached_user(uid)
        eff_email    = cached_email    or email
        eff_username = cached_username or username
        if eff_email:
            send_blink_alert(eff_email, eff_username, alert_word, s["sentence"])
    return frame


def proc_emotion(frame, uid, email, username):
    s = get_state(uid)
    cached_email, cached_username = get_cached_user(uid)
    eff_email    = cached_email    or email
    eff_username = cached_username or username

    if ML_READY:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        for face in mtcnn_det.detect_faces(rgb):
            x, y, w, h = face["box"]
            x, y = max(0, x), max(0, y)
            fi   = rgb[y:y + h, x:x + w]
            try:
                fi   = preprocess_input(cv2.resize(fi, (224, 224)).astype("float32"))
                pred = emotion_model.predict(fi[None], verbose=0)
                raw  = _emo_labels[pred.argmax()]
                conf = float(pred.max())
                buf  = s["emo_buf"]
                buf.append(raw)
                if len(buf) > 7:
                    buf.pop(0)
                smoothed = max(set(buf), key=buf.count)
                if conf > 0.55:
                    s["emotion"]  = smoothed
                    s["emo_conf"] = round(conf * 100, 1)
                    s["sess_emotions"].append(smoothed)
                    if smoothed in ("fear", "sadness"):
                        now = time.time()
                        s["emo_start"].setdefault(smoothed, now)
                        elapsed = now - s["emo_start"][smoothed]
                        last    = s["last_emo_alert"].get(smoothed, 0)
                        if elapsed >= 20 and (now - last) > 1800:
                            s["last_emo_alert"][smoothed] = now
                            s["emo_start"].pop(smoothed, None)
                            if eff_email:
                                send_emotion_alert(eff_email, eff_username,
                                                   smoothed, round(conf * 100, 1), elapsed)
                    else:
                        s["emo_start"].pop(smoothed, None)
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 229, 192), 2)
                cv2.putText(frame, f"{s.get('emotion','')} {s.get('emo_conf',0)}%",
                            (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 229, 192), 2)
            except Exception as e:
                print(f"[EMOTION] frame error: {e}")
    else:
        slot  = int(time.time() / 30) % len(DEMO_EMOTIONS)
        demo  = DEMO_EMOTIONS[slot]
        s["emotion"]  = demo
        s["emo_conf"] = round(random.uniform(60, 90), 1)
        s["sess_emotions"].append(demo)
        cv2.putText(frame, f"DEMO: {demo}", (10, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
        if demo in ("fear", "sadness"):
            now = time.time()
            s["emo_start"].setdefault(demo, now)
            elapsed = now - s["emo_start"][demo]
            last    = s["last_emo_alert"].get(demo, 0)
            if elapsed >= 20 and (now - last) > 1800:
                s["last_emo_alert"][demo] = now
                s["emo_start"].pop(demo, None)
                if eff_email:
                    send_emotion_alert(eff_email, eff_username,
                                       demo, round(s["emo_conf"], 1), elapsed)
        else:
            for emo in ("fear", "sadness"):
                s["emo_start"].pop(emo, None)

    now = time.time()
    if s["sess_emotions"] and (now - s.get("last_sess_save", 0)) > 30:
        s["last_sess_save"] = now
        emotions_copy = list(s["sess_emotions"])
        duration = len(emotions_copy)
        threading.Thread(target=db_save_emotion_session,
                         args=(uid, emotions_copy, duration), daemon=True).start()
        s["sess_emotions"] = []
    return frame


# ── STREAM GENERATOR ──────────────────────────────────────────
def generate_frames(uid, email, username, mode="both"):
    _email    = email
    _username = username
    while True:
        cached_e, cached_u = get_cached_user(uid)
        if cached_e:
            _email    = cached_e
            _username = cached_u
        with _flock:
            if _latest is None:
                time.sleep(0.05)
                continue
            frame = _latest.copy()
        if mode in ("both", "blink"):
            frame = proc_blink(frame, uid, _email, _username)
        if mode in ("both", "emotion"):
            frame = proc_emotion(frame, uid, _email, _username)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
        time.sleep(0.033)


# ── AUTH ROUTES ───────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user_id" in session else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        if not email or not password:
            error = "Enter email and password."
        else:
            user = db_get_user_by_email(email)
            if not user:
                error = "Email not found. Please register first."
            elif user["password"] != hash_pw(password):
                error = "Incorrect password."
            else:
                session["user_id"]  = user["id"]
                session["username"] = user["username"]
                cache_user(user["id"], user["email"], user["username"])
                return redirect(url_for("dashboard"))
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    error   = None
    success = None
    step    = request.args.get("step", "form")

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "send_otp":
            username = request.form.get("username", "").strip()
            age      = request.form.get("age", "").strip()
            gender   = request.form.get("gender", "").strip()
            phone    = request.form.get("phone", "").strip()
            email    = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "").strip()
            confirm  = request.form.get("confirm", "").strip()

            if not all([username, age, gender, phone, email, password, confirm]):
                error = "All fields are required."
            elif len(password) < 6:
                error = "Password must be at least 6 characters."
            elif len(password) > 32:
                error = "Password must be 32 characters or fewer."
            elif password != confirm:
                error = "Passwords do not match."
            elif db_username_exists(username):
                error = "Username already taken."
            elif db_email_exists(email):
                error = "Email already registered."
            else:
                otp = generate_otp()
                store_otp(email, otp, pending_data={
                    "username": username, "age": age, "gender": gender,
                    "phone": phone, "email": email, "password": hash_pw(password)
                })
                send_otp_email(email, username, otp)
                session["pending_email"] = email
                return redirect(url_for("register") + "?step=verify")

        elif action == "verify_otp":
            otp_input = request.form.get("otp", "").strip()
            email = request.form.get("email", "").strip().lower() or session.get("pending_email", "")
            if not email:
                error = "Session expired. Please register again."
                step  = "form"
            else:
                ok, result = verify_otp(email, otp_input)
                if ok:
                    data = result
                    uid  = hashlib.md5(f"{data['username']}{time.time()}".encode()).hexdigest()
                    try:
                        db_create_user(uid, data)
                        session.pop("pending_email", None)
                        session["user_id"]  = uid
                        session["username"] = data["username"]
                        cache_user(uid, data["email"], data["username"])
                        return redirect(url_for("dashboard"))
                    except Exception as e:
                        error = f"Could not save account: {e}"
                else:
                    error = result
                    step  = "verify"

        elif action == "resend_otp":
            email = session.get("pending_email", "")
            if not email:
                error = "Session expired. Please register again."
                step  = "form"
            else:
                with _otp_lock:
                    entry    = _otp_store.get(email, {})
                    username = entry.get("data", {}).get("username", "User")
                otp = generate_otp()
                store_otp(email, otp, pending_data=entry.get("data", {}))
                send_otp_email(email, username, otp)
                success = "A new OTP has been sent to your email."
                step    = "verify"

    if request.args.get("step") == "verify" or step == "verify":
        pending_email = session.get("pending_email", "")
        return render_template("register.html", error=error, success=success,
                               step="verify", pending_email=pending_email)
    return render_template("register.html", error=error, success=success, step="form")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    uid, email, username = current_user()
    if not uid:
        session.clear()
        return redirect(url_for("login"))
    cache_user(uid, email, username)
    user = db_get_user_by_id(uid) or {}
    return render_template("dashboard.html", user=user)


# ── APP ROUTES ────────────────────────────────────────────────

@app.route("/blink")
def blink_app():
    if "user_id" not in session:
        return redirect(url_for("login"))
    uid, email, username = current_user()
    cache_user(uid, email, username)
    return render_template("blink.html", username=username)


@app.route("/emotion")
def emotion_app():
    if "user_id" not in session:
        return redirect(url_for("login"))
    uid, email, username = current_user()
    cache_user(uid, email, username)
    user = db_get_user_by_id(uid) or {}
    return render_template("emotion.html", user=user)


# ── VIDEO STREAMS ─────────────────────────────────────────────

def _get_uid_email_username():
    uid = session.get("user_id")
    if not uid:
        return None, None, None
    u = db_get_user_by_id(uid) or {}
    return uid, u.get("email", ""), u.get("username", "User")


@app.route("/video/blink")
def video_blink():
    if "user_id" not in session:
        return "Unauthorized", 401
    uid, email, username = _get_uid_email_username()
    if not uid:
        return "Unauthorized", 401
    cache_user(uid, email, username)
    return Response(generate_frames(uid, email, username, "blink"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/video/emotion")
def video_emotion():
    if "user_id" not in session:
        return "Unauthorized", 401
    uid, email, username = _get_uid_email_username()
    if not uid:
        return "Unauthorized", 401
    cache_user(uid, email, username)
    return Response(generate_frames(uid, email, username, "emotion"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/video/both")
def video_both():
    if "user_id" not in session:
        return "Unauthorized", 401
    uid, email, username = _get_uid_email_username()
    if not uid:
        return "Unauthorized", 401
    cache_user(uid, email, username)
    return Response(generate_frames(uid, email, username, "both"),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


# ── API: BLINK STATE ──────────────────────────────────────────

@app.route("/api/blink/state")
def api_blink_state():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    s = get_state(uid)
    return jsonify({
        "sentence": s["sentence"], "blink_count": s["blink_count"],
        "selected_index": s["sel"], "selected_word": BLINK_OPTIONS[s["sel"]],
        "ear": s["ear_val"], "face_detected": s["face_ok"],
        "scan_speed": s["scan_speed"], "ear_threshold": s["ear_thresh"],
        "paused": s["paused"], "log": s["log"][-20:], "options": BLINK_OPTIONS,
    })


@app.route("/api/blink/clear", methods=["POST"])
def api_blink_clear():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    get_state(uid)["sentence"] = ""
    return jsonify({"ok": True})


@app.route("/api/blink/speak", methods=["POST"])
def api_blink_speak():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    s = get_state(uid)
    speak_async(s["sentence"].strip() or "Nothing to say")
    return jsonify({"ok": True})


@app.route("/api/blink/settings", methods=["POST"])
def api_blink_settings():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    s = get_state(uid)
    d = request.json or {}
    if "scan_speed"    in d: s["scan_speed"] = float(d["scan_speed"])
    if "ear_threshold" in d: s["ear_thresh"] = float(d["ear_threshold"])
    if "paused"        in d: s["paused"]     = bool(d["paused"])
    return jsonify({"ok": True})


@app.route("/api/blink/alert", methods=["POST"])
def api_blink_alert():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, email, username = current_user()
    cached_email, cached_username = get_cached_user(uid)
    eff_email    = cached_email    or email
    eff_username = cached_username or username
    s   = get_state(uid)
    msg = s["sentence"].strip()
    if msg and eff_email:
        send_blink_alert(eff_email, eff_username, "MANUAL ALERT", msg)
    return jsonify({"ok": True})


# ── API: EMOTION STATE ────────────────────────────────────────

@app.route("/api/emotion/state")
def api_emotion_state():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    s = get_state(uid)
    return jsonify({"emotion": s["emotion"], "confidence": s["emo_conf"]})


@app.route("/api/user_stats")
def user_stats():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    stats = db_get_user_stats(uid)
    return jsonify(stats)


@app.route("/api/save_session", methods=["POST"])
def save_session_api():
    if "user_id" not in session:
        return jsonify({}), 401
    uid, _, _ = current_user()
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    emotions = data.get("emotions", [])
    duration = data.get("duration", len(emotions))
    threading.Thread(target=db_save_emotion_session,
                     args=(uid, emotions, duration), daemon=True).start()
    return jsonify({"ok": True})


# ── OTP RESEND API ────────────────────────────────────────────

@app.route("/api/resend_otp", methods=["POST"])
def api_resend_otp():
    email = session.get("pending_email", "")
    if not email:
        return jsonify({"ok": False, "error": "Session expired."}), 400
    with _otp_lock:
        entry    = _otp_store.get(email.lower(), {})
        username = entry.get("data", {}).get("username", "User")
        pdata    = entry.get("data", {})
    if not pdata:
        return jsonify({"ok": False, "error": "No pending registration found."}), 400
    otp = generate_otp()
    store_otp(email, otp, pending_data=pdata)
    send_otp_email(email, username, otp)
    return jsonify({"ok": True, "message": "New OTP sent to your email."})


# ── TEST EMAIL ────────────────────────────────────────────────

@app.route("/test_email")
def test_email():
    if "user_id" not in session:
        return "Login first", 401
    uid, email, username = current_user()
    if not email:
        return f"No email found for uid={uid}.", 400
    cache_user(uid, email, username)
    send_blink_alert(email, username, "TEST", "This is a test alert from BLink AAC.")
    return (f"<h3>Test alert dispatched to <b>{email}</b></h3>"
            "<p>Check your inbox and spam folder.</p>")


# ── DB STATUS API ─────────────────────────────────────────────

@app.route("/api/db_status")
def db_status():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        count = cur.fetchone()[0]
        cur.close(); conn.close()
        return jsonify({"status": "connected", "users": count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── CLEANUP ───────────────────────────────────────────────────
def _cleanup():
    if _camera and _camera.isOpened():
        _camera.release()


atexit.register(_cleanup)

if __name__ == "__main__":
    init_db()   # ← Creates tables on startup
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)