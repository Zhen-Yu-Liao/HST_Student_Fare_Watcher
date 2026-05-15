# -*- coding: utf-8 -*-
"""
THSR 監看 → 命中自動訂 (單檔版，使用 CONFIG 設定)

需求:
  pip install playwright ddddocr
  python -m playwright install chromium

說明:
  - 將下方 CONFIG 改成你的參數後，直接執行此檔。
  - 會每次開新瀏覽器查詢 → 發現符合折數(預設「學生5折」)就自動選班次並完成訂位。
  - 成功或到期未命中，都會寄 Email 通知。(無簡訊)

"""

from __future__ import annotations
import os, time, random, re, smtplib, traceback
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright
import ddddocr
from dotenv import load_dotenv

# =============================
#            CONFIG
# =============================
CONFIG = {
    "search": {
        "origin": "新竹",
        "dest": "台南",
        "date": "2026-05-11",   # YYYY-MM-DD
        "time": "11:00",        # 下拉選項顯示文字
        "adult": 0,
        "student": 1,
        "discount_key": "學生5折",  # 命中關鍵字 (例: 學生5折 / 學生75折 / 學生88折)
    },
    "booking": {
        "idno": "",
        "phone": "",
        "email": "",
    },
    "watch": {
        # 每回合等待秒數區間 (含隨機抖動)
        "interval_min": 3,
        "interval_max": 10,
        # 到期時間 (Asia/Taipei)，到期仍未命中會寄信並結束；留空代表無期限
        "until": "2026-05-11 12:30",
        # 安全網: 最多嘗試回合數 (None 代表不限制)
        "max_rounds": None,
    },
    "browser": {
        "use_edge": True,       # True 則使用 Edge channel
        "headless": True,      # 改 True 可無頭，瀏覽器就不會跳出來了，但開發測試時建議先用 False 看看流程跑得順不順
        "proxies_file": "proxies.txt",  # 可空字串或檔案不存在則不使用
        # 可選：覆寫 UA / Accept-Language
        "force_user_agent": None,  # 例如: Edge on Windows UA；None 則使用預設(隨 channel)
        "accept_language": "zh-TW,zh;q=0.9,en;q=0.8",
    },
    "notify": {
        "enabled": True,
        "smtp": {
            "host": "smtp.gmail.com",
            "port": 587,
            "username": "",
            "password": "",  # 16 碼 App Password
            "starttls": True,
        },
        "mail_from": "",
        "mail_to": [],
        "subject_prefix": "[THSR Watcher Test] ",
    },
}

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

URL = "https://irs.thsrc.com.tw/IMINT/?utm_source=thsrc&utm_medium=btnlink&utm_term=booking"
TZ = ZoneInfo("Asia/Taipei")
DEFAULT_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)


@dataclass
class SelectedTrain:
    train_no: Optional[str]
    discount_text: str
    row_text: str


@dataclass
class BookingEvidence:
    booking_code: str
    train_no: Optional[str]
    page_text: str
    ticket_html: Optional[str] = None
    html_path: Optional[str] = None
    text_path: Optional[str] = None
    screenshot_path: Optional[str] = None

# =============================
#          Utilities
# =============================

