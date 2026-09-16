from flask import Flask, request, jsonify, render_template
from datetime import datetime, timezone, timedelta
import requests
import os

app = Flask(__name__)

API_TOKEN = os.environ.get("PROM_API_TOKEN", "3850b2862e6b8e1b7f36d8c1bb7ea64ca3c2747f")

# Константи
ДНІВ_ДО_ППВ_EVOPAY     = 7
ДНІВ_ДО_ППВ_RPAY_PARTS = 2


def parse_iso(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        if "." in s:
            parts = s.split("+")
            if len(parts) == 2:
                base, tz = parts[0], "+" + parts[1]
            elif "-" in s[19:]:
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
        return dt
    except:
        return None


def to_kyiv(dt):
    return dt + timedelta(hours=3) if dt else None


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

    dt_mod      = parse_iso(st_mod)
    dt_mod_kyiv = to_kyiv(dt_mod)
    IS_PP       = (pay_id == 7586820)

    result = {
        "order_id":    order.get("id"),
        "client":      f"{order.get('client_first_name','')} {order.get('client_last_name','')}".strip(),
        "status":      order.get("status_name", ord_st),
        "pay_name":    pay_name,
        "pay_id":      pay_id,
        "pay_status":  pay_st,
        "pay_type":    pay_type,
        "pay_date":    fmt(dt_mod_kyiv),
        "amount":      order.get("full_price", "—"),
        "declaration": decl,
        "np_status":   np_st,
        "is_prompay":  IS_PP,
        "timeline":    [],
        "summary":     "",
        "summary_type": "info",
    }

    tl = result["timeline"]

    if not IS_PP:
        result["summary"] = f"Спосіб оплати '{pay_name}' — не PromPay. Регламент це замовлення не обробляє."
        result["summary_type"] = "warn"
        return result

    # Крок 1 — завантаження в 1С
    dt_created      = parse_iso(order.get("date_created"))
    dt_created_kyiv = to_kyiv(dt_created)
    tl.append({
        "date":  fmt(dt_created_kyiv),
        "type":  "ok",
        "title": "Замовлення завантажено в 1С",
        "desc":  "СпособОплаты = PromPay → ТолькоПросмотр = Істина (заблоковано). Потрапило у відбір регламенту (ППВ_PromPay_Создан = Ложь)."
    })

    # Крок 2 — зміна статусу оплати
    if dt_mod_kyiv:
        tl.append({
            "date":  fmt(dt_mod_kyiv),
            "type":  "event",
            "title": f"payment_data.status → '{pay_st}'",
            "desc":  f"Тип оплати: {pay_type}"
        })

    # Крок 3 — дії регламенту
    if pay_st == "paid_out":
        dt_ppv_kyiv = dt_mod_kyiv
        tl.append({
            "date":  fmt(dt_ppv_kyiv),
            "type":  "ok",
            "title": "МАЛО СТВОРИТИСЬ ППВ",
            "desc":  (
                f"Розрахунок: status_modified + 3 год (UTC→Київ) = {fmt(dt_ppv_kyiv)}. "
                f"Сума: ПолучитьСуммуОстаткаОплатыПоЗаказу(КонецДня({fmt(dt_ppv_kyiv)})). "
                f"Після запису: ППВ_PromPay_Создан = Істина → замовлення знято з моніторингу."
            )
        })
        result["summary"]      = f"ППВ мало створитись {fmt(dt_ppv_kyiv)}. Перевірте в 1С чи є проведене ППВ."
        result["summary_type"] = "ok"

    elif pay_st == "paid":
        days  = ДНІВ_ДО_ППВ_RPAY_PARTS if pay_type == "rpay_parts" else ДНІВ_ДО_ППВ_EVOPAY
        const = "ДнейДоППВ_PromPay_Paid_RPayParts" if pay_type == "rpay_parts" else "ДнейДоППВ_PromPay_Paid"

        if dt_mod:
            dt_ppv      = dt_mod + timedelta(days=days)
            dt_ppv_kyiv = to_kyiv(dt_ppv)
            past        = now_utc() >= dt_ppv
            delta       = dt_ppv - now_utc()
            d, h        = max(delta.days, 0), max(delta.seconds // 3600, 0)

            if past:
                tl.append({
                    "date":  fmt(dt_ppv_kyiv),
                    "type":  "ok",
                    "title": f"МАЛО СТВОРИТИСЬ ППВ (оплата + {days} днів)",
                    "desc":  (
                        f"Константа {const} = {days} днів. "
                        f"Дата: {fmt(dt_mod_kyiv)} + {days} дн. = {fmt(dt_ppv_kyiv)}. "
                        f"Сума: ПолучитьСуммуОстаткаОплатыПоЗаказу(КонецДня({fmt(dt_ppv_kyiv)})). "
                        f"Після запису: ППВ_PromPay_Создан = Істина."
                    )
                })
                result["summary"]      = f"ППВ мало створитись {fmt(dt_ppv_kyiv)}. Перевірте в 1С чи є проведене ППВ."
                result["summary_type"] = "ok"
            else:
                tl.append({
                    "date":  fmt(dt_ppv_kyiv),
                    "type":  "wait",
                    "title": f"ППВ ЩЕ НЕ МАЛО СТВОРЮВАТИСЬ — залишилось {d} дн. {h} год.",
                    "desc":  (
                        f"Константа {const} = {days} днів. "
                        f"Дата ППВ: {fmt(dt_ppv_kyiv)}. "
                        f"Замовлення залишається заблокованим (ТолькоПросмотр = Істина)."
                    )
                })
                result["summary"]      = f"ППВ буде створено {fmt(dt_ppv_kyiv)} — дата ще не настала."
                result["summary_type"] = "wait"
        else:
            result["summary"]      = "Дата status_modified невідома — неможливо розрахувати дату ППВ."
            result["summary_type"] = "err"

    elif pay_st == "unpaid":
        tl.append({
            "date":  "—",
            "type":  "wait",
            "title": "Клієнт не сплатив — регламент нічого не робив",
            "desc":  "Замовлення залишалось заблокованим (ТолькоПросмотр = Істина). Очікування оплати або expired."
        })
        result["summary"]      = "ППВ не мало створюватись. Замовлення заблоковано."
        result["summary_type"] = "wait"

    elif pay_st == "expired":
        tl.append({
            "date":  fmt(dt_mod_kyiv),
            "type":  "err",
            "title": "Час оплати вийшов — ППВ НЕ створюється",
            "desc":  "Регламент мав встановити ППВ_PromPay_Создан = Істина і розблокувати замовлення (ТолькоПросмотр = Ложь). Менеджер обирає новий спосіб оплати."
        })
        result["summary"]      = "ППВ не створюється (expired). Замовлення мало розблокуватись."
        result["summary_type"] = "err"

    elif pay_st == "refunded":
        tl.append({
            "date":  fmt(dt_mod_kyiv),
            "type":  "warn",
            "title": "Повернення — регламент шукав ППВ і мав створити ППІ",
            "desc":  "Якщо ППВ знайдено → мало створитись ППІ (Платіжне Поручення Вихідне) для повернення. ППВ_PromPay_Создан = Істина."
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
                "desc":  "Ініціатор — покупець. Статус 'canceled' НЕ передається в Prom з 1С (лише якщо ініціатор — компанія)."
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
