from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.configure_windows import load_values, save_values  # noqa: E402


class Cancelled(Exception):
    pass


def validate_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "La URL debe comenzar por http:// o https:// e incluir un servidor válido."
        )
    return value


def validate_key(value: str, *, enabled: bool) -> str:
    value = value.strip()
    if enabled and not value:
        raise ValueError("Debes pegar la API Key de ProdSeller.")
    if value and not value.startswith("psk_"):
        raise ValueError("La API Key de ProdSeller debe comenzar por psk_.")
    return value


def validate_markup(value: str) -> str:
    try:
        amount = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("El margen debe ser un número válido.") from exc
    if amount < 0 or amount > 1000:
        raise ValueError("El margen debe estar entre 0 y 1000 por ciento.")
    return f"{amount.quantize(Decimal('0.01')):f}"


def validate_minutes(value: str) -> str:
    value = value.strip()
    if not value.isdigit() or not 0 <= int(value) <= 1440:
        raise ValueError("La sincronización debe estar entre 0 y 1440 minutos.")
    return str(int(value))


def normalized_values(raw: dict[str, object], base: dict[str, str]) -> dict[str, str]:
    values = base.copy()
    enabled = bool(raw["enabled"])
    base_url = validate_url(str(raw["base_url"]))
    api_key = validate_key(str(raw["api_key"]), enabled=enabled)
    allow_http = bool(raw["allow_http"])
    if enabled and base_url.lower().startswith("http://") and not allow_http:
        raise ValueError(
            "La URL usa HTTP sin cifrado. Marca la aceptación explícita o solicita una URL HTTPS."
        )

    values.update(
        {
            "PRODSELLER_ENABLED": "true" if enabled else "false",
            "PRODSELLER_BASE_URL": base_url,
            "PRODSELLER_API_KEY": api_key,
            "PRODSELLER_ALLOW_INSECURE_HTTP": "true" if allow_http else "false",
            "PRODSELLER_MARKUP_PERCENT": validate_markup(str(raw["markup"])),
            "PRODSELLER_SYNC_PRICES": "true" if bool(raw["sync_prices"]) else "false",
            "PRODSELLER_ALLOW_BELOW_COST": ("true" if bool(raw["allow_below_cost"]) else "false"),
            "PRODSELLER_AUTO_SYNC_MINUTES": validate_minutes(str(raw["sync_minutes"])),
        }
    )
    return values


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    if not (ROOT / ".env").exists():
        messagebox.showerror(
            "Falta configuración",
            "Primero ejecuta configurar.bat para crear el archivo .env.",
        )
        return 1

    values = load_values()
    root = tk.Tk()
    root.title("SHOP ULTRA BOT - ProdSeller API")
    root.geometry("720x570")
    root.minsize(650, 520)

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(
        frame,
        text="Integración ProdSeller API v1",
        font=("Segoe UI", 15, "bold"),
    ).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(
        frame,
        text=(
            "Este formulario modifica solamente la sección ProdSeller del .env. "
            "No borra usuarios, balances, productos ni depósitos."
        ),
        wraplength=660,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 14))

    enabled = tk.BooleanVar(value=values.get("PRODSELLER_ENABLED", "false").lower() == "true")
    allow_http = tk.BooleanVar(
        value=values.get("PRODSELLER_ALLOW_INSECURE_HTTP", "false").lower() == "true"
    )
    sync_prices = tk.BooleanVar(
        value=values.get("PRODSELLER_SYNC_PRICES", "false").lower() == "true"
    )
    allow_below = tk.BooleanVar(
        value=values.get("PRODSELLER_ALLOW_BELOW_COST", "false").lower() == "true"
    )

    ttk.Checkbutton(
        frame,
        text="Activar ProdSeller en el bot",
        variable=enabled,
    ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

    ttk.Label(frame, text="Base URL").grid(row=3, column=0, sticky="w", pady=5)
    url_entry = ttk.Entry(frame)
    url_entry.insert(0, values.get("PRODSELLER_BASE_URL", "http://51.77.244.194/v1"))
    url_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)

    ttk.Label(frame, text="API Key").grid(row=4, column=0, sticky="w", pady=5)
    key_entry = ttk.Entry(frame, show="*")
    key_entry.insert(0, values.get("PRODSELLER_API_KEY", ""))
    key_entry.grid(row=4, column=1, sticky="ew", pady=5)

    def paste_key() -> None:
        try:
            content = root.clipboard_get().strip()
        except tk.TclError:
            messagebox.showwarning("Portapapeles", "No hay texto para pegar.", parent=root)
            return
        key_entry.delete(0, tk.END)
        key_entry.insert(0, content)

    ttk.Button(frame, text="Pegar", command=paste_key, width=9).grid(
        row=4, column=2, padx=(8, 0), pady=5
    )

    show_key = tk.BooleanVar(value=False)

    def toggle_key() -> None:
        key_entry.configure(show="" if show_key.get() else "*")

    ttk.Checkbutton(
        frame,
        text="Mostrar API Key",
        variable=show_key,
        command=toggle_key,
    ).grid(row=5, column=1, columnspan=2, sticky="w")

    ttk.Label(frame, text="Margen para productos nuevos (%)").grid(
        row=6, column=0, sticky="w", pady=5
    )
    markup_entry = ttk.Entry(frame)
    markup_entry.insert(0, values.get("PRODSELLER_MARKUP_PERCENT", "20"))
    markup_entry.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)

    ttk.Label(frame, text="Sincronización automática (minutos)").grid(
        row=7, column=0, sticky="w", pady=5
    )
    sync_entry = ttk.Entry(frame)
    sync_entry.insert(0, values.get("PRODSELLER_AUTO_SYNC_MINUTES", "10"))
    sync_entry.grid(row=7, column=1, columnspan=2, sticky="ew", pady=5)

    ttk.Checkbutton(
        frame,
        text="Actualizar automáticamente precios ya editados durante la sincronización",
        variable=sync_prices,
    ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 2))
    ttk.Checkbutton(
        frame,
        text="Permitir vender por debajo del costo del proveedor (no recomendado)",
        variable=allow_below,
    ).grid(row=9, column=0, columnspan=3, sticky="w", pady=2)
    ttk.Checkbutton(
        frame,
        text="Acepto usar HTTP sin cifrado para esta API",
        variable=allow_http,
    ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(2, 10))

    ttk.Label(
        frame,
        text=(
            "Advertencia: con una URL http:// la API Key y los correos, claves o enlaces "
            "entregados pueden ser interceptados. La opción segura es una URL https://."
        ),
        foreground="#9a4b00",
        wraplength=660,
    ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(0, 12))

    status = tk.StringVar(value="")
    ttk.Label(frame, textvariable=status).grid(row=12, column=0, columnspan=3, sticky="w")

    buttons = ttk.Frame(frame)
    buttons.grid(row=13, column=0, columnspan=3, sticky="e", pady=(16, 0))

    result = {"saved": False}

    def save() -> None:
        raw: dict[str, object] = {
            "enabled": enabled.get(),
            "base_url": url_entry.get(),
            "api_key": key_entry.get(),
            "allow_http": allow_http.get(),
            "markup": markup_entry.get(),
            "sync_prices": sync_prices.get(),
            "allow_below_cost": allow_below.get(),
            "sync_minutes": sync_entry.get(),
        }
        try:
            new_values = normalized_values(raw, values)
            backup = save_values(new_values)
        except ValueError as exc:
            status.set(str(exc))
            messagebox.showerror("Configuración inválida", str(exc), parent=root)
            return
        result["saved"] = True
        backup_text = f"\nCopia de seguridad: {backup}" if backup else ""
        messagebox.showinfo(
            "ProdSeller configurado",
            f"Configuración guardada. Reinicia el bot con iniciar_bot.bat.{backup_text}",
            parent=root,
        )
        root.destroy()

    ttk.Button(buttons, text="Cancelar", command=root.destroy).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="Guardar", command=save).pack(side="left")
    root.bind("<Control-s>", lambda _event: save())
    key_entry.focus_set()
    root.mainloop()
    return 0 if result["saved"] else 1


