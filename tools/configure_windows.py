from __future__ import annotations

import ast
import json
import re
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

DEFAULTS: dict[str, str] = {
    "BOT_TOKEN": "",
    "BOT_ID": "",
    "ADMIN_IDS": "",
    "DATABASE_URL": "sqlite+aiosqlite:///./data/shop.db",
    "STORE_NAME": "Shop Ultra",
    "BINANCE_PAY_ID": "",
    "BINANCE_PAY_NAME": "",
    "SUPPORT_USERNAME": "",
    "BONUS_TIERS": "50:2,100:5",
    "MIN_DEPOSIT": "1.00",
    "BINANCE_API_KEY": "",
    "BINANCE_API_SECRET": "",
    "BINANCE_BASE_URL": "https://api.binance.com",
    "BINANCE_HISTORY_HOURS": "72",
    "BINANCE_CACHE_SECONDS": "15",
    "BINANCE_RECV_WINDOW_MS": "5000",
    "BINANCE_REQUEST_TIMEOUT_SECONDS": "20",
    "BINANCE_VERIFY_ATTEMPTS": "2",
    "BINANCE_VERIFY_RETRY_DELAY_SECONDS": "8",
    "VERIFICATION_COOLDOWN_SECONDS": "15",
    "API_PROVIDERS_FILE": "data/providers.json",
    "PRODSELLER_ENABLED": "false",
    "PRODSELLER_BASE_URL": "http://51.77.244.194/v1",
    "PRODSELLER_API_KEY": "",
    "PRODSELLER_ALLOW_INSECURE_HTTP": "false",
    "PRODSELLER_MARKUP_PERCENT": "20",
    "PRODSELLER_SYNC_PRICES": "false",
    "PRODSELLER_ALLOW_BELOW_COST": "false",
    "PRODSELLER_AUTO_SYNC_MINUTES": "10",
    "PRODSELLER_CACHE_SECONDS": "60",
    "PRODSELLER_TIMEOUT_SECONDS": "20",
    "PRODSELLER_ORDER_POLL_ATTEMPTS": "4",
    "PRODSELLER_ORDER_POLL_DELAY_SECONDS": "2",
    "DROP_PENDING_UPDATES": "false",
    "LOG_LEVEL": "INFO",
}


class ConfigurationCancelled(Exception):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
            try:
                value = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError):
                value = raw_value[1:-1]
        else:
            value = raw_value
        values[key] = str(value)
    return values


def masked(value: str) -> str:
    if not value:
        return "vacío"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * min(12, len(value) - 8)}{value[-4:]}"