def _apply_env_overrides() -> None:
    booking = CONFIG["booking"]
    notify = CONFIG["notify"]
    smtp = notify["smtp"]

    booking["idno"] = os.getenv("THSRC_IDNO", booking["idno"])
    booking["phone"] = os.getenv("THSRC_PHONE", booking["phone"])
    booking["email"] = os.getenv("THSRC_EMAIL", booking["email"])

    gmail_sender = os.getenv("GMAIL_SENDER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    email_to = os.getenv("EMAIL_TO")

    if gmail_sender:
        smtp["username"] = gmail_sender
        notify["mail_from"] = gmail_sender
    if gmail_password:
        smtp["password"] = gmail_password
    if email_to:
        notify["mail_to"] = [addr.strip() for addr in email_to.split(",") if addr.strip()]


_apply_env_overrides()

def _until_dt() -> Optional[datetime]:
    s = CONFIG["watch"].get("until")
    if not s:
        return None
    try:
        if len(s) <= 10:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(hour=23, minute=59)
        else:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=TZ)
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    ts = _now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_booking_code(text: str) -> Optional[str]:
    """只接受緊鄰「訂位代號」標籤後方的 8 碼數字。"""
    if not text:
        return None

    for match in re.finditer(r"(?:訂位代號|訂位編號|訂位代碼)\s*[:：]?\s*([0-9][0-9\s-]{6,18}[0-9])", text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) == 8:
            return digits
    return None


def has_confirmed_booking(text: str) -> bool:
    return extract_booking_code(text) is not None


def is_sold_out_error(text: str) -> bool:
    normalized = normalize_text(text)
    return any(
        phrase in normalized
        for phrase in [
            "可銷售之座位或優惠座位已額滿",
            "優惠座位已額滿",
            "座位已額滿",
            "請重新操作",
            "改選其他行程",
        ]
    )


def extract_train_no(text: str) -> Optional[str]:
    if not text:
        return None

    for pattern in [
        r"(?:車次|車\s*次|Train\s*No\.?)\D{0,20}(\d{3,4})",
        r"^\s*(\d{3,4})\b",
    ]:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1)
    return None


def validate_booking_config(config: dict) -> tuple[list[str], list[str]]:
    booking = config.get("booking", {})
    critical_missing: list[str] = []
    optional_missing: list[str] = []

    if not (booking.get("idno") or "").strip():
        critical_missing.append("THSRC_IDNO")
    if not (booking.get("phone") or "").strip():
        optional_missing.append("THSRC_PHONE")
    if not (booking.get("email") or "").strip():
        optional_missing.append("THSRC_EMAIL")

    return critical_missing, optional_missing


def is_retryable_round_failure(reason: str) -> bool:
    return reason in {"captcha_failed", "submit_failed", "round_exception", "sold_out"}


def run_preflight_checks() -> None:
    critical_missing, optional_missing = validate_booking_config(CONFIG)
    if optional_missing:
        log(f"提醒：{', '.join(optional_missing)} 尚未設定，可能收不到高鐵簡訊或 Email。")
    if critical_missing:
        missing = ", ".join(critical_missing)
        raise SystemExit(
            f"必要訂位資料未設定：{missing}。\n"
            "請先在 .env 加上並填妥以下欄位：\n"
            "THSRC_IDNO=你的身分證字號或取票識別碼\n"
            "THSRC_PHONE=你的手機號碼\n"
            "THSRC_EMAIL=你的電子郵件\n"
            "不要等選到票後才送出空白取票識別碼。"
        )


def load_proxies(path: str) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]


