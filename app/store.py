from app.util import E, VE, is_id, is_int, now_iso, same
from app.sign import sign
from app.pdf import make_pdf

PRICE = 450000
BASE_VARIANTS = {"var-jersey-001-M", "var-jersey-001-L"}

def variants(state):
    return BASE_VARIANTS | set(state["stock"].keys())

def fmt_pkr(minor):
    return f"PKR {minor // 100}.{minor % 100:02d}"

def checkout(state, lock, body):
    if not isinstance(body, dict):
        return VE("body")
    bad = []
    oid, cid = body.get("order_id"), body.get("customer_id")
    if not is_id(oid): bad.append("order_id")
    if not isinstance(cid, str) or not cid or len(cid) > 200: bad.append("customer_id")
    items = body.get("items")
    if not isinstance(items, list) or not (1 <= len(items) <= 10):
        bad.append("items")
    else:
        seen = set()
        for it in items:
            if (not isinstance(it, dict) or not isinstance(it.get("variant_id"), str)
                    or not is_int(it.get("quantity")) or not (1 <= it["quantity"] <= 10)
                    or it["variant_id"] in seen):
                bad.append("items"); break
            seen.add(it["variant_id"])
    dc = body.get("discount_code")
    if dc is not None and not isinstance(dc, str): bad.append("discount_code")
    pay = body.get("payment")
    if not isinstance(pay, dict) or not isinstance(pay.get("token"), str): bad.append("payment")
    if bad:
        return VE(*bad)

    with lock:
        prev = state["orders"].get(oid)
        if prev:
            if same(prev["body"], body):
                st, resp = prev["result"]
                return (200 if st == 201 else st), resp
            return E(409, "DUPLICATE_ORDER_ID", f"Order {oid} already used", {"order_id": oid})

        def done(res):
            state["orders"][oid] = {"body": body, "result": res}
            return res

        for it in items:
            if it["variant_id"] not in variants(state):
                return done(E(404, "VARIANT_NOT_FOUND", "Unknown variant", {"variant_id": it["variant_id"]}))
        if dc is not None and dc != "WELCOME10":
            return done(E(400, "INVALID_DISCOUNT_CODE", "Unknown discount code", {"discount_code": dc}))

        stock = state["stock"]
        for it in items:
            if stock.get(it["variant_id"], 0) < it["quantity"]:
                return done(E(409, "OUT_OF_STOCK", f"Variant {it['variant_id']} has no stock left.",
                              {"variant_id": it["variant_id"]}))
        # reserve
        for it in items:
            stock[it["variant_id"]] -= it["quantity"]
            state["reserved"][it["variant_id"]] = state["reserved"].get(it["variant_id"], 0) + it["quantity"]

        def release():
            for it in items:
                stock[it["variant_id"]] += it["quantity"]
                state["reserved"][it["variant_id"]] -= it["quantity"]

        tok = pay["token"]
        state["counter"] += 1
        n = state["counter"]
        if tok == "tok_declined":
            release()
            state["queue"].append({"type": "payment_declined", "order_id": oid})
            return done(E(402, "PAYMENT_DECLINED", "Payment was declined", {"order_id": oid}))
        if tok == "tok_capture_fail":
            release()
            state["queue"].append({"type": "payment_capture_failed", "order_id": oid})
            return done(E(402, "PAYMENT_CAPTURE_FAILED", "Payment capture failed", {"order_id": oid}))
        if tok != "tok_success":
            release()
            return done(E(402, "PAYMENT_DECLINED", "Unknown payment token", {"order_id": oid}))
        # commit
        for it in items:
            state["reserved"][it["variant_id"]] -= it["quantity"]

        lines = [{"variant_id": it["variant_id"], "quantity": it["quantity"], "unit_price_minor": PRICE} for it in items]
        subtotal = sum(PRICE * it["quantity"] for it in items)
        discount = subtotal * 10 // 100 if dc == "WELCOME10" else 0
        total = subtotal - discount
        payload = {"order_id": oid, "customer_id": cid, "items": lines,
                   "subtotal_minor": subtotal, "discount_minor": discount, "total_minor": total,
                   "currency": "PKR", "paid_at": now_iso()}
        pdf_lines = ["NaSCon Store Receipt", f"Order: {oid}", f"Customer: {cid}"]
        pdf_lines += [f"{l['variant_id']} x{l['quantity']}  {fmt_pkr(l['unit_price_minor'])}" for l in lines]
        pdf_lines += [f"Subtotal: {fmt_pkr(subtotal)}", f"Discount: {fmt_pkr(discount)}", f"Total: {fmt_pkr(total)}"]
        resp = {"order_id": oid, "status": "paid", "currency": "PKR", "items": lines,
                "subtotal_minor": subtotal, "discount_minor": discount, "total_minor": total,
                "payment": {"authorization_id": f"auth_{n}", "capture_id": f"cap_{n}", "state": "captured"},
                "receipt": {"payload": payload, "signature": sign(oid, payload), "pdf_base64": make_pdf(pdf_lines)}}
        state["queue"].append({"type": "order_paid", "order_id": oid})
        return done((201, resp))

def inventory(state, lock, variant_id):
    with lock:
        if variant_id not in variants(state):
            return E(404, "VARIANT_NOT_FOUND", "Unknown variant", {"variant_id": variant_id})
        return 200, {"variant_id": variant_id, "stock": state["stock"].get(variant_id, 0),
                     "reserved": state["reserved"].get(variant_id, 0)}
