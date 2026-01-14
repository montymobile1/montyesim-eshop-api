

from fastapi import APIRouter, Path, Request, Query
from fastapi.responses import HTMLResponse
from starlette import status

router = APIRouter()


@router.get("/pay/v1/{order_id}")
def pay(order_id: str = Path(description="order id ")):
    content = f"""

    <style>
        #my-form {{
            display: none;
        }}
    </style>
    <form action="https://google.com" id="my-form">
        <button type="submit" id="submit-btn">Submit</button>
        <p> order id: {order_id}</p>
    </form>

    <script>
        setTimeout(()=>{{

        var el = document.getElementById("submit-btn")
        el.click();
        }},200)
    </script>

    """
    return HTMLResponse(status_code=status.HTTP_200_OK, content=content)
#
# @router.get("/pay/v2/{order_id}", response_class=HTMLResponse, name="pay_page/v2")
# def pay(request: Request, order_id: str = Path(description="order id")):
#     base_url = str(request.url_for("pay_result/v2", order_id=order_id))
#
#     success_url = f"{base_url}?result_status=success"
#     failed_url  = f"{base_url}?result_status=failed"
#
#     content = f"""
#     <html>
#       <body>
#         <h2>Choose Result</h2>
#         <p>Order ID: {order_id}</p>
#
#         <button onclick="window.location.href='{success_url}'">
#           Success
#         </button>
#
#         <button onclick="window.location.href='{failed_url}'">
#           Failed
#         </button>
#       </body>
#     </html>
#     """
#     return HTMLResponse(
#         status_code=status.HTTP_200_OK,
#         content=content
#     )
#
#
# @router.get("/pay/v2/result/{order_id}", response_class=HTMLResponse, name="pay_result")
# def pay_result(
#     order_id: str,
#     result_status: str = Query(...)
# ):
#     ok = result_status.lower() == "success"
#     title = "Success ✅" if ok else "Failed ❌"
#
#     content = f"""
#     <html>
#       <body>
#         <h2>{title}</h2>
#         <p>Order ID: {order_id}</p>
#       </body>
#     </html>
#     """
#     return HTMLResponse(
#         status_code=status.HTTP_200_OK,
#         content=content
#     )