def send_email(subject: str, html: str):
    if not CONFIG["notify"]["enabled"]:
        return
    conf = CONFIG["notify"]
    smtp = conf["smtp"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = conf.get("subject_prefix", "") + subject
    msg["From"] = conf["mail_from"]
    msg["To"] = ", ".join(conf["mail_to"])
    msg.attach(MIMEText(html, "html", _charset="utf-8"))

    s = smtplib.SMTP(smtp["host"], smtp["port"], timeout=20)
    try:
        if smtp.get("starttls", True):
            s.starttls()
        s.login(smtp["username"], smtp["password"])
        s.sendmail(conf["mail_from"], conf["mail_to"], msg.as_string())
    finally:
        try:
            s.quit()
        except Exception:
            pass


def safe_send_email(subject: str, html: str) -> bool:
    try:
        send_email(subject, html)
        return True
    except Exception as exc:
        log(f"Email 通知失敗，不影響主流程：{exc}")
        return False


def booking_log_dir() -> Path:
    path = Path(__file__).resolve().parent / "booking_logs"
    path.mkdir(exist_ok=True)
    return path


def read_page_text(page) -> str:
    try:
        return normalize_text(page.evaluate("() => document.body ? document.body.innerText : ''"))
    except Exception:
        return ""


def save_page_artifacts(page, prefix: str) -> dict[str, Optional[str]]:
    ts = _now().strftime("%Y%m%d_%H%M%S")
    base = booking_log_dir() / f"{prefix}_{ts}"
    paths: dict[str, Optional[str]] = {"html": None, "text": None, "screenshot": None}

    try:
        html_path = base.with_suffix(".html")
        html_path.write_text(page.content(), encoding="utf-8")
        paths["html"] = str(html_path)
    except Exception as exc:
        log(f"保存 HTML 失敗：{exc}")

    try:
        text_path = base.with_suffix(".txt")
        text_path.write_text(read_page_text(page), encoding="utf-8")
        paths["text"] = str(text_path)
    except Exception as exc:
        log(f"保存頁面文字失敗：{exc}")

    try:
        screenshot_path = base.with_suffix(".png")
        page.screenshot(path=str(screenshot_path), full_page=True)
        paths["screenshot"] = str(screenshot_path)
    except Exception as exc:
        log(f"保存截圖失敗：{exc}")

    return paths


def save_text_artifact(prefix: str, text: str) -> str:
    ts = _now().strftime("%Y%m%d_%H%M%S")
    path = booking_log_dir() / f"{prefix}_{ts}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)

# =============================
#       Playwright helpers
# =============================

def close_consent(page):
    for label in ["我同意", "同意", "我同意，繼續", "同意並繼續"]:
        try:
            page.get_by_role("button", name=label, exact=False).click(timeout=1500)
            human_sleep()
            return
        except Exception:
            pass
    try:
        page.get_by_text("同意", exact=False).first.click(timeout=1500)
        human_sleep()
    except Exception:
        pass


def human_sleep(a: float = 0.15, b: float = 0.45):
    time.sleep(random.uniform(a, b))


def force_hide_mask(page):
    """最佳努力地把殘留遮罩關掉，避免卡在『請稍候』。"""
    try:
        page.evaluate(
            """() => {
                try { hideMaskFrame && hideMaskFrame(); } catch (e) {}
                if (window.$ && $.unblockUI) {
                    try { $.unblockUI(); } catch (e) {}
                }
                for (const sel of ['#divMaskFrame', '#loadingMask', '#BusyBoxDiv']) {
                    const el = document.querySelector(sel);
                    if (el) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.zIndex = '0';
                    }
                }
            }"""
        )
    except Exception:
        pass


def wait_mask_then_clear_if_stuck(page, check_every_ms=400, hard_timeout_ms=16000):
    """
    等待 loading 遮罩消失；若超時則呼叫頁面現成的 hideMaskFrame() 嘗試解除。
    """
    start = time.time()
    while True:
        try:
            ok = page.evaluate(
                """() => {
                    const isHidden = (el) => !el || el.style.display === 'none' || getComputedStyle(el).display === 'none';
                    const a = document.querySelector('#divMaskFrame');
                    const b = document.querySelector('#loadingMask');
                    const c = document.querySelector('#BusyBoxDiv');
                    return isHidden(a) && isHidden(b) && isHidden(c);
                }"""
            )
            if ok:
                return True
        except Exception:
            pass

        if (time.time() - start) * 1000 > hard_timeout_ms:
            log("遮罩疑似卡住，嘗試呼叫 hideMaskFrame() 強制解除")
            try:
                page.evaluate("hideMaskFrame && hideMaskFrame();")
            except Exception:
                pass
            time.sleep(0.6)
            try:
                ok = page.evaluate(
                    """() => {
                        const isHidden = (el) => !el || el.style.display === 'none' || getComputedStyle(el).display === 'none';
                        const a = document.querySelector('#divMaskFrame');
                        const b = document.querySelector('#loadingMask');
                        const c = document.querySelector('#BusyBoxDiv');
                        return isHidden(a) && isHidden(b) && isHidden(c);
                    }"""
                )
                return ok
            except Exception:
                return False

        time.sleep(check_every_ms / 1000.0)


def wait_step2_or_error(page, timeout_ms: int = 15000):
    """
    等待「選擇車次」(Step2) 結果區塊，或錯誤區塊顯示。
    回傳 'step2' / 'error' / 'none'
    """
    start = time.time()
    while (time.time() - start) * 1000 <= timeout_ms:
        try:
            if page.locator("#BookingS2Form_TrainQueryDataViewPanel").first.is_visible(timeout=500):
                return "step2"
        except Exception:
            pass

        try:
            err_block = page.locator("#divErrMSG")
            if err_block.count() > 0 and err_block.first.is_visible(timeout=200):
                return "error"
        except Exception:
            pass

        time.sleep(0.25)

    return "none"


def wait_ajax_idle(page, timeout=20000):
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=timeout)