def run_console() -> int:
    if not (ROOT / ".env").exists():
        print("Primero ejecuta configurar.bat para crear .env.")
        return 1
    values = load_values()
    print("\nCONFIGURACIÓN PRODSELLER API")
    enabled = input("Activar ProdSeller [S/N]: ").strip().upper() == "S"
    base_url = input(
        f"Base URL [{values.get('PRODSELLER_BASE_URL', 'http://51.77.244.194/v1')}]: "
    ).strip() or values.get("PRODSELLER_BASE_URL", "http://51.77.244.194/v1")
    api_key = input("API Key psk_: ").strip() or values.get("PRODSELLER_API_KEY", "")
    markup = input(
        f"Margen porcentual [{values.get('PRODSELLER_MARKUP_PERCENT', '20')}]: "
    ).strip() or values.get("PRODSELLER_MARKUP_PERCENT", "20")
    minutes = input(
        f"Sincronización en minutos [{values.get('PRODSELLER_AUTO_SYNC_MINUTES', '10')}]: "
    ).strip() or values.get("PRODSELLER_AUTO_SYNC_MINUTES", "10")
    allow_http = input("Aceptar HTTP sin cifrado [S/N]: ").strip().upper() == "S"
    raw: dict[str, object] = {
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "allow_http": allow_http,
        "markup": markup,
        "sync_prices": False,
        "allow_below_cost": False,
        "sync_minutes": minutes,
    }
    new_values = normalized_values(raw, values)
    backup = save_values(new_values)
    print("Configuración guardada.")
    if backup:
        print(f"Copia de seguridad: {backup}")
    return 0


def main() -> int:
    try:
        return run_gui()
    except Exception as exc:
        if "tkinter" not in type(exc).__module__.lower():
            print(f"No se pudo abrir el formulario: {exc}")
        try:
            return run_console()
        except (KeyboardInterrupt, EOFError, Cancelled):
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
