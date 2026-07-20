from __future__ import annotations

import ast
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.provider_registry import (  # noqa: E402
    ADAPTER_LABELS,
    CANBOSO_ADAPTER_CODE,
    PRODSELLER_ADAPTER_CODE,
    ProviderConfig,
    ProviderConfigError,
    provider_slug,
    save_provider_configs,
)

ENV_FILE = ROOT / ".env"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        raw_value = value.strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
            try:
                raw_value = str(ast.literal_eval(raw_value))
            except (SyntaxError, ValueError):
                raw_value = raw_value[1:-1]
        values[key.strip()] = raw_value
    return values


def providers_file() -> Path:
    configured = load_env().get("API_PROVIDERS_FILE", "data/providers.json").strip()
    path = Path(configured or "data/providers.json").expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def load_raw_configs() -> list[dict[str, Any]]:
    path = providers_file()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        providers = data.get("providers", []) if isinstance(data, dict) else []
        if not isinstance(providers, list):
            raise ProviderConfigError("providers.json es inválido: providers no es una lista")
        return [dict(item) for item in providers if isinstance(item, dict)]

    # Backward compatibility with the original single-ProdSeller .env settings.
    env = load_env()
    if env.get("PRODSELLER_ENABLED", "false").lower() != "true":
        return []
    key = env.get("PRODSELLER_API_KEY", "").strip()
    if not key:
        return []
    return [
        {
            "code": "prodseller",
            "name": "ProdSeller",
            "adapter": PRODSELLER_ADAPTER_CODE,
            "enabled": True,
            "base_url": env.get("PRODSELLER_BASE_URL", "http://51.77.244.194/v1"),
            "api_key": key,
            "api_key_header": "X-API-Key",
            "allow_insecure_http": env.get("PRODSELLER_ALLOW_INSECURE_HTTP", "false").lower()
            == "true",
            "markup_percent": env.get("PRODSELLER_MARKUP_PERCENT", "20"),
            "auto_sync_minutes": env.get("PRODSELLER_AUTO_SYNC_MINUTES", "10"),
            "cache_seconds": env.get("PRODSELLER_CACHE_SECONDS", "60"),
            "timeout_seconds": env.get("PRODSELLER_TIMEOUT_SECONDS", "20"),
            "allow_below_cost": env.get("PRODSELLER_ALLOW_BELOW_COST", "false").lower() == "true",
            "order_poll_attempts": env.get("PRODSELLER_ORDER_POLL_ATTEMPTS", "4"),
            "order_poll_delay_seconds": env.get("PRODSELLER_ORDER_POLL_DELAY_SECONDS", "2"),
        }
    ]