def validate_bot_token(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", value):
        raise ValueError("El token no tiene el formato esperado de BotFather.")
    return value


def validate_optional_positive_int(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("Debe ser un número entero positivo.")
    return str(int(value))


def validate_admin_ids(value: str) -> str:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        raise ValueError("Debes indicar al menos un ID numérico de administrador.")
    normalized: list[str] = []
    for part in parts:
        if not part.isdigit() or int(part) <= 0:
            raise ValueError(
                "Los ID de administrador deben ser positivos y estar separados por comas."
            )
        normalized.append(str(int(part)))
    return ",".join(dict.fromkeys(normalized))


def validate_nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Este valor es obligatorio.")
    return value


def validate_bonus_tiers(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    normalized: list[tuple[Decimal, Decimal]] = []
    for entry in value.split(","):
        try:
            threshold_raw, percent_raw = entry.strip().split(":", 1)
            threshold = Decimal(threshold_raw.strip())
            percent = Decimal(percent_raw.strip())
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("Usa el formato monto:porcentaje, por ejemplo 50:2,100:5.") from exc
        if threshold <= 0 or percent < 0 or percent > 100:
            raise ValueError("El monto debe ser positivo y el porcentaje debe estar entre 0 y 100.")
        normalized.append((threshold, percent))
    normalized.sort(key=lambda item: item[0])
    return ",".join(f"{threshold:g}:{percent:g}" for threshold, percent in normalized)


def validate_positive_decimal(value: str) -> str:
    value = value.strip().replace(",", ".")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Indica un número válido.") from exc
    if amount <= 0:
        raise ValueError("El valor debe ser mayor que cero.")
    return f"{amount.quantize(Decimal('0.01')):.2f}"


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@")


def serialize_env(values: dict[str, str]) -> str:
    def line(key: str) -> str:
        return f"{key}={json.dumps(values.get(key, ''), ensure_ascii=False)}"

    groups = [
        ("Telegram", ["BOT_TOKEN", "BOT_ID", "ADMIN_IDS"]),
        ("Base de datos", ["DATABASE_URL"]),
        (
            "Datos iniciales de la tienda",
            [
                "STORE_NAME",
                "BINANCE_PAY_ID",
                "BINANCE_PAY_NAME",
                "SUPPORT_USERNAME",
                "BONUS_TIERS",
                "MIN_DEPOSIT",
            ],
        ),
        (
            "Verificación automática de Binance Pay",
            [
                "BINANCE_API_KEY",
                "BINANCE_API_SECRET",
                "BINANCE_BASE_URL",
                "BINANCE_HISTORY_HOURS",
                "BINANCE_CACHE_SECONDS",
                "BINANCE_RECV_WINDOW_MS",
                "BINANCE_REQUEST_TIMEOUT_SECONDS",
                "BINANCE_VERIFY_ATTEMPTS",
                "BINANCE_VERIFY_RETRY_DELAY_SECONDS",
                "VERIFICATION_COOLDOWN_SECONDS",
            ],
        ),
        (
            "Proveedores API",
            ["API_PROVIDERS_FILE"],
        ),
        (
            "Compatibilidad con configuración ProdSeller antigua",
            [
                "PRODSELLER_ENABLED",
                "PRODSELLER_BASE_URL",
                "PRODSELLER_API_KEY",
                "PRODSELLER_ALLOW_INSECURE_HTTP",
                "PRODSELLER_MARKUP_PERCENT",
                "PRODSELLER_SYNC_PRICES",
                "PRODSELLER_ALLOW_BELOW_COST",
                "PRODSELLER_AUTO_SYNC_MINUTES",
                "PRODSELLER_CACHE_SECONDS",
                "PRODSELLER_TIMEOUT_SECONDS",
                "PRODSELLER_ORDER_POLL_ATTEMPTS",
                "PRODSELLER_ORDER_POLL_DELAY_SECONDS",
            ],
        ),
        ("Operación", ["DROP_PENDING_UPDATES", "LOG_LEVEL"]),
    ]

    output: list[str] = [
        "# Archivo generado por configurar.bat.",
        "# No compartas este archivo: contiene credenciales privadas.",
        "",
    ]
    for title, keys in groups:
        output.append(f"# {title}")
        output.extend(line(key) for key in keys)
        output.append("")
    return "\n".join(output).rstrip() + "\n"


def load_values() -> dict[str, str]:
    values = DEFAULTS.copy()
    values.update(parse_env_file(EXAMPLE_PATH))
    values.update(parse_env_file(ENV_PATH))

    if "REEMPLAZA_CON_EL_TOKEN" in values.get("BOT_TOKEN", ""):
        values["BOT_TOKEN"] = ""
    if values.get("BOT_ID") == "1234567890":
        values["BOT_ID"] = ""
    if values.get("ADMIN_IDS") == "123456789":
        values["ADMIN_IDS"] = ""
    if values.get("BINANCE_PAY_ID") == "123456789":
        values["BINANCE_PAY_ID"] = ""
    if values.get("BINANCE_PAY_NAME") == "TU NOMBRE EN BINANCE":
        values["BINANCE_PAY_NAME"] = ""
    if values.get("SUPPORT_USERNAME") == "tu_usuario_telegram":
        values["SUPPORT_USERNAME"] = ""
    return values


def validate_all(raw: dict[str, str], base_values: dict[str, str]) -> dict[str, str]:
    values = base_values.copy()
    values["BOT_TOKEN"] = validate_bot_token(raw.get("BOT_TOKEN", ""))

    bot_id = validate_optional_positive_int(raw.get("BOT_ID", ""))
    values["BOT_ID"] = bot_id or values["BOT_TOKEN"].split(":", 1)[0]
    values["ADMIN_IDS"] = validate_admin_ids(raw.get("ADMIN_IDS", ""))
    values["STORE_NAME"] = validate_nonempty(raw.get("STORE_NAME", ""))
    values["BINANCE_PAY_ID"] = raw.get("BINANCE_PAY_ID", "").strip()
    values["BINANCE_PAY_NAME"] = raw.get("BINANCE_PAY_NAME", "").strip()
    values["SUPPORT_USERNAME"] = normalize_username(raw.get("SUPPORT_USERNAME", ""))
    values["BONUS_TIERS"] = validate_bonus_tiers(raw.get("BONUS_TIERS", ""))
    values["MIN_DEPOSIT"] = validate_positive_decimal(raw.get("MIN_DEPOSIT", ""))

    api_key = raw.get("BINANCE_API_KEY", "").strip()
    api_secret = raw.get("BINANCE_API_SECRET", "").strip()
    if bool(api_key) != bool(api_secret):
        raise ValueError(
            "Debes completar la API Key y la API Secret de Binance, o dejar ambas vacías."
        )
    values["BINANCE_API_KEY"] = api_key
    values["BINANCE_API_SECRET"] = api_secret
    return values


def save_values(values: dict[str, str]) -> str | None:
    temporary_path = ROOT / ".env.tmp"
    temporary_path.write_text(serialize_env(values), encoding="utf-8")

    sys.path.insert(0, str(ROOT))
    try:
        from app.config import Settings

        Settings(_env_file=temporary_path)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        raise ValueError(f"No se pudo validar la configuración: {exc}") from exc

    backup_name: str | None = None
    if ENV_PATH.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = ROOT / f".env.backup-{stamp}"
        shutil.copy2(ENV_PATH, backup_path)
        backup_name = backup_path.name

    temporary_path.replace(ENV_PATH)
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    return backup_name


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    values = load_values()
    result = {"saved": False}

    root = tk.Tk()
    root.title("SHOP ULTRA BOT - Configuración")
    root.geometry("760x700")
    root.minsize(680, 620)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)

    ttk.Label(
        outer, text="Configuración inicial de SHOP ULTRA BOT", font=("Segoe UI", 15, "bold")
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 5))
    ttk.Label(
        outer,
        text="Puedes escribir o pegar con Ctrl+V. También puedes usar los botones Pegar.",
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

    entries: dict[str, ttk.Entry] = {}
    row = 2

    def paste_into(entry: ttk.Entry) -> None:
        try:
            text = root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning(
                "Portapapeles", "No hay texto disponible en el portapapeles.", parent=root
            )
            return
        entry.delete(0, tk.END)
        entry.insert(0, text.strip())
        entry.focus_set()

    def add_section(title: str) -> None:
        nonlocal row
        ttk.Separator(outer, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(9, 7)
        )
        row += 1
        ttk.Label(outer, text=title, font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(0, 5)
        )
        row += 1

    def add_field(
        key: str,
        label: str,
        *,
        secret: bool = False,
        paste_button: bool = False,
    ) -> None:
        nonlocal row
        ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        entry = ttk.Entry(outer, show="*" if secret else "")
        entry.insert(0, values.get(key, ""))
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        entries[key] = entry
        if paste_button:
            ttk.Button(
                outer, text="Pegar", command=lambda item=entry: paste_into(item), width=9
            ).grid(row=row, column=2, sticky="e", padx=(8, 0), pady=4)
        row += 1

    add_section("Telegram")
    add_field("BOT_TOKEN", "Token de BotFather", secret=True, paste_button=True)
    add_field("BOT_ID", "ID numérico del bot (opcional)")
    add_field("ADMIN_IDS", "ID de administrador")

    add_section("Tienda")
    add_field("STORE_NAME", "Nombre de la tienda")
    add_field("BINANCE_PAY_ID", "Binance Pay ID")
    add_field("BINANCE_PAY_NAME", "Nombre en Binance")
    add_field("SUPPORT_USERNAME", "Usuario de soporte (sin @)")
    add_field("BONUS_TIERS", "Bonos (ej. 50:2,100:5)")
    add_field("MIN_DEPOSIT", "Depósito mínimo en USDT")

    add_section("API de Binance (opcional)")
    ttk.Label(
        outer,
        text=(
            "Debe pertenecer a la misma cuenta que recibe el Pay ID. "
            "Déjala vacía para aprobar depósitos manualmente desde /admin."
        ),
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
    row += 1
    add_field("BINANCE_API_KEY", "Binance API Key", secret=True, paste_button=True)
    add_field("BINANCE_API_SECRET", "Binance API Secret", secret=True, paste_button=True)

    show_secrets = tk.BooleanVar(value=False)

    def toggle_secrets() -> None:
        show = "" if show_secrets.get() else "*"
        entries["BOT_TOKEN"].configure(show=show)
        entries["BINANCE_API_KEY"].configure(show=show)
        entries["BINANCE_API_SECRET"].configure(show=show)

    ttk.Checkbutton(
        outer,
        text="Mostrar token y credenciales",
        variable=show_secrets,
        command=toggle_secrets,
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 12))
    row += 1

    status_var = tk.StringVar(value="")
    ttk.Label(outer, textvariable=status_var).grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(0, 8)
    )
    row += 1

    buttons = ttk.Frame(outer)
    buttons.grid(row=row, column=0, columnspan=3, sticky="e")

    def save() -> None:
        raw = {key: entry.get() for key, entry in entries.items()}
        try:
            normalized = validate_all(raw, values)
            backup = save_values(normalized)
        except ValueError as exc:
            status_var.set(str(exc))
            messagebox.showerror("Datos inválidos", str(exc), parent=root)
            return

        result["saved"] = True
        backup_text = f"\nCopia de seguridad: {backup}" if backup else ""
        messagebox.showinfo(
            "Configuración guardada",
            "La configuración se guardó correctamente en .env.\n\n"
            "Ahora ejecuta iniciar_bot.bat."
            f"{backup_text}",
            parent=root,
        )
        root.destroy()

    def cancel() -> None:
        if messagebox.askyesno("Cancelar", "¿Cerrar sin guardar la configuración?", parent=root):
            root.destroy()

    ttk.Button(buttons, text="Cancelar", command=cancel).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="Guardar configuración", command=save).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Control-s>", lambda _event: save())
    entries["BOT_TOKEN"].focus_set()
    root.mainloop()
    return 0 if result["saved"] else 1


