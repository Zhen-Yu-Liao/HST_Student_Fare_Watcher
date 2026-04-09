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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Optional, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import ddddocr
from dotenv import load_dotenv

# =============================
#            CONFIG
# =============================
CONFIG = {
    "search": {
        "origin": "新竹",
        "dest": "台南",
        "date": "2026-04-16",   # YYYY-MM-DD
        "time": "08:00",        # 下拉選項顯示文字
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
        "interval_min": 10,
        "interval_max": 30,
        # 到期時間 (Asia/Taipei)，到期仍未命中會寄信並結束；留空代表無期限
        "until": "2026-04-16 01:50",
        # 安全網: 最多嘗試回合數 (None 代表不限制)
        "max_rounds": None,
    },
    "browser": {
        "use_edge": True,       # True 則使用 Edge channel
        "headless": False,      # 改 True 可無頭，瀏覽器就不會跳出來了，但開發測試時建議先用 False 看看流程跑得順不順
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
                handle_captcha(page)
            except Exception:
                pass
            continue
    return False


def parse_and_pick_discount(page) -> bool:
    key = CONFIG["search"]["discount_key"]
    page.wait_for_load_state("domcontentloaded")
    wait_mask_then_clear_if_stuck(page, hard_timeout_ms=20000)
    if wait_step2_or_error(page, timeout_ms=20000) != "step2":
        return False

    # 找到所有車次列
    items = page.locator("#BookingS2Form_TrainQueryDataViewPanel .result-listing label.result-item")
    n = items.count()
    target_index = -1
    for i in range(n):
        try:
            lab = items.nth(i)
            disc_txt = lab.locator(".discount").inner_text(timeout=800).strip()
            if key in disc_txt:
                target_index = i
                break
        except Exception:
            continue

    if target_index == -1:
        return False

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
    return True


def step3_fill_and_submit(page) -> bool:
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

    try:
        page.wait_for_function(
            """() => /完成訂位|訂位代號|已完成/.test(document.body.innerText)""",
            timeout=25000,
        )
        return True
    except PWTimeoutError:
        return False


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

def run_once(proxy: Optional[str]) -> Tuple[bool, str, Optional[str]]:
    """回傳 (is_success, reason, ticket_html)
    reason: booked / no_match / captcha_failed / submit_failed / exception
    ticket_html: 成功時回傳 Step3 摘要 HTML 片段以供寄信 (容錯: 可能為 None)
    """
    with sync_playwright() as p, make_context(p, proxy) as ctx:
        page = ctx.new_page()
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

            picked = parse_and_pick_discount(page)
            if not picked:
                return False, 'no_match', None

            try:
                card_html = page.locator('.ticket-card').first.inner_html(timeout=5000)
            except Exception:
                card_html = None

            ok = step3_fill_and_submit(page)
            return (True, 'booked', card_html) if ok else (False, 'submit_failed', card_html)
        except Exception:
            return False, 'exception', None


def main():
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
            send_email(subject, html)
            print(subject)
            break

        if until and _now() >= until:
            subject = "已到期，未找到票"
            html = f"<p>監看已到 {until} 結束，未找到『{CONFIG['search']['discount_key']}』。</p>"
            send_email(subject, html)
            print(subject)
            break

        proxy = None
        if proxies:
            proxy = proxies[proxy_idx % len(proxies)]
            proxy_idx += 1

        print(f"== Round {round_no} | proxy={proxy or '-'} ==")
        ok, why, ticket_html = run_once(proxy)
        print(f"結果：{why}")

        if ok:
            s = CONFIG["search"]
            subject = f"命中並嘗試完成訂位：{s['origin']}→{s['dest']} {s['date']} {s['time']} ({s['discount_key']})"
            body = [
                f"<h3>已觸發訂位流程</h3>",
                f"<p>{s['origin']} → {s['dest']} | {s['date']} {s['time']} | 折扣: {s['discount_key']}</p>",
                "<hr>",
                ticket_html or "",
                "<p>請自行登入官網或留意 Email/SMS 確認最終訂位狀態。</p>",
            ]
            send_email(subject, "".join(body))
            break

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
        try:
            send_email("程式異常終止", f"<pre>{tb}</pre>")
        except Exception:
            pass