def read_error_text(page) -> str:
    try:
        if page.locator("#divErrMSG").first.is_visible(timeout=500):
            txt = page.locator("#divErrMSG").inner_text(timeout=500)
            return re.sub(r"\s+", " ", txt).strip()
    except Exception:
        pass
    return ""


class CaptchaSolver:
    def __init__(self):
        self.ocr = ddddocr.DdddOcr()

    def solve_once(self, page) -> str:
        try:
            page.locator("#BookingS1Form_homeCaptcha_reCodeLink").click(timeout=800)
            page.wait_for_timeout(450)
        except Exception:
            pass

        img = page.locator("#BookingS1Form_homeCaptcha_passCode")
        img.wait_for(timeout=6000)
        path = "captcha.png"
        img.screenshot(path=path)
        with open(path, "rb") as f:
            raw = f.read()
        try:
            os.remove(path)
        except Exception:
            pass
        res = self.ocr.classification(raw)
        res = re.sub(r"[^0-9a-zA-Z]", "", res or "")
        return res

    def fill(self, page, text: str):
        sc = page.locator("#securityCode")
        sc.fill(text)
        page.evaluate(
            """() => {
                const el = document.querySelector('#securityCode');
                if (!el) return;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                el.blur();
            }"""
        )


def handle_captcha(page, max_try=6) -> bool:
    solver = CaptchaSolver()
    for i in range(max_try):
        try:
            ans = solver.solve_once(page)
            log(f"OCR 辨識結果: {ans}")
            if not ans:
                continue
            solver.fill(page, ans)
            return True
        except Exception as e:
            log(f"處理驗證碼失敗（{i+1}/{max_try}）：{e}")
    log("超過最大重試次數，無法處理驗證碼")
    return False


def fill_search(page):
    s = CONFIG["search"]
    page.locator('select[name="selectStartStation"]').select_option(label=s["origin"])
    human_sleep()
    page.locator('select[name="selectDestinationStation"]').select_option(label=s["dest"])
    human_sleep()
    yyyy, mm, dd = s["date"].split("-")
    v = f"{yyyy}/{int(mm):02d}/{int(dd):02d}"
    page.evaluate(
        """(val)=>{const el=document.querySelector('#toTimeInputField'); if(!el) return;
        el.value=val; el.setAttribute('value',val);
        el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
        if (window.BookingS1 && BookingS1.typesoftrainCheck) { try{BookingS1.typesoftrainCheck()}catch(e){} }
    }""",
        v,
    )
    human_sleep()
    page.locator('select[name="toTimeTable"]').select_option(label=s["time"])
    human_sleep()
    page.locator('select[name="ticketPanel:rows:0:ticketAmount"]').select_option(value=f"{s['adult']}F")
    human_sleep()
    page.locator('select[name="ticketPanel:rows:4:ticketAmount"]').select_option(value=f"{s['student']}P")
    human_sleep()


