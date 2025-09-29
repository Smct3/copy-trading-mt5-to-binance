#!/usr/bin/env python3
"""
MT5 -> Binance USD-M Futures copier (final)
- Mapeo MT5 -> Binance
- Apalancamiento por grupos
- Trade size entre MIN/MAX (USDT) con tolerancia
- Paraleliza con threading
- Replica aperturas, SL y TP
- Replica cierres parciales (si MT5 reduce volume)
- Sincronización inversa: si se elimina posición en MT5 se cierra en Binance (solo si NO existe en MT5)
- Mensajes en colores y logs
"""

from decimal import Decimal, ROUND_DOWN, getcontext
from threading import Thread, Lock
import threading
import time
import traceback
import logging
import math

# dependencias: python-binance, MetaTrader5, colorama, requests, urllib3
from binance.client import Client
from binance.exceptions import BinanceAPIException
import MetaTrader5 as mt5
from colorama import Fore, Style, init as colorama_init
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

getcontext().prec = 18
colorama_init(autoreset=True)

# ---------------- CONFIG (ajusta según tu entorno) ----------------
MT5_LOGIN = "ingresa tu login de lite finance"
MT5_PASSWORD = "ingresa tu contraseña de trading"
MT5_SERVER = "ingresa el serviror ya sea demo o real(live) "

#la Api debe de permitir el trading en futuros

BINANCE_API_KEY = "ingresa tu api key de binance"
BINANCE_API_SECRET = "ingresa tu api secret de binance"

"""en esta parte puedes cambiar los valores para la apertura de ordene.
minimo en usdt para abrir operaciones
maximo en usdt para abrir operaciones
valor por defecto
tolerancia entre el valor por defecto, entre minimo y maximo. en usdt en este caso es de 0.05 centavos de usdt por operacion.
"""

MIN_TRADE_USDT = Decimal("1.60")
MAX_TRADE_USDT = Decimal("1.80")
TRADE_USDT = Decimal("1.70")   # valor editable al inicio
TRADE_USDT_TOLERANCE = Decimal("0.05")  # ± tolerancia

POLL_INTERVAL = 5
EXCHANGEINFO_CACHE_TTL = 300
MAX_RETRIES = 3

# MT5 -> Binance symbol mapping (edita según tu bróker)
MT5_TO_BINANCE = {
    "APTUSD": "APTUSDT",
    "ZILUSD": "ZILUSDT",
    "ARBUSD": "ARBUSDT",
    "ATOUSD": "ATOMUSDT",
    "BTCUSD": "BTCUSDT",   #AGREGAR AVXUSD/AVAXUSDT
    "ETHUSD": "ETHUSDT",
    "XRPUSD": "XRPUSDT",
    # añade los que uses...
}

# APALANCAMIENTO POR GRUPOS
GROUP_1 = ["APTUSDT"]
GROUP_2 = ["ZILUSDT"]
GROUP_3 = ["ARBUSDT"]
GROUP_4 = ["ATOMUSDT"]
GROUP_5 = ["AVAXUSDT"]            #AJUSTAR USDT X TRADE A 3.0 EN MINIMO, 3.10 MAXIMO, 3.05 POR DEFECTO  

# ajuste de apalancamiento para binance

GROUP_LEVERAGE = {
    "GROUP_1": 7,
    "GROUP_2": 13,
    "GROUP_3": 35,
    "GROUP_4": 3,
    "GROUP_5": 10
}

USE_ISOLATED = True
PARTIAL_CLOSE_RATIO = Decimal("0.30")

# ---------------- logging & binance client ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
# configure client session retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500,502,503,504])
session.mount('https://', HTTPAdapter(max_retries=retries))
client._session = session

# exchangeInfo cache
_exchangeinfo_cache = {"timestamp": 0, "data": None}
_exchangeinfo_lock = Lock()

# MT5 connected flag
_mt5_connected = False