def prompt_value(
    label: str,
    current: str = "",
    *,
    validator: Callable[[str], str] | None = None,
    allow_clear: bool = False,
) -> str:
    while True:
        default_hint = f" [{masked(current)}]" if current else ""
        clear_hint = " (escribe BORRAR para dejarlo vacío)" if allow_clear and current else ""
        try:
            raw = input(f"{label}{default_hint}{clear_hint}: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise ConfigurationCancelled from exc

        if not raw:
            candidate = current
        elif allow_clear and raw.strip().upper() == "BORRAR":
            candidate = ""
        else:
            candidate = raw.strip()

        try:
            return validator(candidate) if validator else candidate
        except ValueError as exc:
            print(f"Error: {exc}")


def run_console() -> int:
    print("\nCONFIGURACIÓN INICIAL DE SHOP ULTRA BOT")
    print("El formulario gráfico no pudo abrirse. Se usará el modo consola.")
    print("Los datos son visibles para permitir escribir y pegar sin bloqueos.\n")

    values = load_values()
    token = prompt_value(
        "Token del bot entregado por BotFather",
        values.get("BOT_TOKEN", ""),
        validator=validate_bot_token,
    )
    bot_id_default = values.get("BOT_ID", "") or token.split(":", 1)[0]

    raw = {
        "BOT_TOKEN": token,
        "BOT_ID": prompt_value(
            "ID numérico del bot",
            bot_id_default,
            validator=validate_optional_positive_int,
            allow_clear=True,
        ),
        "ADMIN_IDS": prompt_value(
            "Tu ID numérico de Telegram", values.get("ADMIN_IDS", ""), validator=validate_admin_ids
        ),
        "STORE_NAME": prompt_value(
            "Nombre de la tienda",
            values.get("STORE_NAME", "Shop Ultra"),
            validator=validate_nonempty,
        ),
        "BINANCE_PAY_ID": prompt_value(
            "Binance Pay ID", values.get("BINANCE_PAY_ID", ""), allow_clear=True
        ),
        "BINANCE_PAY_NAME": prompt_value(
            "Nombre que aparece en Binance", values.get("BINANCE_PAY_NAME", ""), allow_clear=True
        ),
        "SUPPORT_USERNAME": prompt_value(
            "Usuario de soporte sin @", values.get("SUPPORT_USERNAME", ""), allow_clear=True
        ),
        "BONUS_TIERS": prompt_value(
            "Bonos de recarga",
            values.get("BONUS_TIERS", "50:2,100:5"),
            validator=validate_bonus_tiers,
            allow_clear=True,
        ),
        "MIN_DEPOSIT": prompt_value(
            "Depósito mínimo en USDT",
            values.get("MIN_DEPOSIT", "1.00"),
            validator=validate_positive_decimal,
        ),
        "BINANCE_API_KEY": prompt_value(
            "Binance API Key (opcional)", values.get("BINANCE_API_KEY", ""), allow_clear=True
        ),
        "BINANCE_API_SECRET": prompt_value(
            "Binance API Secret (opcional)", values.get("BINANCE_API_SECRET", ""), allow_clear=True
        ),
    }

    normalized = validate_all(raw, values)
    backup = save_values(normalized)
    print("\nConfiguración guardada correctamente en .env")
    if backup:
        print(f"Copia de seguridad creada: {backup}")
    print("Ahora ejecuta iniciar_bot.bat.")
    return 0


def main() -> int:
    if "--console" in sys.argv:
        return run_console()
    try:
        return run_gui()
    except Exception as exc:
        print(f"No se pudo abrir el formulario gráfico: {exc}")
        return run_console()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigurationCancelled:
        print("\nConfiguración cancelada.")
        raise SystemExit(1) from None
    except ValueError as exc:
        print(f"\nERROR: {exc}")
        raise SystemExit(1) from None