def click_search(page):
    page.locator('#SubmitButton').click(no_wait_after=True)


def submit_and_wait_step2(page, max_submit_retries=5):
    for attempt in range(max_submit_retries):
        click_search(page)
        wait_mask_then_clear_if_stuck(page, hard_timeout_ms=18000)
        state = wait_step2_or_error(page, timeout_ms=18000)

        if state == "step2":
            return True
        elif state == "error":
            err = read_error_text(page)
            log(f"提交後出現錯誤：{err or '(無內容)'}")
            if "驗證碼" in err or "錯誤" in err or "請重新輸入" in err:
                log(f"嘗試重新解驗證碼並重送（{attempt+1} / {max_submit_retries}）")
                if not handle_captcha(page):
                    return False
                continue
            else:
                return False
        else:
            log(f"等待結果超時（{attempt+1} / {max_submit_retries}），嘗試再送")
            try:
                captcha_ok = handle_captcha(page)
            except Exception as exc:
                log(f"重新處理驗證碼時發生例外：{exc}")
                captcha_ok = False
            if not captcha_ok:
                log("重新處理驗證碼失敗，跳過本輪。")
                return False
            continue
    return False


def parse_and_pick_discount(page) -> Optional[SelectedTrain]:
    key = CONFIG["search"]["discount_key"]
    page.wait_for_load_state("domcontentloaded")
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=20000)
    if wait_step2_or_error(page, timeout_ms=20000) != "step2":
        return None

    # 找到所有車次列
    items = page.locator("#BookingS2Form_TrainQueryDataViewPanel .result-listing label.result-item")
    n = items.count()
    target_index = -1
    selected_discount = ""
    selected_text = ""
    for i in range(n):
        try:
            lab = items.nth(i)
            disc_txt = lab.locator(".discount").inner_text(timeout=800).strip()
            if key in disc_txt:
                target_index = i
                selected_discount = normalize_text(disc_txt)
                selected_text = normalize_text(lab.inner_text(timeout=1000))
                break
        except Exception:
            continue

    if target_index == -1:
        return None

    row = items.nth(target_index)
    row.click()
    human_sleep()
    # 確認車次
    try:
        page.locator('input.btn-next[value="確認車次"]').click()
    except Exception:
        try:
            page.get_by_role("button", name="確認車次", exact=False).click()
        except Exception:
            pass
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=20000)
    return SelectedTrain(
        train_no=extract_train_no(selected_text),
        discount_text=selected_discount,
        row_text=selected_text,
    )


def wait_for_booking_confirmation(page, selected_train: Optional[SelectedTrain], timeout_ms: int = 30000) -> Optional[BookingEvidence]:
    deadline = time.time() + timeout_ms / 1000.0

    while time.time() < deadline:
        text = read_page_text(page)

        booking_code = extract_booking_code(text)
        if booking_code:
            paths = save_page_artifacts(page, "booking_success")
            return BookingEvidence(
                booking_code=booking_code,
                train_no=(selected_train.train_no if selected_train else extract_train_no(text)),
                page_text=text,
                html_path=paths["html"],
                text_path=paths["text"],
                screenshot_path=paths["screenshot"],
            )

        err = read_error_text(page)
        if err and any(key in err for key in ["錯誤", "失敗", "無法", "逾時", "未完成"]):
            log(f"訂位送出後出現錯誤：{err}")
            break

        time.sleep(0.5)

    paths = save_page_artifacts(page, "booking_unconfirmed")
    log("送出後未取得 8 碼訂位代號，因此不再判定為 booked。")
    if paths.get("text"):
        log(f"已保存未確認頁面文字：{paths['text']}")
    return None