# replicated positions store: mt5_ticket -> {mt5_symbol, bin_symbol, mt5_volume, bin_side, bin_qty (Decimal), bin_orderId}
replicated_positions = {}
replicated_lock = Lock()

# ---------------- Helpers ----------------

def status_print(msg: str, status: str="info"):
    if status == "yellow":
        col = Fore.YELLOW
    elif status == "green":
        col = Fore.GREEN
    elif status == "red":
        col = Fore.RED
    elif status == "blue":
        col = Fore.CYAN
    elif status == "magenta":
        col = Fore.MAGENTA
    else:
        col = Fore.WHITE
    print(col + msg + Style.RESET_ALL)

def get_exchange_info():
    with _exchangeinfo_lock:
        now = time.time()
        if _exchangeinfo_cache["data"] and (now - _exchangeinfo_cache["timestamp"] < EXCHANGEINFO_CACHE_TTL):
            return _exchangeinfo_cache["data"]
        for attempt in range(MAX_RETRIES):
            try:
                info = client.futures_exchange_info()
                _exchangeinfo_cache["data"] = info
                _exchangeinfo_cache["timestamp"] = now
                return info
            except Exception as e:
                logging.warning(f"Error fetching exchangeInfo (attempt {attempt+1}): {e}")
                time.sleep(0.5*(attempt+1))
        raise RuntimeError("No se pudo obtener exchangeInfo tras varios intentos.")

def get_symbol_filters(symbol: str):
    info = get_exchange_info()
    for s in info.get("symbols", []):
        if s.get("symbol") == symbol:
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            lot = filters.get("LOT_SIZE")
            min_notional = filters.get("MIN_NOTIONAL")
            return {
                "minQty": Decimal(str(lot["minQty"])) if lot else Decimal("0"),
                "stepSize": Decimal(str(lot["stepSize"])) if lot else Decimal("1"),
                "minNotional": Decimal(str(min_notional["notional"])) if min_notional else Decimal("0"),
            }
    return None

def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return qty
    multiplier = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    val = multiplier * step
    step_str = format(step.normalize(), 'f')
    if '.' in step_str:
        decimals = len(step_str.split('.')[1])
        return val.quantize(Decimal((0, (1,), -decimals)))
    return val