@router.get("/pay/{order_id}", response_class=HTMLResponse, name="pay_page")
def pay(request: Request, order_id: str = Path(description="order id")):
    base_url = str(request.url_for("pay_result", order_id=order_id))

    success_url = f"{base_url}?result_status=success"
    failed_url  = f"{base_url}?result_status=failed"

    content = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Payment Result</title>

      <style>
        :root {{
          --bg1: #0f172a;
          --bg2: #111827;
          --card: rgba(255, 255, 255, 0.08);
          --border: rgba(255, 255, 255, 0.14);
          --text: rgba(255, 255, 255, 0.92);
          --muted: rgba(255, 255, 255, 0.70);
          --shadow: 0 20px 60px rgba(0,0,0,0.45);
          --radius: 18px;

          --success1: #16a34a;
          --success2: #22c55e;

          --fail1: #dc2626;
          --fail2: #ef4444;
        }}

        * {{ box-sizing: border-box; }}
        body {{
          margin: 0;
          min-height: 100vh;
          font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
          color: var(--text);
          display: grid;
          place-items: center;
          background:
            radial-gradient(1200px 600px at 20% 20%, rgba(99,102,241,0.35), transparent 55%),
            radial-gradient(1000px 500px at 80% 30%, rgba(34,197,94,0.25), transparent 55%),
            radial-gradient(900px 500px at 50% 90%, rgba(239,68,68,0.18), transparent 60%),
            linear-gradient(135deg, var(--bg1), var(--bg2));
          padding: 18px;
        }}

        .card {{
          width: min(680px, 100%);
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: var(--radius);
          box-shadow: var(--shadow);
          padding: 24px;
          backdrop-filter: blur(10px);
        }}

        .header {{
          text-align: center;
          margin-bottom: 16px;
        }}

        .title {{
          font-size: 22px;
          font-weight: 800;
          letter-spacing: 0.2px;
          margin: 0;
        }}

        .subtitle {{
          margin: 10px 0 0;
          color: var(--muted);
          font-size: 14px;
        }}

        .pill {{
          display: inline-flex;
          align-items: center;
          gap: 8px;
          margin-top: 14px;
          padding: 10px 12px;
          border-radius: 999px;
          border: 1px solid var(--border);
          background: rgba(0,0,0,0.18);
          color: var(--muted);
          font-size: 13px;
        }}

        .orderId {{
          color: var(--text);
          font-weight: 700;
          letter-spacing: 0.3px;
        }}

        .grid {{
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
          margin-top: 18px;
        }}

        @media (max-width: 520px) {{
          .grid {{ grid-template-columns: 1fr; }}
        }}

        .btn {{
          border: 0;
          width: 100%;
          padding: 14px 16px;
          border-radius: 14px;
          cursor: pointer;
          font-weight: 800;
          font-size: 15px;
          color: white;
          transition: transform 0.08s ease, filter 0.18s ease, box-shadow 0.18s ease;
          box-shadow: 0 12px 30px rgba(0,0,0,0.25);
        }}
        .btn:active {{
          transform: translateY(1px) scale(0.99);
        }}
        .btn:hover {{
          filter: brightness(1.06);
        }}

        .btn-success {{
          background: linear-gradient(135deg, var(--success1), var(--success2));
        }}
        .btn-failed {{
          background: linear-gradient(135deg, var(--fail1), var(--fail2));
        }}

        .hint {{
          margin-top: 16px;
          text-align: center;
          color: var(--muted);
          font-size: 12.5px;
        }}
      </style>
    </head>

    <body>
      <div class="card">
        <div class="header">
          <h1 class="title">Choose Payment Result</h1>
          <p class="subtitle">Simulation page for testing success / failed flow</p>

          <div class="pill">
            <span>Order ID:</span>
            <span class="orderId">{order_id}</span>
          </div>
        </div>

        <div class="grid">
          <button class="btn btn-success" onclick="window.location.href='{success_url}'">
            ✅ Success
          </button>

          <button class="btn btn-failed" onclick="window.location.href='{failed_url}'">
            ❌ Failed
          </button>
        </div>

        <div class="hint">Tip: This page is centered and works with any router prefix automatically.</div>
      </div>
    </body>
    </html>
    """

    return HTMLResponse(status_code=http_status.HTTP_200_OK, content=content)


from fastapi import Request, Query
from fastapi.responses import HTMLResponse
from starlette import status as http_status

@router.get("/pay/result/{order_id}", response_class=HTMLResponse, name="pay_result")
def pay_result(request: Request, order_id: str, result_status: str = Query(...)):
    is_success = result_status.lower() == "success"

    title = "Payment Success" if is_success else "Payment Failed"
    subtitle = "Your payment was completed successfully" if is_success else "Your payment could not be processed"
    emoji = "✅" if is_success else "❌"

    accent_1 = "#16a34a" if is_success else "#dc2626"
    accent_2 = "#22c55e" if is_success else "#ef4444"
    glow = "rgba(34,197,94,0.25)" if is_success else "rgba(239,68,68,0.25)"

    back_url = str(request.url_for("pay_page", order_id=order_id))

    content = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{title}</title>

      <style>
        :root {{
          --bg1: #0f172a;
          --bg2: #111827;
          --card: rgba(255,255,255,0.08);
          --border: rgba(255,255,255,0.14);
          --text: rgba(255,255,255,0.92);
          --muted: rgba(255,255,255,0.72);
          --shadow: 0 20px 60px rgba(0,0,0,0.45);
          --radius: 18px;
        }}

        * {{ box-sizing: border-box; }}
        body {{
          margin: 0;
          min-height: 100vh;
          font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;
          display: grid;
          place-items: center;
          padding: 18px;
          color: var(--text);
          background:
            radial-gradient(1000px 500px at 50% 30%, {glow}, transparent 60%),
            linear-gradient(135deg, var(--bg1), var(--bg2));
        }}

        .card {{
          width: min(680px, 100%);
          background: var(--card);
          border: 1px solid var(--border);
          border-radius: var(--radius);
          box-shadow: var(--shadow);
          padding: 28px 22px;
          text-align: center;
          backdrop-filter: blur(10px);
        }}

        .icon {{
          font-size: 52px;
          margin-bottom: 8px;
        }}

        h1 {{
          margin: 6px 0 0;
          font-size: 26px;
          font-weight: 900;
          background: linear-gradient(135deg, {accent_1}, {accent_2});
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
        }}

        .subtitle {{
          margin-top: 10px;
          color: var(--muted);
          font-size: 15px;
        }}

        .order {{
          margin: 18px auto 0;
          padding: 12px 16px;
          border-radius: 14px;
          border: 1px solid var(--border);
          background: rgba(0,0,0,0.18);
          display: inline-block;
        }}

        .order b {{
          letter-spacing: 0.3px;
        }}

        .actions {{
          margin-top: 22px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
        }}

        @media (max-width: 520px) {{
          .actions {{ grid-template-columns: 1fr; }}
        }}

        .btn {{
          border: 0;
          padding: 14px 16px;
          border-radius: 14px;
          cursor: pointer;
          font-weight: 900;
          font-size: 15px;
          color: white;
          transition: transform .08s ease, filter .18s ease;
          box-shadow: 0 12px 30px rgba(0,0,0,.25);
        }}

        .btn:active {{ transform: scale(.98); }}
        .btn:hover {{ filter: brightness(1.06); }}

        .btn-primary {{
          background: linear-gradient(135deg, {accent_1}, {accent_2});
        }}

        .btn-secondary {{
          background: rgba(255,255,255,0.12);
          border: 1px solid var(--border);
        }}
      </style>
    </head>

    <body>
      <div class="card">
        <div class="icon">{emoji}</div>
        <h1>{title}</h1>
        <div class="subtitle">{subtitle}</div>

        <div class="order">
          Order ID: <b>{order_id}</b>
        </div>

        <div class="actions">
          <button class="btn btn-primary" onclick="window.location.href='{back_url}'">
            🔁 Back
          </button>
          <button class="btn btn-secondary" onclick="window.close()">
            ✖ Close
          </button>
        </div>
      </div>
    </body>
    </html>
    """

    return HTMLResponse(status_code=http_status.HTTP_200_OK, content=content)