def step3_fill_and_submit(page, selected_train: Optional[SelectedTrain]) -> Optional[BookingEvidence]:
    b = CONFIG["booking"]
    page.wait_for_selector('#BookingS3FormSP', timeout=25000)

    page.locator('#idInputRadio').select_option(value='0')
    page.locator('#idNumber').fill(b['idno'])
    if b.get('phone'):
        page.locator('#mobilePhone').fill(b['phone'])
    if b.get('email'):
        page.locator('#email').fill(b['email'])

    try:
        page.locator('#memberSystemRadio3').check()
    except Exception:
        pass

    page.locator('input[name="agree"]').check()

    page.locator('#isSubmit').scroll_into_view_if_needed()
    human_sleep()
    page.locator('#isSubmit').click()
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=20000)

    # 可能彈窗
    for sel in ['#btn-custom2', '#SubmitPassButton']:
        try:
            page.locator(sel).click(timeout=1500)
        except Exception:
            pass
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=20000)

    return wait_for_booking_confirmation(page, selected_train, timeout_ms=30000)


@contextmanager
def make_context(p, proxy: Optional[str]):
    br = CONFIG["browser"]
    browser = p.chromium.launch(
        headless=br.get("headless", False),
        channel=("msedge" if br.get("use_edge") else None),
        proxy=(proxy and {"server": proxy}) or None,
    )
    user_agent = br.get("force_user_agent") or DEFAULT_EDGE_UA
    ctx = browser.new_context(
        locale="zh-TW",
        timezone_id="Asia/Taipei",
        viewport={"width":1280, "height":900},
        user_agent=user_agent,
        extra_http_headers={"Accept-Language": br.get("accept_language", "zh-TW,zh;q=0.9")},
    )
    ctx.add_init_script(
        """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW','zh','en-US','en'] });
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
            if (originalQuery) {
              window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                  Promise.resolve({ state: Notification.permission }) :
                  originalQuery(parameters)
              );
            }
        """
    )
    try:
        yield ctx
    finally:
        ctx.close(); browser.close()


# =============================
#           Runner
# =============================

def run_once(proxy: Optional[str]) -> Tuple[bool, str, Optional[BookingEvidence]]:
    """回傳 (is_success, reason, evidence)
    reason: booked / no_match / captcha_failed / submit_failed / sold_out / round_exception / confirmation_missing / exception
    evidence: 成功時包含 8 碼訂位代號與本地頁面證據
    """
    with sync_playwright() as p, make_context(p, proxy) as ctx:
        page = ctx.new_page()
        stage = "search"
        try:
            page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            close_consent(page)
            wait_ajax_idle(page, 15000)  # 首屏遮罩先確保關掉

            fill_search(page)
            if not handle_captcha(page):
                return False, 'captcha_failed', None

            log("送出查詢")
            ok_submit = submit_and_wait_step2(page, max_submit_retries=6)
            if not ok_submit:
                return False, 'submit_failed', None

            selected_train = parse_and_pick_discount(page)
            if not selected_train:
                return False, 'no_match', None
            stage = "train_selected"
            if selected_train.train_no:
                log(f"選取車次：{selected_train.train_no}（{CONFIG['search']['discount_key']}）")
            else:
                log(f"已選取符合「{CONFIG['search']['discount_key']}」的車次，但未能解析車次號碼")

            try:
                card_html = page.locator('.ticket-card').first.inner_html(timeout=5000)
            except Exception:
                card_html = None

            stage = "booking_submit"
            evidence = step3_fill_and_submit(page, selected_train)
            if evidence:
                evidence.ticket_html = card_html
                return True, 'booked', evidence
            if is_sold_out_error(read_page_text(page)):
                log("高鐵回覆座位或優惠座位已額滿，跳過本輪。")
                return False, 'sold_out', None
            return False, 'confirmation_missing', None
        except Exception as exc:
            tb = traceback.format_exc()
            log(f"本回合例外：{exc}")
            try:
                paths = save_page_artifacts(page, "round_exception")
                if paths.get("text"):
                    log(f"已保存例外頁面文字：{paths['text']}")
                error_path = save_text_artifact("round_exception_traceback", tb)
                log(f"已保存例外 traceback：{error_path}")
            except Exception:
                pass
            reason = "exception" if stage == "booking_submit" else "round_exception"
            return False, reason, None


