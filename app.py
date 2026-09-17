from flask import Flask, request, jsonify, render_template
from datetime import datetime, timezone, timedelta
import requests
import os

app = Flask(__name__)

API_TOKEN = os.environ.get("PROM_API_TOKEN", "3850b2862e6b8e1b7f36d8c1bb7ea64ca3c2747f")

ДНІВ_ДО_ППВ_EVOPAY     = 7
ДНІВ_ДО_ППВ_RPAY_PARTS = 2
KYIV_OFFSET = timedelta(hours=3)


def parse_iso(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if "." in s:
            if "+" in s[10:]:
                base, tz = s.rsplit("+", 1)
                tz = "+" + tz
            elif s.count("-") > 2:
                idx = s.rfind("-")
                base, tz = s[:idx], s[idx:]
            else:
                base, tz = s, ""
            if "." in base:
                dt_p, us = base.split(".")
                us = us[:6].ljust(6, "0")
                base = f"{dt_p}.{us}"
            s = base + tz
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)  # завжди в UTC
    except:
        return None


def kyiv(dt_utc):
    """UTC → Київ (+3 год)"""
    return dt_utc + KYIV_OFFSET if dt_utc else None


def fmt(dt):
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def now_utc():
    return datetime.now(timezone.utc)


def get_order_from_prom(order_id):
    try:
        resp = requests.get(
            f"https://my.prom.ua/api/v1/orders/{order_id}",
            headers={
                "Authorization": f"Bearer {API_TOKEN}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json().get("order", {}), None
    except Exception as e:
        return None, str(e)


def analyze_order(order):
    pd       = order.get("payment_data") or {}
    pay_id   = (order.get("payment_option") or {}).get("id")
    pay_name = (order.get("payment_option") or {}).get("name", "—")
    pay_st   = pd.get("status")
    pay_type = pd.get("type")
    st_mod   = pd.get("status_modified")
    ord_st   = order.get("status")
    decl     = (order.get("delivery_provider_data") or {}).get("declaration_number")
    np_st    = (order.get("delivery_provider_data") or {}).get("unified_status")
    canc     = order.get("cancellation")

    # Всі дати в UTC, відображення в Київ (+3)
    dt_mod_utc  = parse_iso(st_mod)       # status_modified UTC
    dt_mod_k    = kyiv(dt_mod_utc)        # status_modified Київ (+3)

    IS_PP = (pay_id == 7586820)
    now   = now_utc()

    result = {
        "order_id":     order.get("id"),
        "client":       f"{order.get('client_first_name','')} {order.get('client_last_name','')}".strip(),
        "status":       order.get("status_name", ord_st),
        "ord_st":       ord_st,
        "pay_name":     pay_name,
        "pay_id":       pay_id,
        "pay_status":   pay_st,
        "pay_type":     pay_type,
        "pay_date":     fmt(dt_mod_k),
        "pay_date_utc": fmt(dt_mod_utc),
        "amount":       order.get("full_price", "—"),
        "declaration":  decl,
        "np_status":    np_st,
        "is_prompay":   IS_PP,
        "timeline":     [],
        "summary":      "",
        "summary_type": "info",
    }

    tl = result["timeline"]

    if not IS_PP:
        result["summary"]      = f"Спосіб оплати '{pay_name}' — не PromPay. Регламент це замовлення не обробляє."
        result["summary_type"] = "warn"
        return result

    # ── Крок 1: Замовлення завантажено в 1С ──────────────────
    dt_created_utc = parse_iso(order.get("date_created"))
    tl.append({
        "date":  fmt(kyiv(dt_created_utc)),
        "type":  "event",
        "title": "Замовлення завантажено в 1С",
        "desc":  "СпособОплаты = PromPay → ТолькоПросмотр = Істина (заблоковано). Потрапило у відбір регламенту (ППВ_PromPay_Создан = Ложь)."
    })

    # ── Крок 2: Зміна статусу оплати ─────────────────────────
    tl.append({
        "date":  fmt(dt_mod_k),
        "type":  "event",
        "title": f"payment_data.status → '{pay_st}'",
        "desc":  (
            f"Тип оплати: {pay_type}<br>"
            f"status_modified: {fmt(dt_mod_utc)} UTC → {fmt(dt_mod_k)} Київ (+3 год)"
        )
    })

    # ── Крок 3: Логіка регламенту ─────────────────────────────

    if pay_st == "paid_out":
        # ДатаППВ = status_modified UTC + 3 год (Київ)
        dt_ppv_k = dt_mod_k  # вже Київ

        tl.append({
            "date":  fmt(dt_ppv_k),
            "type":  "ok",
            "title": "МАЛО СТВОРИТИСЬ ППВ (або вже існує з попереднього кроку)",
            "desc":  (
                f"Дата ППВ = status_modified + 3 год (UTC→Київ) = {fmt(dt_ppv_k)}.<br>"
                f"Регламент перевіряє: чи вже є проведене ППВ?<br>"
                f"• ППВ є (створено раніше по paid + N днів) → нічого не робимо ✅<br>"
                f"• ППВ немає (paid_out прийшов раніше N днів) → створюємо датою {fmt(dt_ppv_k)} ✅<br>"
                f"<br>"
                f"⚠️ Увага: дата коли був статус 'paid' недоступна з API Prom — "
                f"API зберігає тільки поточний статус. Для точної ретроспективи потрібен "
                f"реквізит ДатаОплатиPromPay з 1С."
            )
        })
        result["summary"]      = f"paid_out отримано {fmt(dt_ppv_k)}. ППВ мало створитись цією датою (або раніше по paid+N днів). Перевірте в 1С."
        result["summary_type"] = "ok"

    elif pay_st == "paid":
        days  = ДНІВ_ДО_ППВ_RPAY_PARTS if pay_type == "rpay_parts" else ДНІВ_ДО_ППВ_EVOPAY
        const = "ДнейДоППВ_PromPay_Paid_RPayParts" if pay_type == "rpay_parts" else "ДнейДоППВ_PromPay_Paid"

        # ДатаОплати Київ = status_modified UTC + 3 год
        # ДатаППВ Київ    = ДатаОплати Київ + N днів
        # Порівняння:      ДатаППВ UTC = status_modified UTC + N днів
        dt_ppv_utc = dt_mod_utc + timedelta(days=days) if dt_mod_utc else None
        dt_ppv_k   = kyiv(dt_ppv_utc)
        past       = (now >= dt_ppv_utc) if dt_ppv_utc else False

        if past:
            delta = now - dt_ppv_utc
            d, h  = delta.days, delta.seconds // 3600
            tl.append({
                "date":  fmt(dt_ppv_k),
                "type":  "ok",
                "title": f"МАЛО СТВОРИТИСЬ ППВ (оплата + {days} днів)",
                "desc":  (
                    f"Константа {const} = {days} днів.<br>"
                    f"Дата оплати (status_modified + 3 год): {fmt(dt_mod_k)} Київ.<br>"
                    f"Дата ППВ: {fmt(dt_mod_k)} + {days} дн. = {fmt(dt_ppv_k)} Київ.<br>"
                    f"Дата вже минула {d} дн. {h} год. тому.<br>"
                    f"Сума: ПолучитьСуммуОстаткаОплатыПоЗаказу(КонецДня({fmt(dt_ppv_k)})).<br>"
                    f"Якщо після цього прийде paid_out → ППВ вже є → нічого не робимо."
                )
            })
            result["summary"]      = f"ППВ мало створитись {fmt(dt_ppv_k)} (paid + {days} днів). Перевірте в 1С чи є проведене ППВ."
            result["summary_type"] = "ok"
        else:
            if dt_ppv_utc:
                delta = dt_ppv_utc - now
                d, h  = delta.days, delta.seconds // 3600
                tl.append({
                    "date":  fmt(dt_ppv_k),
                    "type":  "wait",
                    "title": f"ППВ ЩЕ НЕ МАЄ СТВОРЮВАТИСЬ — залишилось {d} дн. {h} год.",
                    "desc":  (
                        f"Константа {const} = {days} днів.<br>"
                        f"Дата оплати (status_modified + 3 год): {fmt(dt_mod_k)} Київ.<br>"
                        f"Дата ППВ: {fmt(dt_mod_k)} + {days} дн. = {fmt(dt_ppv_k)} Київ.<br>"
                        f"Замовлення заблоковано (ТолькоПросмотр = Істина).<br>"
                        f"Регламент кожні 15 хв перевіряє — як настане {fmt(dt_ppv_k)}, створить ППВ.<br>"
                        f"Або якщо раніше прийде paid_out → одразу створить ППВ по даті paid_out + 3 год."
                    )
                })
                result["summary"]      = f"ППВ буде створено {fmt(dt_ppv_k)} — дата ще не настала. Або раніше якщо прийде paid_out."
                result["summary_type"] = "wait"

    elif pay_st == "unpaid":
        tl.append({
            "date":  "—",
            "type":  "wait",
            "title": "Клієнт ще не сплатив — регламент нічого не робить",
            "desc":  "Замовлення заблоковано (ТолькоПросмотр = Істина). Очікуємо оплати або закінчення терміну (expired)."
        })
        result["summary"]      = "ППВ не створюється. Очікуємо оплати клієнта."
        result["summary_type"] = "wait"

    elif pay_st == "expired":
        tl.append({
            "date":  fmt(dt_mod_k),
            "type":  "err",
            "title": "Час оплати вийшов — ППВ НЕ створюється",
            "desc":  (
                f"status_modified + 3 год = {fmt(dt_mod_k)} Київ.<br>"
                f"Регламент мав встановити ППВ_PromPay_Создан = Істина і розблокувати замовлення.<br>"
                f"Замовлення розблоковується (ТолькоПросмотр = Ложь).<br>"
                f"Менеджер має вручну обрати новий спосіб оплати."
            )
        })
        result["summary"]      = "ППВ не створюється (expired). Замовлення мало розблокуватись — менеджер обирає спосіб оплати."
        result["summary_type"] = "err"

    elif pay_st == "refunded":
        tl.append({
            "date":  fmt(dt_mod_k),
            "type":  "warn",
            "title": "Повернення — регламент шукав ППВ і мав створити ППІ",
            "desc":  (
                f"status_modified + 3 год = {fmt(dt_mod_k)} Київ.<br>"
                f"Регламент шукає існуюче ППВ по замовленню.<br>"
                f"Якщо ППВ знайдено → мало створитись ППІ для повернення коштів.<br>"
                f"ППВ_PromPay_Создан = Істина (знято з моніторингу)."
            )
        })
        result["summary"]      = "Повернення: перевірте чи є ППВ і ППІ в 1С."
        result["summary_type"] = "warn"

    else:
        result["summary"]      = f"Невідомий статус оплати: '{pay_st}'"
        result["summary_type"] = "err"

    # Скасування
    if canc:
        initiator = canc.get("initiator")
        title_c   = canc.get("title", "—")
        if initiator == "user":
            tl.append({
                "date":  "—",
                "type":  "warn",
                "title": f"Скасовано покупцем: «{title_c}»",
                "desc":  "Ініціатор — покупець. Статус 'canceled' НЕ передається в Prom з 1С."
            })
        else:
            tl.append({
                "date":  "—",
                "type":  "ok",
                "title": f"Скасовано компанією: «{title_c}»",
                "desc":  "Статус 'canceled' МАВ бути переданий в Prom через ПередатьОтменуЗаказаВPromAPI."
            })

    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze", methods=["GET"])
def api_analyze():
    order_id = request.args.get("order_id", "").strip()
    if not order_id.isdigit():
        return jsonify({"error": "Введіть числовий ID замовлення"}), 400

    order, err = get_order_from_prom(order_id)
    if err:
        return jsonify({"error": f"Помилка API Prom: {err}"}), 500
    if not order:
        return jsonify({"error": "Замовлення не знайдено"}), 404

    result = analyze_order(order)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