def backup_file() -> Path | None:
    path = providers_file()
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = ROOT / "backups" / stamp / "data" / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def run_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    configs = [ProviderConfig.from_dict(raw).to_dict() for raw in load_raw_configs()]

    root = tk.Tk()
    root.title("Shop Ultra Bot - Configurar proveedores API")
    root.geometry("1030x760")
    root.minsize(970, 700)

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)
    outer.columnconfigure(1, weight=1)
    outer.rowconfigure(1, weight=1)

    ttk.Label(
        outer,
        text="Proveedores API",
        font=("Segoe UI", 16, "bold"),
    ).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(
        outer,
        text=(
            "Registra múltiples conexiones ProdSeller API v1 o Canboso Telegram Buyer "
            "API v1.2. Después reinicia el bot, sincroniza cada catálogo y selecciona "
            "los productos desde /admin."
        ),
        wraplength=960,
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(34, 14))

    left = ttk.Frame(outer)
    left.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
    left.rowconfigure(0, weight=1)
    listbox = tk.Listbox(left, width=38, exportselection=False)
    listbox.grid(row=0, column=0, columnspan=3, sticky="nsew")
    scrollbar = ttk.Scrollbar(left, orient="vertical", command=listbox.yview)
    scrollbar.grid(row=0, column=3, sticky="ns")
    listbox.configure(yscrollcommand=scrollbar.set)

    editor = ttk.LabelFrame(outer, text="Datos del proveedor", padding=12)
    editor.grid(row=1, column=1, sticky="nsew")
    editor.columnconfigure(1, weight=1)

    fields: dict[str, tk.Variable] = {
        "adapter": tk.StringVar(value=PRODSELLER_ADAPTER_CODE),
        "name": tk.StringVar(),
        "code": tk.StringVar(),
        "base_url": tk.StringVar(value="https://"),
        "api_key": tk.StringVar(),
        "api_key_header": tk.StringVar(value="X-API-Key"),
        "markup_percent": tk.StringVar(value="20"),
        "auto_sync_minutes": tk.StringVar(value="10"),
        "cache_seconds": tk.StringVar(value="60"),
        "timeout_seconds": tk.StringVar(value="20"),
        "order_poll_attempts": tk.StringVar(value="4"),
        "order_poll_delay_seconds": tk.StringVar(value="2"),
        "enabled": tk.BooleanVar(value=True),
        "allow_insecure_http": tk.BooleanVar(value=False),
        "allow_below_cost": tk.BooleanVar(value=False),
    }

    labels = [
        ("Tipo de API", "adapter"),
        ("Nombre", "name"),
        ("Código interno", "code"),
        ("Base URL", "base_url"),
        ("API Key", "api_key"),
        ("Header de API Key", "api_key_header"),
        ("Margen inicial (%)", "markup_percent"),
        ("Sincronización (min)", "auto_sync_minutes"),
        ("Caché (seg)", "cache_seconds"),
        ("Timeout (seg)", "timeout_seconds"),
        ("Intentos de entrega", "order_poll_attempts"),
        ("Espera entre intentos", "order_poll_delay_seconds"),
    ]
    widgets: dict[str, tk.Widget] = {}
    rows: dict[str, int] = {}
    for row, (label, key) in enumerate(labels):
        rows[key] = row
        ttk.Label(editor, text=label).grid(row=row, column=0, sticky="w", pady=4)
        if key == "adapter":
            widget: tk.Widget = ttk.Combobox(
                editor,
                textvariable=fields[key],
                values=(PRODSELLER_ADAPTER_CODE, CANBOSO_ADAPTER_CODE),
                state="readonly",
            )
        else:
            widget = ttk.Entry(
                editor,
                textvariable=fields[key],
                show="*" if key == "api_key" else "",
            )
        widget.grid(row=row, column=1, sticky="ew", pady=4)
        widgets[key] = widget

    def paste_key() -> None:
        try:
            value = root.clipboard_get().strip()
        except tk.TclError:
            messagebox.showwarning("Portapapeles", "No hay texto para pegar.", parent=root)
            return
        fields["api_key"].set(value)

    ttk.Button(editor, text="Pegar", command=paste_key).grid(
        row=rows["api_key"], column=2, padx=(8, 0)
    )

    show_key = tk.BooleanVar(value=False)

    def toggle_key() -> None:
        widgets["api_key"].configure(show="" if show_key.get() else "*")

    options_row = len(labels)
    ttk.Checkbutton(
        editor,
        text="Mostrar API Key",
        variable=show_key,
        command=toggle_key,
    ).grid(row=options_row, column=1, sticky="w", pady=(8, 2))
    ttk.Checkbutton(editor, text="Proveedor activo", variable=fields["enabled"]).grid(
        row=options_row + 1, column=1, sticky="w", pady=2
    )
    ttk.Checkbutton(
        editor,
        text="Acepto usar HTTP sin cifrado",
        variable=fields["allow_insecure_http"],
    ).grid(row=options_row + 2, column=1, sticky="w", pady=2)
    ttk.Checkbutton(
        editor,
        text="Permitir venta por debajo del costo (no recomendado)",
        variable=fields["allow_below_cost"],
    ).grid(row=options_row + 3, column=1, sticky="w", pady=2)
    ttk.Label(
        editor,
        text=(
            "Canboso: usa https://canboso.com y la clave tgb_...; la autenticación se "
            "envía automáticamente como key, por lo que el campo Header se ignora. "
            "El código interno relaciona productos, compras e historial y no debe "
            "cambiarse después de guardar. Las claves se guardan en data\\providers.json."
        ),
        wraplength=610,
        foreground="#8a4b00",
    ).grid(row=options_row + 4, column=0, columnspan=3, sticky="w", pady=(10, 0))

    current_index: int | None = None

    def apply_adapter_defaults(_event: object = None) -> None:
        adapter = str(fields["adapter"].get())
        current_url = str(fields["base_url"].get()).strip()
        if adapter == CANBOSO_ADAPTER_CODE:
            if current_url in {"", "https://"} or "51.77.244.194" in current_url:
                fields["base_url"].set("https://canboso.com")
            fields["api_key_header"].set("X-API-Key")
            fields["order_poll_attempts"].set("1")
            fields["order_poll_delay_seconds"].set("0")
        elif current_url == "https://canboso.com":
            fields["base_url"].set("https://")
            fields["order_poll_attempts"].set("4")
            fields["order_poll_delay_seconds"].set("2")

    widgets["adapter"].bind("<<ComboboxSelected>>", apply_adapter_defaults)

    def refresh_list(select_index: int | None = None) -> None:
        listbox.delete(0, tk.END)
        for item in configs:
            status = "ACTIVA" if item.get("enabled", True) else "PAUSADA"
            adapter = ADAPTER_LABELS.get(str(item.get("adapter") or ""), "API")
            listbox.insert(
                tk.END,
                f"{item.get('name', '?')} [{status}] ({item.get('code', '?')}) · {adapter}",
            )
        if select_index is not None and 0 <= select_index < len(configs):
            listbox.selection_set(select_index)
            listbox.activate(select_index)
            load_selected(select_index)

    def clear_form() -> None:
        nonlocal current_index
        current_index = None
        defaults: dict[str, Any] = {
            "adapter": PRODSELLER_ADAPTER_CODE,
            "name": "",
            "code": "",
            "base_url": "https://",
            "api_key": "",
            "api_key_header": "X-API-Key",
            "markup_percent": "20",
            "auto_sync_minutes": "10",
            "cache_seconds": "60",
            "timeout_seconds": "20",
            "order_poll_attempts": "4",
            "order_poll_delay_seconds": "2",
            "enabled": True,
            "allow_insecure_http": False,
            "allow_below_cost": False,
        }
        for key, value in defaults.items():
            fields[key].set(value)
        widgets["code"].configure(state="normal")
        widgets["name"].focus_set()

    def load_selected(index: int) -> None:
        nonlocal current_index
        if index < 0 or index >= len(configs):
            return
        current_index = index
        item = configs[index]
        for key, variable in fields.items():
            fallback: Any = False if isinstance(variable, tk.BooleanVar) else ""
            variable.set(item.get(key, fallback))
        widgets["code"].configure(state="disabled")

    def on_select(_event: object = None) -> None:
        selection = listbox.curselection()
        if selection:
            load_selected(int(selection[0]))

    listbox.bind("<<ListboxSelect>>", on_select)

    def form_raw() -> dict[str, Any]:
        name = str(fields["name"].get()).strip()
        code = str(fields["code"].get()).strip().lower() or provider_slug(name)
        return {
            "name": name,
            "code": code,
            "adapter": str(fields["adapter"].get()).strip() or PRODSELLER_ADAPTER_CODE,
            "base_url": str(fields["base_url"].get()).strip(),
            "api_key": str(fields["api_key"].get()).strip(),
            "api_key_header": str(fields["api_key_header"].get()).strip(),
            "markup_percent": str(fields["markup_percent"].get()).strip(),
            "auto_sync_minutes": str(fields["auto_sync_minutes"].get()).strip(),
            "cache_seconds": str(fields["cache_seconds"].get()).strip(),
            "timeout_seconds": str(fields["timeout_seconds"].get()).strip(),
            "order_poll_attempts": str(fields["order_poll_attempts"].get()).strip(),
            "order_poll_delay_seconds": str(fields["order_poll_delay_seconds"].get()).strip(),
            "enabled": bool(fields["enabled"].get()),
            "allow_insecure_http": bool(fields["allow_insecure_http"].get()),
            "allow_below_cost": bool(fields["allow_below_cost"].get()),
        }

    def apply_form() -> bool:
        nonlocal current_index
        try:
            validated = ProviderConfig.from_dict(form_raw())
        except ProviderConfigError as exc:
            messagebox.showerror("Configuración inválida", str(exc), parent=root)
            return False
        for index, item in enumerate(configs):
            if index != current_index and item.get("code") == validated.code:
                messagebox.showerror(
                    "Código duplicado",
                    f"Ya existe un proveedor con el código {validated.code}.",
                    parent=root,
                )
                return False
        if current_index is None:
            configs.append(validated.to_dict())
            current_index = len(configs) - 1
        else:
            configs[current_index] = validated.to_dict()
        refresh_list(current_index)
        return True

    def delete_selected() -> None:
        nonlocal current_index
        if current_index is None:
            return
        if not messagebox.askyesno(
            "Quitar conexión",
            "Se quitará la conexión, pero los productos e historiales guardados no se "
            "borrarán. Puedes recuperarlos agregando otra vez el mismo código. ¿Continuar?",
            parent=root,
        ):
            return
        configs.pop(current_index)
        current_index = None
        refresh_list()
        clear_form()

    buttons_left = ttk.Frame(left)
    buttons_left.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
    ttk.Button(buttons_left, text="Nuevo", command=clear_form).pack(side="left")
    ttk.Button(buttons_left, text="Eliminar", command=delete_selected).pack(
        side="left", padx=(8, 0)
    )
    ttk.Button(editor, text="Aplicar proveedor", command=apply_form).grid(
        row=options_row + 5, column=1, sticky="e", pady=(14, 0)
    )

    result = {"saved": False}

    def save_all() -> None:
        if str(fields["name"].get()).strip() and not apply_form():
            return
        try:
            validated = [ProviderConfig.from_dict(item) for item in configs]
            codes = [item.code for item in validated]
            if len(codes) != len(set(codes)):
                raise ProviderConfigError("Hay códigos duplicados")
            backup = backup_file()
            save_provider_configs(providers_file(), validated)
        except (ProviderConfigError, OSError, json.JSONDecodeError) as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=root)
            return
        result["saved"] = True
        backup_text = f"\nRespaldo: {backup}" if backup else ""
        messagebox.showinfo(
            "APIs guardadas",
            f"Configuración guardada. Reinicia el bot con iniciar_bot.bat.{backup_text}",
            parent=root,
        )
        root.destroy()

    bottom = ttk.Frame(outer)
    bottom.grid(row=2, column=0, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(bottom, text="Cancelar", command=root.destroy).pack(side="left", padx=(0, 8))
    ttk.Button(bottom, text="Guardar todas", command=save_all).pack(side="left")

    refresh_list(0 if configs else None)
    if not configs:
        clear_form()
    root.bind("<Control-s>", lambda _event: save_all())
    root.mainloop()
    return 0 if result["saved"] else 1


def run_console() -> int:
    print("No se pudo abrir el formulario gráfico.")
    print("Edita data/providers.json usando data/providers.example.json como referencia.")
    return 1


def main() -> int:
    try:
        return run_gui()
    except Exception as exc:
        print(f"Error abriendo el configurador: {exc}")
        return run_console()


if __name__ == "__main__":
    raise SystemExit(main())