def main():
    run_preflight_checks()

    br = CONFIG["browser"]
    proxies = load_proxies(br.get("proxies_file", ""))
    proxy_idx = 0

    until = _until_dt()
    max_rounds = CONFIG["watch"].get("max_rounds")

    round_no = 0
    start_ts = _now()

    while True:
        round_no += 1
        if max_rounds is not None and round_no > max_rounds:
            subject = "已達最大回合，未找到票"
            html = f"<p>從 {start_ts} 起共嘗試 {max_rounds} 回，仍未找到『{CONFIG['search']['discount_key']}』。</p>"
            safe_send_email(subject, html)
            print(subject)
            break

        if until and _now() >= until:
            subject = "已到期，未找到票"
            html = f"<p>監看已到 {until} 結束，未找到『{CONFIG['search']['discount_key']}』。</p>"
            safe_send_email(subject, html)
            print(subject)
            break

        proxy = None
        if proxies:
            proxy = proxies[proxy_idx % len(proxies)]
            proxy_idx += 1

        print(f"== Round {round_no} | proxy={proxy or '-'} ==")
        ok, why, evidence = run_once(proxy)
        print(f"結果：{why}")

        if ok:
            s = CONFIG["search"]
            subject = f"已完成訂位：{s['origin']}→{s['dest']} {s['date']} {s['time']} ({s['discount_key']})"
            print(f"訂位代號：{evidence.booking_code}")
            if evidence.train_no:
                print(f"車次：{evidence.train_no}")
            if evidence.text_path:
                print(f"成功頁面文字已保存：{evidence.text_path}")
            body = [
                f"<h3>已完成訂位</h3>",
                f"<p>訂位代號：<strong>{evidence.booking_code}</strong></p>",
                f"<p>車次：{evidence.train_no or '未解析'}</p>",
                f"<p>{s['origin']} → {s['dest']} | {s['date']} {s['time']} | 折扣: {s['discount_key']}</p>",
                "<hr>",
                evidence.ticket_html or "",
                "<p>請使用訂位代號與取票識別碼到官方通路付款/取票。</p>",
            ]
            safe_send_email(subject, "".join(body))
            break

        if why == "confirmation_missing":
            subject = "訂位狀態未確認，已停止自動重試"
            html = (
                "<p>系統已選到符合條件的車次並送出訂位資料，但沒有在完成頁取得 8 碼訂位代號。</p>"
                "<p>為避免重複訂位，程式已停止自動重試。請先檢查本機 booking_logs 內保存的頁面，"
                "或用官方網站 / T-EX App 查詢訂位狀態。</p>"
            )
            safe_send_email(subject, html)
            print(subject)
            break

        if why == "exception":
            subject = "程式例外，已停止自動重試"
            html = (
                "<p>本回合執行時發生例外。為避免 selector 壞掉時持續重試或重複送出訂位資料，"
                "程式已停止。請檢查 booking_logs 內最新的 round_exception 與 traceback 檔。</p>"
            )
            safe_send_email(subject, html)
            print(subject)
            break

        if is_retryable_round_failure(why):
            log("本輪屬於可重試失敗，等待下一輪。")

        # 未命中 → 等待後重試
        minv = int(CONFIG["watch"]["interval_min"])
        maxv = int(CONFIG["watch"]["interval_max"])
        wait_sec = random.randint(minv, maxv)
        print(f"未命中，{wait_sec} 秒後再試...")
        time.sleep(wait_sec)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Fatal error:", e)
        tb = traceback.format_exc()
        safe_send_email("程式異常終止", f"<pre>{tb}</pre>")