def get_mark_price(symbol: str) -> Decimal:
    for attempt in range(MAX_RETRIES):
        try:
            mp = client.futures_mark_price(symbol=symbol)
            return Decimal(str(mp["markPrice"]))
        except Exception as e:
            logging.warning(f"Error fetching mark price for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(0.5*(attempt+1))
    raise RuntimeError(f"No se pudo obtener mark price para {symbol}")

def get_leverage_for_binance_symbol(symbol: str) -> int:
    if symbol in GROUP_1:
        return GROUP_LEVERAGE["GROUP_1"]
    if symbol in GROUP_2:
        return GROUP_LEVERAGE["GROUP_2"]
    if symbol in GROUP_3:
        return GROUP_LEVERAGE["GROUP_3"]
    if symbol in GROUP_4:
        return GROUP_LEVERAGE["GROUP_4"]
    if symbol in GROUP_5:
        return GROUP_LEVERAGE["GROUP_5"]
    return 20

def configure_symbol_margin_and_leverage(symbol: str, leverage: int):
    """Set margin type to ISOLATED (if enabled) and leverage. Ignore -4046 (already set)."""
    try:
        if USE_ISOLATED:
            try:
                client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
                logging.info(f"{Fore.GREEN}Margin type ISOLATED aplicado a {symbol}{Style.RESET_ALL}")
            except BinanceAPIException as e:
                code = getattr(e, "code", None)
                if code == -4046:
                    logging.info(f"{Fore.YELLOW}{symbol} ya estaba en ISOLATED — se omite.{Style.RESET_ALL}")
                else:
                    raise
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        logging.info(f"{Fore.GREEN}✅ Apalancamiento {leverage}x aplicado a {symbol}{Style.RESET_ALL}")
        return True
    except BinanceAPIException as e:
        logging.error(f"{Fore.RED}BinanceAPIException on configure {symbol}: {e}{Style.RESET_ALL}")
        return False
    except Exception as e:
        logging.error(f"{Fore.RED}Error configure {symbol}: {e}{Style.RESET_ALL}")
        return False

def calculate_qty_from_usdt(symbol: str, usdt_amount: Decimal, leverage: int) -> Decimal:
    """qty = floor( (usdt * leverage) / mark_price, stepSize ). Validate minQty and minNotional."""
    mark = get_mark_price(symbol)
    notional = (usdt_amount * Decimal(leverage))
    qty_raw = (notional) / mark
    filters = get_symbol_filters(symbol)
    if not filters:
        raise RuntimeError(f"No se encontraron filtros para {symbol}")
    step = filters["stepSize"]
    minQty = filters["minQty"]
    qty = floor_to_step(qty_raw, step)
    # check minQty
    if qty < minQty:
        return Decimal("0")
    # check minNotional
    minNotional = filters.get("minNotional", Decimal("0"))
    if (qty * mark) < minNotional:
        return Decimal("0")
    return qty

def place_order_with_retry(symbol, side, order_type, quantity_str, reduceOnly=False, price=None, stopPrice=None, timeInForce=None):
    for attempt in range(MAX_RETRIES):
        try:
            params = {
                "symbol": symbol,
                "side": side,
                "type": order_type,
                "quantity": quantity_str
            }
            if reduceOnly:
                params["reduceOnly"] = True
            if price is not None:
                params["price"] = str(price)
            if stopPrice is not None:
                params["stopPrice"] = str(stopPrice)
            if timeInForce is not None:
                params["timeInForce"] = timeInForce
            res = client.futures_create_order(**params)
            return res
        except BinanceAPIException as e:
            logging.warning(f"BinanceAPIException place order {symbol} (attempt {attempt+1}): {e}")
            time.sleep(0.5*(attempt+1))
        except Exception as e:
            logging.warning(f"Error placing order {symbol} (attempt {attempt+1}): {e}")
            time.sleep(0.5*(attempt+1))
    raise RuntimeError(f"No se pudo colocar la orden para {symbol} tras {MAX_RETRIES} intentos.")

def cancel_all_conditional_orders(symbol: str):
    """Cancela órdenes condicionales abiertas (STOP/TP/TAKE_PROFIT) para un símbolo."""
    for attempt in range(MAX_RETRIES):
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
            return
        except Exception as e:
            logging.warning(f"Error canceling orders for {symbol} (attempt {attempt+1}): {e}")
            time.sleep(0.5*(attempt+1))
    logging.error(f"No se pudieron cancelar todas las órdenes para {symbol}")

# ---------------- Normalización MT5 ----------------

def normalize_mt5_symbol(sym: str) -> str:
    """
    Normaliza símbolos MT5 para buscar en MT5_TO_BINANCE:
    - elimina puntos
    - mayúsculas
    - adapta sufijos si tu bróker usa otros
    """
    if not sym:
        return sym
    s = str(sym).upper().replace(".", "")
    # si tu bróker usa "USD" o "USDT" etc, aquí podrías transformar
    return s

def normalize_mt5_position(pos):
    """Normaliza objeto MT5 position a dict útil."""
    try:
        ticket = int(getattr(pos, "ticket", 0) or 0)
        raw_symbol = getattr(pos, "symbol", None)
        symbol = normalize_mt5_symbol(raw_symbol)
        volume = Decimal(str(getattr(pos, "volume", "0")))
        type_val = getattr(pos, "type", None)
        type_str = "BUY" if type_val == 0 else "SELL"
        price_open = Decimal(str(getattr(pos, "price_open", "0")))
        sl_val = getattr(pos, "sl", 0.0)
        tp_val = getattr(pos, "tp", 0.0)
        sl = Decimal(str(sl_val)) if sl_val is not None and float(sl_val) != 0.0 else None
        tp = Decimal(str(tp_val)) if tp_val is not None and float(tp_val) != 0.0 else None
        return {
            "ticket": ticket,
            "symbol": symbol,
            "volume": volume,
            "price_open": price_open,
            "sl": sl,
            "tp": tp,
            "type": type_str
        }
    except Exception as e:
        logging.error(f"Error normalizing MT5 position: {e}")
        return None

# ---------------- Core replication & sync ----------------

def map_symbol(mt5_symbol: str):
    return MT5_TO_BINANCE.get(mt5_symbol)

def update_sl_tp_on_binance(bin_symbol: str, mt5_sl: Decimal, mt5_tp: Decimal, local_rec: dict):
    """Cancela condicionales previas y crea nuevas SL/TP reduceOnly según MT5."""
    try:
        cancel_all_conditional_orders(bin_symbol)
    except Exception:
        pass

    side = local_rec.get("bin_side")
    qty = Decimal(local_rec.get("bin_qty"))
    filters = get_symbol_filters(bin_symbol)
    step = filters["stepSize"]
    step_str = format(step.normalize(), 'f')
    decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
    qty_str = f"{qty:.{decimals}f}"

    # TAKE PROFIT
    if mt5_tp:
        try:
            side_tp = "SELL" if side == "BUY" else "BUY"
            params = {
                "symbol": bin_symbol,
                "side": side_tp,
                "type": "TAKE_PROFIT_MARKET",
                "quantity": qty_str,
                "stopPrice": str(mt5_tp),
                "reduceOnly": True
            }
            client.futures_create_order(**params)
            status_print(f"TP establecido para {bin_symbol} en {mt5_tp}", "blue")
        except Exception as e:
            logging.error(f"Error creando TP en Binance: {e}")

    # STOP LOSS
    if mt5_sl:
        try:
            side_sl = "SELL" if side == "BUY" else "BUY"
            params = {
                "symbol": bin_symbol,
                "side": side_sl,
                "type": "STOP_MARKET",
                "quantity": qty_str,
                "stopPrice": str(mt5_sl),
                "reduceOnly": True
            }
            client.futures_create_order(**params)
            status_print(f"SL establecido para {bin_symbol} en {mt5_sl}", "blue")
        except Exception as e:
            logging.error(f"Error creando SL en Binance: {e}")

def replicate_position_to_binance(mt5_pos):
    """Replica apertura (y SL/TP) en Binance usando TRADE_USDT & leverage groups."""
    pos = normalize_mt5_position(mt5_pos)
    if not pos:
        return
    mt5_sym = pos["symbol"]
    bin_sym = map_symbol(mt5_sym)
    if not bin_sym:
        status_print(f"Símbolo {mt5_sym} no está en mapeo; ignorando.", "yellow")
        return

    side_bin = "BUY" if pos["type"] == "BUY" else "SELL"
    leverage = get_leverage_for_binance_symbol(bin_sym)

    # Configure margin & leverage (ignore -4046)
    configured = configure_symbol_margin_and_leverage(bin_sym, leverage)
    if not configured:
        status_print(f"No se pudo configurar {bin_sym} — se omitirá temporalmente.", "yellow")
        return

    # Ensure TRADE_USDT within range
    trade_usdt = TRADE_USDT
    if trade_usdt < MIN_TRADE_USDT or trade_usdt > MAX_TRADE_USDT:
        logging.warning("TRADE_USDT fuera de rango; ajustando a rango permitido.")
        trade_usdt = max(MIN_TRADE_USDT, min(MAX_TRADE_USDT, trade_usdt))

    candidate_usdts = [trade_usdt,
                       max(MIN_TRADE_USDT, trade_usdt - TRADE_USDT_TOLERANCE),
                       min(MAX_TRADE_USDT, trade_usdt + TRADE_USDT_TOLERANCE)]

    chosen_qty = Decimal("0")
    chosen_usdt = None
    for u in candidate_usdts:
        try:
            qty = calculate_qty_from_usdt(bin_sym, Decimal(u), leverage)
        except Exception as e:
            logging.warning(f"Error calculando qty para {bin_sym} con usdt {u}: {e}")
            qty = Decimal("0")
        if qty > 0:
            chosen_qty = qty
            chosen_usdt = Decimal(u)
            break

    if chosen_qty == 0:
        status_print(f"No se pudo calcular qty válida para {bin_sym} (trade_usdt ~ {trade_usdt}). Se omite.", "yellow")
        return

    # format qty string
    filters = get_symbol_filters(bin_sym)
    step = filters["stepSize"]
    step_str = format(step.normalize(), 'f')
    decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
    qty_str = f"{chosen_qty:.{decimals}f}"

    ticket = pos["ticket"]
    with replicated_lock:
        existing = replicated_positions.get(ticket)

    if existing:
        # MT5 decreased volume -> partial close on Binance
        existing_vol = existing.get("mt5_volume", Decimal("0"))
        if pos["volume"] < existing_vol:
            try:
                ratio = (existing_vol - pos["volume"]) / existing_vol if existing_vol != 0 else Decimal("0")
                close_qty = floor_to_step( (Decimal(str(existing["bin_qty"])) * ratio), step)
                if close_qty > 0:
                    status_print(f"Detected partial close on MT5 ticket {ticket}: closing {close_qty} {bin_sym} on Binance", "yellow")
                    side_close = "SELL" if existing["bin_side"] == "BUY" else "BUY"
                    res = place_order_with_retry(bin_sym, side_close, "MARKET", f"{close_qty:.{decimals}f}", reduceOnly=True)
                    status_print(f"Partial close placed: {res}", "yellow")
                    with replicated_lock:
                        replicated_positions[ticket]["mt5_volume"] = pos["volume"]
                        replicated_positions[ticket]["bin_qty"] = Decimal(str(existing["bin_qty"])) - close_qty
            except Exception as e:
                logging.error(f"Error al procesar partial close: {e}\n{traceback.format_exc()}")

        # Update SL/TP if changed
        try:
            update_sl_tp_on_binance(bin_sym, pos["sl"], pos["tp"], existing)
        except Exception as e:
            logging.error(f"Error actualizando SL/TP: {e}")
    else:
        # New open on Binance
        status_print(f"Abriendo nueva posición replicate: MT5 {mt5_sym} -> Binance {bin_sym} | side={side_bin}", "blue")
        try:
            res = place_order_with_retry(bin_sym, side_bin, "MARKET", qty_str)
            with replicated_lock:
                replicated_positions[ticket] = {
                    "mt5_symbol": mt5_sym,
                    "bin_symbol": bin_sym,
                    "mt5_volume": pos["volume"],
                    "bin_side": side_bin,
                    "bin_qty": chosen_qty,
                    "bin_order": res
                }
            status_print(f"Posición replicada: {bin_sym} qty={qty_str} | side={side_bin}", "green" if side_bin=="BUY" else "red")
            # add SL/TP if present
            if pos["sl"] or pos["tp"]:
                try:
                    update_sl_tp_on_binance(bin_sym, pos["sl"], pos["tp"], replicated_positions[ticket])
                except Exception as e:
                    logging.error(f"Error creando SL/TP inicial: {e}")
        except Exception as e:
            logging.error(f"Error al crear posición en Binance: {e}\n{traceback.format_exc()}")

# ---------------- Synchronization inverse (MT5 removed -> close Binance) ----------------

def sync_inverse_close(mt5_positions):
    """
    Cierra en Binance solo posiciones cuyo símbolo NO exista entre las posiciones abiertas en MT5.
    Para ello:
      - construye el set de bin_symbols que están representadas en MT5 (mapeo MT5->BINANCE)
      - por cada posición con positionAmt != 0 en Binance, si symbol NOT IN set => cerrar
    Esto evita cerrar posiciones justo después de abrirlas (porque la MT5->Binance mapping coincidirá).
    """
    try:
        # construir set de símbolos Binance presentes en MT5
        mt5_bin_symbols = set()
        if mt5_positions:
            for p in mt5_positions:
                norm = normalize_mt5_symbol(getattr(p, "symbol", None))
                mapped = map_symbol(norm)
                if mapped:
                    mt5_bin_symbols.add(mapped)

        acct = client.futures_account()
        for p in acct.get("positions", []):
            sym = p["symbol"]
            amt = Decimal(str(p["positionAmt"]))
            if amt != 0 and sym not in mt5_bin_symbols:
                # cerrar pos en Binance (reduceOnly)
                step_filters = get_symbol_filters(sym)
                if not step_filters:
                    logging.warning(f"No se encontraron filtros para {sym}, no se cierra automáticamente.")
                    continue
                step = step_filters["stepSize"]
                qty = floor_to_step(abs(amt), step)
                if qty <= 0:
                    logging.warning(f"No se pudo calcular qty para cerrar {sym}")
                    continue
                qty_str = format(qty, 'f')
                side = "SELL" if amt > 0 else "BUY"
                try:
                    client.futures_create_order(symbol=sym, side=side, type="MARKET", quantity=qty_str, reduceOnly=True)
                    status_print(f"Cerrada {sym} en Binance porque no existe en MT5 (qty {qty_str})", "magenta")
                except Exception as e:
                    logging.error(f"Error cerrando {sym} en Binance: {e}")
    except Exception as e:
        logging.error(f"Error al sincronizar cierre inverso: {e}\n{traceback.format_exc()}")

# ---------------- Polling and threading ----------------

def worker_handle_position(pos_obj):
    try:
        replicate_position_to_binance(pos_obj)
    except Exception as e:
        logging.error(f"Worker error: {e}\n{traceback.format_exc()}")

def poll_mt5_and_replicate():
    global _mt5_connected
    if not _mt5_connected:
        if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            logging.error(f"No se pudo inicializar MT5: {mt5.last_error()}")
            return
        _mt5_connected = True
        logging.info("Conectado a MT5.")

    while True:
        try:
            positions = mt5.positions_get()
            if not positions or len(positions) == 0:
                status_print("Sin posiciones abiertas en MT5.", "yellow")
            else:
                status_print(f"Revisando {len(positions)} posiciones en MT5...", "blue")
                ticket_map = {}
                for p in positions:
                    norm = normalize_mt5_position(p)
                    if norm:
                        ticket_map[norm["ticket"]] = p
                threads = []
                for ticket, pos_obj in ticket_map.items():
                    t = Thread(target=worker_handle_position, args=(pos_obj,))
                    t.daemon = True
                    t.start()
                    threads.append(t)
                # not joining threads to keep loop responsive

            # inverse sync: close Binance positions that no longer exist in MT5 (using the improved logic)
            try:
                sync_inverse_close(positions)
            except Exception as e:
                logging.error(f"Error syncing inverse close: {e}")

            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logging.info("Interrupción por teclado: cerrando.")
            break
        except Exception as e:
            logging.error(f"Error en polling loop: {e}\n{traceback.format_exc()}")
            time.sleep(1)

# ---------------- Entrypoint ----------------

if __name__ == "__main__":
    status_print("Iniciando MT5->Binance Futures copier bot...", "blue")
    status_print(f"Trade USDT objetivo: {TRADE_USDT} (min {MIN_TRADE_USDT} max {MAX_TRADE_USDT})", "yellow")
    try:
        poll_mt5_and_replicate()
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
