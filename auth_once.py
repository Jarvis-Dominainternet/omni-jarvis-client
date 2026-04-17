"""Script de un solo uso para autenticar y guardar el session_token en config.json."""
import asyncio, json, pathlib, websockets

CONFIG   = pathlib.Path(__file__).parent / "config.json"
EMAIL    = "sistema@dominainternet.com"
PASSWORD = "Santi_negro0000"

async def do_auth():
    cfg = json.loads(CONFIG.read_text())
    uri = f"ws://{cfg['server_ip']}:{cfg['server_port']}/stream"
    print(f"Conectando a {uri} ...")

    # ping_interval=None desactiva el keepalive para que no corte mientras escribes
    async with websockets.connect(uri, ping_interval=None, max_size=5*1024*1024) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        print("Servidor:", msg)
        if msg.get("type") == "status":
            print("El servidor no requiere auth.")
            return

        # enviar credenciales
        await ws.send(json.dumps({"type": "auth_init", "email": EMAIL, "password": PASSWORD}))
        raw  = await asyncio.wait_for(ws.recv(), timeout=15)
        resp = json.loads(raw)
        print("Respuesta:", resp.get("type"), "—", resp.get("message", ""))

        if resp.get("type") == "auth_ok":
            _save_token(cfg, resp)
            return

        if resp.get("type") == "auth_code_required":
            code = input("\nCódigo 2FA (revisa el correo): ").strip()
            await ws.send(json.dumps({"type": "auth_code", "code": code}))
            raw  = await asyncio.wait_for(ws.recv(), timeout=30)
            resp = json.loads(raw)
            print("Respuesta 2FA:", resp.get("type"), "—", resp.get("message", ""))
            if resp.get("type") == "auth_ok":
                _save_token(cfg, resp)
                return

        print("Error:", resp.get("message", resp))

def _save_token(cfg, resp):
    token = resp.get("token", "")
    if token:
        cfg["session_token"] = token
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        print("\nToken guardado en config.json — ya puedes lanzar client.py normalmente.")
    else:
        print("Auth OK pero sin token:", resp)

asyncio.run(do_auth())
